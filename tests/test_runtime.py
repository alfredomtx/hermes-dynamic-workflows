from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_dynamic_workflows.engine.cache import ResumeCache, agent_fingerprint, is_cache_miss
from hermes_dynamic_workflows.engine.api import WorkflowAPI
from hermes_dynamic_workflows.engine.context import PauseGate, WorkflowExecutionContext
from hermes_dynamic_workflows.core.config import PluginConfig, load_config
from hermes_dynamic_workflows.core.errors import (
    ChildAgentError,
    ChildAgentSkipped,
    WorkflowLimitExceeded,
    WorkflowParseError,
    WorkflowRuntimeError,
)
from hermes_dynamic_workflows.engine.runtime import WorkflowOptions, run_workflow
from hermes_dynamic_workflows.core.types import (
    AgentHandleRecord,
    ChildAgentRequest,
    ChildAgentResult,
    ChildAgentRunner,
    WorkflowFrame,
)
from hermes_dynamic_workflows.adapters.workflow import DYNAMIC_WORKFLOW_SCHEMA
from hermes_dynamic_workflows.storage.store import WorkflowStore


class FakeRunner(ChildAgentRunner):
    def __init__(self, responses=None):
        self.requests: list[ChildAgentRequest] = []
        self.responses = list(responses or [])

    def run(self, request: ChildAgentRequest):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return f"{request.label}:{request.prompt}"


class RemovedChildToolBudgetTests(unittest.TestCase):
    _base = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "maxTurns": 10,
    }

    def _script(self, options):
        return (
            'meta = {"name": "removed-budget-contract", "description": "Test workflow"}\n'
            f"return await agent(\"go\", {options!r})"
        )

    def test_omitted_tool_budgets_are_absent_from_config_request_and_public_schema(self):
        config = PluginConfig()
        runner = FakeRunner()
        result = run_workflow(
            self._script(self._base),
            WorkflowOptions(config=config, child_runner=runner),
        )

        request = runner.requests[0]
        self.assertFalse(hasattr(config, "max_tool_calls"))
        self.assertFalse(hasattr(config, "max_tool_output_chars"))
        self.assertNotIn("max_tool_calls", request.__dict__)
        self.assertNotIn("max_tool_output_chars", request.__dict__)
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertNotIn("maxToolCalls", request.resolved.cache_inputs())
        self.assertNotIn("maxToolOutputChars", request.resolved.cache_inputs())
        agent = result.state.snapshot()["agents"][0]
        self.assertNotIn("max_tool_calls", agent)
        self.assertNotIn("max_tool_output_chars", agent)
        description = DYNAMIC_WORKFLOW_SCHEMA["description"]
        for removed in (
            "maxToolCalls",
            "maxToolOutputChars",
            "max_tool_calls",
            "max_tool_output_chars",
        ):
            self.assertNotIn(removed, description)

    def test_explicit_removed_tool_budgets_are_unsupported(self):
        for key in ("maxToolCalls", "maxToolOutputChars"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(Exception, r"unsupported agent\(\) option"):
                    run_workflow(
                        self._script({**self._base, key: 1}),
                        WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()),
                    )


class IdRunner(ChildAgentRunner):
    def __init__(self):
        self.requests: list[ChildAgentRequest] = []

    def run(self, request: ChildAgentRequest):
        self.requests.append(request)
        return f"{request.id}:{request.label}"


class TokenRunner(ChildAgentRunner):
    def __init__(self, tokens: int):
        self.tokens = tokens

    def run(self, request: ChildAgentRequest):
        return ChildAgentResult(content=request.label, metadata={"tokens": self.tokens})


class LiveUpdateRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        if request.on_start is not None:
            request.on_start(
                {
                    "task_id": "workflow-live",
                    "session_id": "workflow-live",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "medium",
                }
            )
        if request.on_update is not None:
            request.on_update(
                {
                    "tokens": 321,
                    "tool_calls": 2,
                    "activity": 'terminal({"command":"pwd"})',
                }
            )
        return ChildAgentResult(
            content="done",
            metadata={"tokens": 321, "tool_calls": 2},
        )


class FailingRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        raise RuntimeError(f"failed:{request.label}")


class FailedMetadataRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        if request.on_update is not None:
            request.on_update(
                {
                    "tokens": 37,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "reasoning_tokens": 7,
                    "cache_read_tokens": 3,
                    "cache_write_tokens": 2,
                    "tool_calls": 1,
                    "stop_reason": "maxToolCalls",
                }
            )
        raise ChildAgentError("child exhausted maxToolCalls=1")


class SkippingRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        raise ChildAgentSkipped("skipped by user")


class DurableHandleRunner(ChildAgentRunner):
    def __init__(self, *, block=False):
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests: list[ChildAgentRequest] = []
        self.calls = 0

    def run(self, request: ChildAgentRequest):
        self.requests.append(request)
        self.calls += 1
        self.started.set()
        if self.block:
            self.release.wait(timeout=5)
        session_id = request.session_id or f"session-{self.calls}"
        return ChildAgentResult(
            content=f"result:{request.prompt}",
            metadata={
                "task_id": f"task-{self.calls}",
                "session_id": session_id,
                "hermes_session_id": session_id,
                "workspace": request.cwd,
            },
        )


class InterruptibleHandleRunner(DurableHandleRunner):
    def __init__(self, *, acknowledge=False):
        super().__init__(block=True)
        self.acknowledge = acknowledge
        self.interrupt_ids: list[str] = []

    def skip_child(self, task_id: str) -> bool:
        self.interrupt_ids.append(task_id)
        if self.acknowledge:
            self.release.set()
        return self.acknowledge


def _durable_api(
    runner,
    *,
    cwd="/workspace",
    events=None,
    agent_handles=None,
    handle_lineage_id=None,
):
    root = WorkflowFrame(id="root", meta={"name": "handles"}, args=None, cwd=cwd)
    context = WorkflowExecutionContext(
        config=PluginConfig(),
        runner=runner,
        codex_runner=SimpleNamespace(),
        stop_event=threading.Event(),
        pause_gate=PauseGate(),
        resume_cache=ResumeCache(),
        deadline=10**12,
        root=root,
        on_journal=(events.append if events is not None else None),
        initial_agent_handles=agent_handles or {},
        handle_lineage_id=handle_lineage_id or "lineage-test",
    )
    return WorkflowAPI(context=context, frame=root)


class RuntimeTests(unittest.TestCase):
    def test_live_child_updates_refresh_snapshot_and_journal(self):
        events = []
        result = run_workflow(
            'meta = {"name": "live", "description": "live"}\nreturn await agent("work", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})',
            WorkflowOptions(
                config=PluginConfig(),
                child_runner=LiveUpdateRunner(),
                on_journal=events.append,
            ),
        )

        agent = result.state.snapshot()["agents"][0]
        self.assertEqual(agent["tokens"], 321)
        self.assertEqual(agent["tool_calls"], 2)
        self.assertEqual(agent["model"], "gpt-5.6-luna")
        self.assertEqual(agent["reasoning_effort"], "medium")
        workflow_events = [
            event for event in events if event["type"] != "agent_lifecycle"
        ]
        self.assertEqual(
            [event["type"] for event in workflow_events],
            ["started", "activity", "result"],
        )
        self.assertIn("pwd", workflow_events[1]["activity"])

    def test_runs_strict_async_script_body(self):
        script = """
meta = {"name": "simple", "description": "Test workflow", "phases": ["scan"]}

phase("scan")
return await agent("inspect repo", {"label": "scan-agent", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        with patch(
            "hermes_dynamic_workflows.child.runner._discoverable_child_toolsets",
            return_value=[],
        ):
            result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(result.value, "scan-agent:inspect repo")
        self.assertEqual(result.agent_count, 1)
        self.assertEqual(runner.requests[0].label, "scan-agent")
        self.assertEqual(runner.requests[0].toolsets, ["web", "file", "terminal", "skills"])
        self.assertEqual(result.state.current_phase, "scan")

    def test_rejects_sync_workflow_function(self):
        script = """
meta = {"name": "sync-is-not-supported", "description": "Test workflow"}

def workflow():
    return "old sync DSL"
"""
        with self.assertRaises(WorkflowParseError) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))

        self.assertIn("do not define workflow()", str(ctx.exception))

    def test_top_level_await_script_body(self):
        script = """
meta = {"name": "top-level-await", "description": "Test workflow", "phases": ["scan"]}

phase("scan")
return await agent("inspect repo", {"label": "top-agent", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(result.value, "top-agent:inspect repo")
        self.assertEqual(result.agent_count, 1)
        self.assertEqual(runner.requests[0].label, "top-agent")
        self.assertEqual(result.state.current_phase, "scan")

    def test_workflow_script_supports_isinstance(self):
        script = """
meta = {"name": "isinstance", "description": "Test workflow"}
return isinstance("value", str)
"""
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))

        self.assertTrue(result.value)

    def test_parallel_preserves_order(self):
        script = """
meta = {"name": "parallel", "description": "Test workflow"}

return await parallel([
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
    lambda: agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
])
"""
        runner = FakeRunner()
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(concurrency=2), child_runner=runner),
        )

        self.assertEqual(result.value, ["a:a", "b:b", "c:c"])
        self.assertEqual({req.label for req in runner.requests}, {"a", "b", "c"})

    def test_parallel_records_runtime_topology(self):
        script = """
meta = {"name": "parallel-topology", "description": "Test workflow"}

return await parallel([
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3}),
    lambda: agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3}),
])
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(concurrency=3), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "parallel", "status": "done", "lanes": 3, "agent_ids": [1, 2, 3]}],
        )

    def test_pipeline_records_items_and_stages_without_counting_inner_agents_as_sequential(self):
        script = """
meta = {"name": "pipeline-topology", "description": "Test workflow"}

async def inspect(value, original, index):
    return await agent("inspect " + value, {"label": "inspect:" + value, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})

async def verify(value, original, index):
    return await agent("verify " + value, {"label": "verify:" + original, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})

return await pipeline(["a", "b"], inspect, verify)
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(concurrency=4), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "pipeline", "status": "done", "items": 2, "stages": 2, "agent_ids": [1, 2, 3, 4]}],
        )

    def test_direct_agents_record_observed_sequential_steps(self):
        script = """
meta = {"name": "sequential-topology", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})
await agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})
return await agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "sequential", "status": "done", "steps": 3, "agent_ids": [1, 2, 3]}],
        )

    def test_parallel_rejects_arrays_over_vm_boundary_before_agent_launch(self):
        script = """
meta = {"name": "too-many-parallel", "description": "Test workflow"}

thunks = [lambda i=i: agent(str(i), {"label": str(i)}) for i in range(4097)]
return await parallel(thunks)
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests, [])
        self.assertIn(
            "array length 4097 exceeds the maximum of 4096 supported across the workflow VM boundary",
            str(ctx.exception),
        )

    def test_pipeline_rejects_arrays_over_vm_boundary_before_agent_launch(self):
        script = """
meta = {"name": "too-many-pipeline", "description": "Test workflow"}

items = list(range(4097))
return await pipeline(items, lambda item, original, index: agent(str(item)))
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests, [])
        self.assertIn(
            "array length 4097 exceeds the maximum of 4096 supported across the workflow VM boundary",
            str(ctx.exception),
        )

    def test_structured_output(self):
        script = """
meta = {"name": "structured", "description": "Test workflow"}

return await agent(
    "return status",
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "schema": {"type": "object", "required": ["ok"]}},
)
"""
        runner = FakeRunner(
            responses=[
                ChildAgentResult(
                    content="done",
                    metadata={
                        "structured_captured": True,
                        "structured_result": {"ok": True},
                        "structured_attempts": 1,
                    },
                )
            ]
        )
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(result.value, {"ok": True})

    def test_structured_output_does_not_parse_final_message(self):
        script = """
meta = {"name": "structured-no-parse", "description": "Test workflow"}

return await agent(
    "return status",
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "schema": {"type": "object", "required": ["ok"]}},
)
"""
        runner = FakeRunner(responses=['{"ok": true}'])
        with self.assertRaises(ChildAgentError):
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
        self.assertEqual(len(runner.requests), 1)

    def test_invalid_structured_schema_fails_before_child_launch(self):
        script = """
meta = {"name": "invalid-schema", "description": "Test workflow"}

return await agent(
    "return status",
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "schema": {"type": 123}},
)
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests, [])
        self.assertIn("invalid JSON Schema", str(ctx.exception))

    def test_agent_rejects_runtime_policy_options(self):
        script = """
meta = {"name": "unsupported-options", "description": "Test workflow"}

return await agent("go", {"label": "r", "toolsets": ["web"], "retries": 2})
"""
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))
        self.assertIn("unsupported agent() option(s): retries", str(ctx.exception))
        self.assertIn("toolsets", str(ctx.exception))


    def test_agent_rejects_auto_provider(self):
        script = """
meta = {"name": "auto-provider", "description": "Test workflow"}
return await agent("go", {"provider": "auto", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 10})
"""
        with self.assertRaisesRegex(Exception, "provider must be explicit"):
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))

    def test_agent_rejects_configured_model_alias(self):
        script = """
meta = {"name": "model-alias", "description": "Test workflow"}
return await agent("go", {"provider": "bedrock", "model": "sonnet", "reasoningEffort": "high", "maxTurns": 10})
"""
        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"model_aliases": {"sonnet": {"provider": "bedrock", "model": "canonical"}}},
            ),
            self.assertRaisesRegex(Exception, "canonical model id"),
        ):
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))

    def test_agent_accepts_inline_runtime_agent_options(self):
        script = """
meta = {"name": "inline-agent", "description": "Test workflow"}

return await agent(
    "go",
    {
        "label": "inline",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "maxTurns": 10,
        "instructions": "INLINE ROLE",
        "toolsets": ["file"],
        "allowedTools": ["read_file", "search_files"],
        "disallowedTools": ["write_file"],
    },
)
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(result.value, "inline:go")
        request = runner.requests[0]
        self.assertEqual(request.toolsets, ["file"])
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertIn("INLINE ROLE", request.resolved.agent_type_spec.instructions)
        self.assertEqual(request.resolved.allowed_tools, ("read_file", "search_files"))
        self.assertTrue(request.resolved.allowed_tools_explicit)
        self.assertEqual(request.resolved.disallowed_tools, ("write_file",))
        self.assertTrue(request.resolved.toolsets_explicit)

    def test_inline_toolsets_empty_is_explicit_no_tools(self):
        script = """
meta = {"name": "inline-empty-tools", "description": "Test workflow"}

return await agent("go", {"label": "empty", "toolsets": [], "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests[0].toolsets, [])
        self.assertIsNotNone(runner.requests[0].resolved)
        assert runner.requests[0].resolved is not None
        self.assertTrue(runner.requests[0].resolved.toolsets_explicit)

    def test_inline_toolsets_none_inherits_default_toolsets(self):
        script = """
meta = {"name": "inline-none-tools", "description": "Test workflow"}

return await agent("go", {"label": "none", "toolsets": None, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        with patch(
            "hermes_dynamic_workflows.child.runner._discoverable_child_toolsets",
            return_value=[],
        ):
            run_workflow(
                script,
                WorkflowOptions(
                    config=PluginConfig(default_child_toolsets=("file", "terminal")),
                    child_runner=runner,
                ),
            )

        self.assertEqual(runner.requests[0].toolsets, ["file", "terminal"])
        self.assertIsNotNone(runner.requests[0].resolved)
        assert runner.requests[0].resolved is not None
        self.assertTrue(runner.requests[0].resolved.toolsets_explicit)

    def test_inline_allowed_tools_none_inherits_preset_allowlist(self):
        script = """
meta = {
    "name": "inline-none-allow",
    "description": "Test workflow",
    "agents": {
        "reader": {
            "instructions": "RUNTIME READER",
            "toolsets": ["file"],
            "allowedTools": ["read_file"],
        }
    },
}

return await agent("go", {"agentType": "reader", "label": "reader", "allowedTools": None, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertIsNotNone(runner.requests[0].resolved)
        assert runner.requests[0].resolved is not None
        self.assertEqual(runner.requests[0].resolved.allowed_tools, ("read_file",))
        self.assertTrue(runner.requests[0].resolved.allowed_tools_explicit)

    def test_runtime_meta_agent_definition_resolves_agent_type(self):
        script = """
meta = {
    "name": "runtime-agent",
    "description": "Test workflow",
    "agents": {
        "reader": {
            "instructions": "RUNTIME READER",
            "toolsets": ["file"],
            "allowedTools": ["read_file"],
        }
    },
}

return await agent("go", {"agentType": "reader", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        request = runner.requests[0]
        self.assertEqual(request.agent_type, "reader")
        self.assertEqual(request.toolsets, ["file"])
        self.assertEqual(request.model, "gpt-5.6-luna")
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertEqual(request.resolved.agent_type_spec.source, "meta.agents.reader")
        self.assertIn("RUNTIME READER", request.resolved.agent_type_spec.instructions)
        self.assertEqual(request.resolved.allowed_tools, ("read_file",))
        self.assertTrue(request.resolved.allowed_tools_explicit)

    def test_agent_requires_inline_provider_model_and_effort_before_launch(self):
        complete = {
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
            "reasoningEffort": "high",
            "maxTurns": 10,
        }
        for missing in ("provider", "model", "reasoningEffort"):
            opts = {key: value for key, value in complete.items() if key != missing}
            script = (
                'meta = {"name": "missing-routing", "description": "Test workflow"}\n'
                f'return await agent("go", {opts!r})\n'
            )
            runner = FakeRunner()

            with self.subTest(missing=missing):
                with self.assertRaises(Exception) as ctx:
                    run_workflow(
                        script,
                        WorkflowOptions(config=PluginConfig(), child_runner=runner),
                    )

                self.assertIn(f"agent() {missing} is required", str(ctx.exception))
                self.assertEqual(runner.requests, [])

    def test_inline_provider_model_and_effort_reach_request_and_cache_inputs(self):
        script = """
meta = {"name": "explicit-routing", "description": "Test workflow"}

return await agent("go", {
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "max",
    "maxTurns": 10,
})
"""
        runner = FakeRunner()
        run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        request = runner.requests[0]
        self.assertEqual(request.provider, "openai-codex")
        self.assertEqual(request.model, "gpt-5.6-luna")
        self.assertEqual(request.reasoning_effort, "max")
        self.assertEqual(request.resolved.cache_inputs()["provider"], "openai-codex")

    def test_runtime_preset_rejects_routing_and_nested_agent_type_fields(self):
        cases = {
            "provider": '"provider": "openai-codex"',
            "model": '"model": "gpt-5.6-luna"',
            "reasoningEffort": '"reasoningEffort": "high"',
            "agentType": '"agentType": "activix-reviewer"',
        }
        for field, definition in cases.items():
            script = f'''meta = {{
    "name": "preset-routing",
    "description": "Test workflow",
    "agents": {{"reader": {{"instructions": "Read.", {definition}}}}},
}}
return await agent("go", {{
    "agentType": "reader",
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "xhigh",
    "maxTurns": 10,
}})
'''
            runner = FakeRunner()

            with self.subTest(field=field), self.assertRaises(Exception) as ctx:
                run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

            self.assertIn(f"meta.agents.reader {field} is not supported", str(ctx.exception))
            self.assertEqual(runner.requests, [])

    def test_phase_model_is_rejected(self):
        script = """
meta = {
    "name": "phase-routing",
    "description": "Test workflow",
    "phases": [{"title": "Audit", "model": "gpt-5.6-sol"}],
}
phase("Audit")
return await agent("go", {
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "high",
    "maxTurns": 10,
})
"""
        runner = FakeRunner()

        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertIn("meta.phases[].model is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_runtime_meta_agent_precedes_project_file_agent(self):
        script = """
meta = {
    "name": "runtime-precedence",
    "description": "Test workflow",
    "agents": {
        "reader": {
            "instructions": "META WINS",
            "toolsets": ["file"],
        }
    },
}

return await agent("go", {"agentType": "reader", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".hermes" / "dynamic-workflows" / "agents"
            agent_dir.mkdir(parents=True)
            (agent_dir / "reader.md").write_text("FILE LOSES", encoding="utf-8")
            runner = FakeRunner()
            run_workflow(script, WorkflowOptions(cwd=tmp, config=PluginConfig(), child_runner=runner))

        self.assertIn("META WINS", runner.requests[0].resolved.agent_type_spec.instructions)
        self.assertNotIn("FILE LOSES", runner.requests[0].resolved.agent_type_spec.instructions)

    def test_missing_agent_type_policy_loads_from_env(self):
        with patch.dict(
            os.environ,
            {"HERMES_DYNAMIC_WORKFLOWS_MISSING_AGENT_TYPE_POLICY": "fallback_warn"},
        ):
            self.assertEqual(load_config().missing_agent_type_policy, "fallback_warn")
        with patch.dict(
            os.environ,
            {"HERMES_DYNAMIC_WORKFLOWS_MISSING_AGENT_TYPE_POLICY": "bogus"},
        ):
            self.assertEqual(load_config().missing_agent_type_policy, "error")

    def test_missing_agent_type_fallback_warn_uses_generic_and_logs(self):
        script = """
meta = {"name": "missing-fallback", "description": "Test workflow"}

return await agent("go", {"agentType": "missing-reader", "label": "fallback", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        result = run_workflow(
            script,
            WorkflowOptions(
                config=PluginConfig(missing_agent_type_policy="fallback_warn"),
                child_runner=runner,
            ),
        )

        self.assertEqual(result.value, "fallback:go")
        self.assertEqual(runner.requests[0].agent_type, "general-purpose")
        self.assertTrue(any("missing-reader" in item and "falling back" in item for item in result.state.logs))

    def test_malformed_runtime_meta_agent_definitions_raise_before_launch(self):
        cases = [
            ("""meta = {"name":"bad","description":"bad","agents": []}
return await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})""", "meta.agents must be an object"),
            ("""meta = {"name":"bad","description":"bad","agents": {"../bad": {"instructions":"x"}}}
return await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})""", "invalid runtime agent name"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": []}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})""", "meta.agents.reader must be an object"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": {"instructions":"x", "toolsets": 12}}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})""", "toolsets must be"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": {"instructions":"x", "isolation": "bad"}}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})""", "isolation must be"),
        ]
        for script, message in cases:
            runner = FakeRunner()
            with self.subTest(message=message):
                with self.assertRaises(Exception) as ctx:
                    run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
                self.assertIn(message, str(ctx.exception))
                self.assertEqual(runner.requests, [])

    def test_inline_allowed_tools_empty_is_explicit_deny_all(self):
        script = """
meta = {"name": "empty-allow", "description": "Test workflow"}

return await agent("go", {"label": "deny", "allowedTools": [], "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests[0].resolved.allowed_tools, ())
        self.assertTrue(runner.requests[0].resolved.allowed_tools_explicit)

    def test_workflow_may_return_without_agent_call(self):
        script = """
meta = {"name": "empty", "description": "Test workflow"}

return "no agents"
"""
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))
        self.assertEqual(result.value, "no agents")
        self.assertEqual(result.agent_count, 0)

    def test_direct_agent_failure_raises(self):
        script = """
meta = {"name": "direct-failure", "description": "Test workflow"}

return await agent("fail", {"label": "direct", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with self.assertRaises(ChildAgentError) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=FailingRunner()))
        self.assertIn("failed:direct", str(ctx.exception))

    def test_pipeline_agent_failure_drops_item_and_skips_remaining_stages(self):
        script = """
meta = {"name": "pipeline-failure", "description": "Test workflow"}

return await pipeline(
    ["a", "b"],
    lambda item, original, index: agent(item, {"label": item, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
    lambda prior, original, index: agent("after-" + original, {"label": "after-" + original, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
)
"""

        class HalfFailingRunner(ChildAgentRunner):
            def __init__(self):
                self.labels = []

            def run(self, request):
                self.labels.append(request.label)
                if request.label == "a":
                    raise RuntimeError("no a")
                return request.label

        runner = HalfFailingRunner()
        result = run_workflow(script, WorkflowOptions(child_runner=runner))
        self.assertEqual(result.value, [None, "after-b"])
        self.assertEqual(result.error_count, 1)
        self.assertNotIn("after-a", runner.labels)

    def test_parallel_child_failure_is_counted_once(self):
        script = """
meta = {"name": "parallel-failure-count", "description": "Test workflow"}

return await parallel([
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}),
])
"""

        class HalfFailingRunner(ChildAgentRunner):
            def run(self, request):
                if request.label == "a":
                    raise RuntimeError("no a")
                return request.label

        result = run_workflow(script, WorkflowOptions(child_runner=HalfFailingRunner()))

        self.assertEqual(result.value, [None, "b"])
        self.assertEqual(result.error_count, 1)

    def test_intentionally_skipped_agent_returns_none(self):
        script = """
meta = {"name": "skip", "description": "Test workflow"}

return await agent("skip me", {"label": "skipped", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        result = run_workflow(script, WorkflowOptions(child_runner=SkippingRunner()))
        self.assertIsNone(result.value)
        agent_state = result.state.snapshot()["agents"][0]
        self.assertEqual(agent_state["status"], "skipped")
        self.assertEqual(agent_state["error"], "")

    def test_unknown_agent_type_raises_before_child_launch(self):
        script = """
meta = {"name": "missing-agent-type", "description": "Test workflow"}

return await agent("work", {"agentType": "definitely-missing", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=runner))
        self.assertIn(
            "agent({agentType}): agent type 'definitely-missing' not found",
            str(ctx.exception),
        )
        self.assertIn("Available agents:", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_agent_type_preset_routing_fields_rejected(self):
        script = """
meta = {"name": "preset-routing", "description": "Test workflow"}

return await agent("work", {
    "agentType": "planner",
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "xhigh",
    "maxTurns": 10,
})
"""
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".hermes" / "dynamic-workflows" / "agents"
            agent_dir.mkdir(parents=True)
            (agent_dir / "planner.md").write_text(
                "---\nname: planner\nmodel: inherit\nreasoning_effort: high\n---\n\nPlan carefully.\n",
                encoding="utf-8",
            )
            runner = FakeRunner()
            with self.assertRaises(Exception) as ctx:
                run_workflow(script, WorkflowOptions(cwd=tmp, child_runner=runner))

        self.assertIn("model is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_phase_model_is_rejected_before_child_launch(self):
        script = """
meta = {
    "name": "phase-model",
    "description": "Test workflow",
    "phases": [{"title": "Search", "model": "gpt-5.6-luna"}],
}
phase("Search")
return await agent("work", {
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "high",
    "maxTurns": 10,
})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=runner))

        self.assertIn("meta.phases[].model is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_agent_phase_option_does_not_supply_routing(self):
        script = """
meta = {
    "name": "opts-phase-model",
    "description": "Test workflow",
    "phases": [{"title": "Verify", "model": "gpt-5.6-luna"}],
}
return await agent("work", {
    "phase": "Verify",
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "high",
    "maxTurns": 10,
})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=runner))

        self.assertIn("meta.phases[].model is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_agent_model_does_not_override_phase_routing(self):
        script = """
meta = {
    "name": "explicit-model",
    "description": "Test workflow",
    "phases": [{"title": "Search", "model": "gpt-5.6-luna"}],
}
phase("Search")
return await agent("work", {
    "provider": "openai-codex",
    "model": "gpt-5.6-luna",
    "reasoningEffort": "high",
    "maxTurns": 10,
})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=runner))

        self.assertIn("meta.phases[].model is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_public_isolation_only_accepts_worktree(self):
        script = """
meta = {"name": "strict-isolation", "description": "Test workflow"}

return await agent("work", {"isolation": "shared", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=FakeRunner()))
        self.assertIn("isolation must be 'worktree'", str(ctx.exception))

    def test_log_requires_string(self):
        script = """
meta = {"name": "strict-log", "description": "Test workflow"}

log({"not": "text"})
return None
"""
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=FakeRunner()))
        self.assertIn("log() expects a string", str(ctx.exception))

    def test_removed_script_globals_are_unavailable(self):
        for name, script_line in (
            ("cwd", "return cwd"),
            ("print", 'print("no")'),
            ("set", "return set([1])"),
        ):
            with self.subTest(name=name):
                script = f'''
meta = {{"name": "no-{name}", "description": "Test workflow"}}

{script_line}
'''
                with self.assertRaises(NameError):
                    run_workflow(script, WorkflowOptions(child_runner=FakeRunner()))

    def test_workflow_helper_shares_global_agent_sequence_and_snapshot_tree(self):
        parent = """
meta = {"name": "parent", "description": "Test workflow", "phases": [{"title": "Root"}]}

phase("Root")
first = await agent("root", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
child = await workflow({"scriptPath": args["child"]})
last = await agent("after", {"label": "after", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return [first, child, last]
"""
        child = """
meta = {"name": "child", "description": "Test workflow", "phases": [{"title": "Child"}]}

phase("Child")
return await agent("child", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as tmp:
            child_path = Path(tmp) / "child.py"
            child_path.write_text(child, encoding="utf-8")
            runner = IdRunner()
            result = run_workflow(
                parent,
                WorkflowOptions(
                    args={"child": str(child_path)},
                    cwd=tmp,
                    config=PluginConfig(),
                    child_runner=runner,
                ),
            )

        self.assertEqual(result.value, ["1:root", "2:child", "3:after"])
        snapshot = result.state.snapshot()
        self.assertEqual(snapshot["agents"][0]["id"], 1)
        self.assertEqual(snapshot["children"][0]["agents"][0]["id"], 2)
        self.assertEqual(snapshot["agents"][1]["id"], 3)
        self.assertEqual(snapshot["totals"]["agents"], 3)
        self.assertEqual(
            snapshot["topologies"],
            [
                {"id": 1, "kind": "sequential", "status": "done", "steps": 1, "agent_ids": [1]},
                {"id": 2, "kind": "sequential", "status": "done", "steps": 1, "agent_ids": [3]},
            ],
        )

    def test_nested_workflow_tracks_its_own_sequential_topology_inside_parent_pipeline(self):
        parent = """
meta = {"name": "parent-pipeline", "description": "Test workflow"}

async def run_child(value, original, index):
    return await workflow({"scriptPath": args["child"]})

return await pipeline(["one"], run_child)
"""
        child = """
meta = {"name": "child-sequential", "description": "Test workflow"}

return await agent("child", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3})
"""
        with tempfile.TemporaryDirectory() as tmp:
            child_path = Path(tmp) / "child.py"
            child_path.write_text(child, encoding="utf-8")
            result = run_workflow(
                parent,
                WorkflowOptions(
                    args={"child": str(child_path)},
                    cwd=tmp,
                    config=PluginConfig(),
                    child_runner=FakeRunner(),
                ),
            )

        snapshot = result.state.snapshot()
        self.assertEqual(
            snapshot["topologies"],
            [{"id": 1, "kind": "pipeline", "status": "done", "items": 1, "stages": 1, "agent_ids": []}],
        )
        self.assertEqual(
            snapshot["children"][0]["topologies"],
            [{"id": 1, "kind": "sequential", "status": "done", "steps": 1, "agent_ids": [1]}],
        )

    def test_budget_is_token_budget(self):
        script = """
meta = {"name": "budget", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return {"total": budget.total, "spent": budget.spent(), "remaining": budget.remaining()}
"""
        result = run_workflow(
            script,
            WorkflowOptions(
                config=PluginConfig(),
                child_runner=TokenRunner(tokens=40),
                token_budget_total=100,
            ),
        )

        self.assertEqual(result.value, {"total": 100, "spent": 40, "remaining": 60})

    def test_failed_child_metadata_updates_record_and_spends_tokens_once(self):
        script = """
meta = {"name": "failed-child", "description": "Test workflow"}

try:
    await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
except Exception:
    return {"spent": budget.spent()}
return {"spent": budget.spent()}
"""
        result = run_workflow(
            script,
            WorkflowOptions(
                config=PluginConfig(),
                child_runner=FailedMetadataRunner(),
            ),
        )

        snapshot = result.state.snapshot()
        self.assertEqual(result.value, {"spent": 37})
        self.assertEqual(snapshot["agents"][0]["status"], "error")
        self.assertEqual(snapshot["agents"][0]["tokens"], 37)
        self.assertEqual(snapshot["agents"][0]["cache_read_tokens"], 3)
        self.assertEqual(snapshot["agents"][0]["cache_write_tokens"], 2)
        self.assertEqual(snapshot["totals"]["tokens"], 37)

    def test_token_budget_blocks_further_agents(self):
        script = """
meta = {"name": "budget-stop", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return await agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        # Budget exhaustion is a hard ceiling: it raises WorkflowLimitExceeded,
        # a WorkflowHalt (BaseException) a script's `except Exception` cannot
        # swallow — so it is NOT an `Exception` subclass.
        with self.assertRaises(WorkflowLimitExceeded):
            run_workflow(
                script,
                WorkflowOptions(
                    config=PluginConfig(),
                    child_runner=TokenRunner(tokens=20),
                    token_budget_total=10,
                ),
            )
        self.assertFalse(issubclass(WorkflowLimitExceeded, Exception))

    def test_meta_token_budget_is_ignored(self):
        script = """
meta = {"name": "budget-meta", "description": "Test workflow", "token_budget": 100}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return {"total": budget.total, "remaining": budget.remaining()}
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(), child_runner=TokenRunner(tokens=40)),
        )

        self.assertIsNone(result.value["total"])
        self.assertEqual(result.value["remaining"], float("inf"))

    def test_workflow_helper_nesting_respects_configured_single_level(self):
        # With max_nesting_depth=1, parent(0) -> child(1) is allowed but the
        # child calling workflow() again (depth 1 >= 1) raises.
        parent = """
meta = {"name": "parent", "description": "Test workflow"}

return await workflow({"scriptPath": args["child"]}, args)
"""
        child = """
meta = {"name": "child", "description": "Test workflow"}

return await workflow({"scriptPath": args["grand"]})
"""
        grand = """
meta = {"name": "grand", "description": "Test workflow"}

return await agent("grand", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as tmp:
            child_path = Path(tmp) / "child.py"
            grand_path = Path(tmp) / "grand.py"
            child_path.write_text(child, encoding="utf-8")
            grand_path.write_text(grand, encoding="utf-8")
            with self.assertRaises(Exception):
                run_workflow(
                    parent,
                    WorkflowOptions(
                        args={"child": str(child_path), "grand": str(grand_path)},
                        cwd=tmp,
                        config=PluginConfig(max_nesting_depth=1),
                        child_runner=FakeRunner(),
                    ),
                )

    def test_workflow_helper_rejects_inline_script_reference(self):
        script = """
meta = {"name": "strict-nested-ref", "description": "Test workflow"}

return await workflow({"script": "meta = {}"})
"""
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=FakeRunner()))
        self.assertIn("workflow() expects a non-empty workflow name or", str(ctx.exception))

    def test_named_nested_workflow_uses_parent_store(self):
        parent = """
meta = {"name": "parent-store", "description": "Test workflow"}

return await workflow("private-child")
"""
        child = """
meta = {"name": "private-child", "description": "Test workflow"}

return await agent("child", {"label": "private-child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp) / "custom-store")
            (store.workflows_dir / "private-child.py").write_text(child, encoding="utf-8")
            runner = FakeRunner()
            result = run_workflow(
                parent,
                WorkflowOptions(
                    cwd=tmp,
                    child_runner=runner,
                    store=store,
                ),
            )
        self.assertEqual(result.value, "private-child:child")

    def test_unknown_nested_workflow_reports_available_names(self):
        script = """
meta = {"name": "unknown-child", "description": "Test workflow"}

return await workflow("missing-child")
"""
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp) / "custom-store")
            with self.assertRaises(Exception) as ctx:
                run_workflow(
                    script,
                    WorkflowOptions(
                        cwd=tmp,
                        child_runner=FakeRunner(),
                        store=store,
                    ),
                )

        self.assertIn(
            "workflow('missing-child'): no workflow with that name. Available: none",
            str(ctx.exception),
        )

    def test_resume_cache_ignores_label_and_phase(self):
        first_script = """
meta = {"name": "cache-display-one", "description": "Test workflow"}

return await agent("same prompt", {"label": "first", "phase": "One", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        second_script = """
meta = {"name": "cache-display-two", "description": "Test workflow"}

return await agent("same prompt", {"label": "second", "phase": "Two", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        first_runner = FakeRunner()
        first_cache = ResumeCache()
        first = run_workflow(
            first_script,
            WorkflowOptions(child_runner=first_runner, resume_cache=first_cache),
        )
        second_runner = FakeRunner()
        second = run_workflow(
            second_script,
            WorkflowOptions(
                child_runner=second_runner,
                resume_cache=ResumeCache(first_cache.current),
            ),
        )
        self.assertEqual(second.value, first.value)
        self.assertEqual(second_runner.requests, [])

    def test_resume_cache_invalidates_when_agent_type_content_changes(self):
        script = """
meta = {"name": "cache-agent-type", "description": "Test workflow"}

return await agent("same prompt", {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / ".hermes" / "dynamic-workflows" / "agents"
            agent_dir.mkdir(parents=True)
            agent_file = agent_dir / "researcher.md"
            agent_file.write_text(
                "---\nname: researcher\n---\nVersion one.\n",
                encoding="utf-8",
            )
            first_cache = ResumeCache()
            run_workflow(
                script,
                WorkflowOptions(
                    cwd=tmp,
                    child_runner=FakeRunner(),
                    resume_cache=first_cache,
                ),
            )
            agent_file.write_text(
                "---\nname: researcher\n---\nVersion two.\n",
                encoding="utf-8",
            )
            second_runner = FakeRunner()
            run_workflow(
                script,
                WorkflowOptions(
                    cwd=tmp,
                    child_runner=second_runner,
                    resume_cache=ResumeCache(first_cache.current),
                ),
            )
        self.assertEqual(len(second_runner.requests), 1)

    def test_resume_cache_does_not_cross_workspaces(self):
        script = """
meta = {"name": "cache-workspace", "description": "Test workflow"}

return await agent("same prompt", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        with tempfile.TemporaryDirectory() as first_cwd, tempfile.TemporaryDirectory() as second_cwd:
            first_cache = ResumeCache()
            run_workflow(
                script,
                WorkflowOptions(
                    cwd=first_cwd,
                    child_runner=FakeRunner(),
                    resume_cache=first_cache,
                ),
            )
            second_runner = FakeRunner()
            run_workflow(
                script,
                WorkflowOptions(
                    cwd=second_cwd,
                    child_runner=second_runner,
                    resume_cache=ResumeCache(first_cache.current),
                ),
            )
        self.assertEqual(len(second_runner.requests), 1)


class ReasoningEffortRuntimeTests(unittest.TestCase):
    def test_luna_high_fails_before_child_launch(self):
        script = """
meta = {"name": "luna-high", "description": "Test workflow"}
return await agent("go", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 10})
"""
        runner = FakeRunner()

        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests, [])
        self.assertIn("gpt-5.6-luna reasoningEffort must be xhigh or max", str(ctx.exception))

    def test_inline_effort_overrides_runtime_preset_and_is_recorded(self):
        script = """
meta = {
    "name": "reasoning-inline",
    "description": "Test workflow",
    "agents": {
        "researcher": {
            "instructions": "Research.",
        }
    },
}
return await agent(
    "go",
    {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "xhigh", "maxTurns": 10},
)
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        request = runner.requests[0]
        self.assertEqual(request.reasoning_effort, "xhigh")
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertEqual(request.resolved.reasoning_effort, "xhigh")
        self.assertEqual(result.state.snapshot()["agents"][0]["reasoning_effort"], "xhigh")

    def test_runtime_preset_effort_is_rejected(self):
        script = """
meta = {
    "name": "reasoning-preset",
    "description": "Test workflow",
    "agents": {
        "researcher": {
            "instructions": "Research.",
            "reasoningEffort": "medium",
        }
    },
}
return await agent("go", {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
        self.assertIn("reasoningEffort is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])

    def test_missing_effort_fails_before_child_launch(self):
        script = """
meta = {
    "name": "reasoning-missing",
    "description": "Test workflow",
    "agents": {"researcher": {"instructions": "Research."}},
}
return await agent("go", {"agentType": "researcher", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "maxTurns": 10})
"""
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(runner.requests, [])
        self.assertIn("reasoningEffort is required", str(ctx.exception))
        self.assertIn("agent() reasoningEffort is required", str(ctx.exception))

    def test_invalid_inline_efforts_fail_before_child_launch(self):
        for value in (None, True, False, "", "none", "HIGH", "minimal ", 1, []):
            script = (
                'meta = {"name": "reasoning-invalid", "description": "Test workflow"}\n'
                f'return await agent("go", {{"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": {value!r}, "maxTurns": 10}})'
            )
            runner = FakeRunner()
            with self.subTest(value=value), self.assertRaises(Exception) as ctx:
                run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
            self.assertEqual(runner.requests, [])
            self.assertIn("agent() reasoningEffort must be one of", str(ctx.exception))

    def test_invalid_runtime_preset_efforts_fail_before_child_launch(self):
        for value in (None, True, False, "", "none", "HIGH", "minimal ", 1, []):
            meta = {
                "name": "reasoning-invalid-preset",
                "description": "Test workflow",
                "agents": {
                    "researcher": {
                        "instructions": "Research.",
                        "reasoningEffort": value,
                    }
                },
            }
            script = f"meta = {meta!r}\nreturn await agent('go', {{'agentType': 'researcher', 'provider': 'openai-codex', 'model': 'gpt-5.6-luna', 'reasoningEffort': 'medium', 'maxTurns': 10}})"
            runner = FakeRunner()
            with self.subTest(value=value), self.assertRaises(Exception) as ctx:
                run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
            self.assertEqual(runner.requests, [])
            self.assertIn(
                "meta.agents.researcher reasoningEffort is not supported",
                str(ctx.exception),
            )

    @patch("hermes_dynamic_workflows.child.runner._discoverable_child_toolsets", return_value=[])
    def test_effort_changes_cache_identity_and_survives_cache_hit(self, _toolsets):
        def script(effort: str) -> str:
            return (
                'meta = {"name": "reasoning-cache", "description": "Test workflow"}\n'
                f'return await agent("same prompt", {{"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "{effort}", "maxTurns": 10}})'
            )

        low_cache = ResumeCache()
        run_workflow(
            script("low"),
            WorkflowOptions(child_runner=FakeRunner(), resume_cache=low_cache),
        )

        xhigh_runner = FakeRunner()
        run_workflow(
            script("xhigh"),
            WorkflowOptions(
                child_runner=xhigh_runner,
                resume_cache=ResumeCache(low_cache.current),
            ),
        )
        self.assertEqual(len(xhigh_runner.requests), 1)

        cached_runner = FakeRunner()
        cached = run_workflow(
            script("low"),
            WorkflowOptions(
                child_runner=cached_runner,
                resume_cache=ResumeCache(low_cache.current),
            ),
        )
        self.assertEqual(cached_runner.requests, [])
        self.assertEqual(cached.state.snapshot()["agents"][0]["reasoning_effort"], "low")


class MaxTurnsRuntimeTests(unittest.TestCase):
    _base = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "maxTurns": 10,
    }

    def _script(self, options):
        return (
            'meta = {"name": "turn-contract", "description": "Test workflow"}\n'
            f"return await agent(\"go\", {options!r})"
        )

    def test_plugin_max_turns_default_is_150(self):
        self.assertEqual(PluginConfig().max_turns, 150)

    def test_max_turns_env_override_is_clamped_to_workflow_range(self):
        for raw, expected in (("275", 275), ("0", 1), ("1001", 1000), ("not-an-int", 150)):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {"HERMES_DYNAMIC_WORKFLOWS_MAX_TURNS": raw},
            ):
                self.assertEqual(load_config().max_turns, expected)

    def test_omitted_inline_max_turns_uses_config_default(self):
        options = dict(self._base)
        options.pop("maxTurns")
        runner = FakeRunner()
        run_workflow(
            self._script(options),
            WorkflowOptions(config=PluginConfig(), child_runner=runner),
        )
        self.assertEqual(runner.requests[0].max_turns, 150)

    def test_omitted_inline_max_turns_uses_config_override(self):
        options = dict(self._base)
        options.pop("maxTurns")
        runner = FakeRunner()
        run_workflow(
            self._script(options),
            WorkflowOptions(config=PluginConfig(max_turns=42), child_runner=runner),
        )
        self.assertEqual(runner.requests[0].max_turns, 42)

    def test_inline_max_turns_overrides_config_default(self):
        runner = FakeRunner()
        run_workflow(
            self._script({**self._base, "maxTurns": 7}),
            WorkflowOptions(config=PluginConfig(max_turns=42), child_runner=runner),
        )
        self.assertEqual(runner.requests[0].max_turns, 7)

    def test_invalid_inline_max_turns_fails_before_launch(self):
        for value in (None, True, 1.5, "2", 0, -1, 1001):
            runner = FakeRunner()
            with self.subTest(value=value), self.assertRaises(Exception) as ctx:
                run_workflow(
                    self._script({**self._base, "maxTurns": value}),
                    WorkflowOptions(config=PluginConfig(), child_runner=runner),
                )
            self.assertIn("agent() maxTurns must be an integer", str(ctx.exception))
            self.assertEqual(runner.requests, [])

    def test_preset_max_turns_are_rejected(self):
        script = '''meta = {
    "name": "preset-turns",
    "description": "Test workflow",
    "agents": {"researcher": {"instructions": "Research.", "maxTurns": 10}},
}
return await agent("go", {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
'''
        runner = FakeRunner()
        with self.assertRaises(Exception) as ctx:
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
        self.assertIn("maxTurns is not supported", str(ctx.exception))
        self.assertEqual(runner.requests, [])


class ResumeCacheTests(unittest.TestCase):
    def test_content_addressed_fifo_for_duplicate_fingerprints(self):
        fp = agent_fingerprint("same prompt", {"label": "x"})
        run1 = ResumeCache()
        run1.put(fp, "r1")
        run1.put(fp, "r2")

        run2 = ResumeCache(run1.current)
        # Two identical calls each consume one cached result (FIFO), then miss.
        self.assertEqual(run2.get(fp), "r1")
        self.assertEqual(run2.get(fp), "r2")
        self.assertTrue(is_cache_miss(run2.get(fp)))

    def test_ignores_malformed_cache_without_crashing(self):
        fp = agent_fingerprint("p", {"label": "y"})
        # Unexpected shapes (e.g. a crashed/hand-edited run) are ignored -> miss.
        cache = ResumeCache({fp: {"not": "a list"}, "other": 123})
        self.assertTrue(is_cache_miss(cache.get(fp)))


class ControlFlowRuntimeTests(unittest.TestCase):
    def test_while_loop_runs_end_to_end(self):
        script = """
meta = {"name": "while-ok", "description": "Test workflow"}

results = []
i = 0
while i < 3:
    results.append(await agent("x" + str(i), {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}))
    i = i + 1
return results
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(child_runner=runner))
        self.assertEqual(len(result.value), 3)
        self.assertEqual([r.prompt for r in runner.requests], ["x0", "x1", "x2"])

    def test_try_except_handles_recoverable_error(self):
        script = """
meta = {"name": "try-ok", "description": "Test workflow"}

try:
    y = 1 / 0
except Exception:
    y = "caught"
await agent("a", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return y
"""
        result = run_workflow(script, WorkflowOptions(child_runner=TokenRunner(tokens=1)))
        self.assertEqual(result.value, "caught")

    def test_except_exception_cannot_swallow_budget_halt(self):
        # A while loop that catches Exception around agent() must STILL halt when
        # the token budget is exhausted — the halt is BaseException, not caught.
        script = """
meta = {"name": "no-swallow", "description": "Test workflow"}

out = []
while True:
    try:
        out.append(await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10}))
    except Exception:
        out.append("swallowed")
return out
"""
        with self.assertRaises(WorkflowLimitExceeded):
            run_workflow(
                script,
                WorkflowOptions(
                    config=PluginConfig(),
                    child_runner=TokenRunner(tokens=20),
                    token_budget_total=10,
                ),
            )

    def test_compute_only_loop_is_bounded_by_iteration_cap(self):
        # A pure-compute infinite loop (never calls agent()) is bounded by the
        # injected loop guard's iteration cap — proving the deadline/stop check
        # actually fires inside such a loop.
        script = """
meta = {"name": "spin", "description": "Test workflow"}

await agent("a", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
while True:
    pass
return 1
"""
        with self.assertRaises(WorkflowLimitExceeded):
            run_workflow(
                script,
                WorkflowOptions(
                    config=PluginConfig(max_loop_iterations=100),
                    child_runner=TokenRunner(tokens=1),
                ),
            )

    def test_compute_only_for_loop_is_bounded_by_iteration_cap(self):
        script = """
meta = {"name": "for-spin", "description": "Test workflow"}

for i in range(1000000):
    value = i
return value
"""
        with self.assertRaises(WorkflowLimitExceeded):
            run_workflow(
                script,
                WorkflowOptions(
                    config=PluginConfig(max_loop_iterations=100),
                    child_runner=TokenRunner(tokens=1),
                ),
            )


class NestingDepthTests(unittest.TestCase):
    """workflow() nesting depth is config-driven and run-wide caps bind across frames."""

    @staticmethod
    def _write_chain(tmp: str) -> dict[str, str]:
        # grandchild: depth 2 when reached via root -> child -> grandchild.
        grandchild = """
meta = {"name": "gc", "description": "grandchild"}

return await agent("gc-work", {"label": "gc", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
"""
        child = """
meta = {"name": "child", "description": "child"}

inner = await workflow({"scriptPath": args["grandchild"]}, args)
mine = await agent("child-work", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
return [inner, mine]
"""
        root = """
meta = {"name": "root", "description": "root"}

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
nested = await workflow({"scriptPath": args["child"]}, args)
return [mine, nested]
"""
        gc_path = Path(tmp) / "gc.py"
        child_path = Path(tmp) / "child.py"
        gc_path.write_text(grandchild, encoding="utf-8")
        child_path.write_text(child, encoding="utf-8")
        return {"grandchild": str(gc_path), "child": str(child_path)}

    def test_nesting_allowed_to_configured_depth(self):
        # Default max_nesting_depth=2 permits root -> child -> grandchild.
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_chain(tmp)
            runner = FakeRunner()
            result = run_workflow(
                """
meta = {"name": "root", "description": "root"}

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
nested = await workflow({"scriptPath": args["child"]}, args)
return [mine, nested]
""",
                WorkflowOptions(
                    args={"child": paths["child"], "grandchild": paths["grandchild"]},
                    cwd=tmp,
                    config=PluginConfig(max_nesting_depth=2),
                    child_runner=runner,
                ),
            )
        # root agent + child's grandchild result + child agent all resolved.
        self.assertEqual(result.value, ["root:root-work", ["gc:gc-work", "child:child-work"]])
        self.assertEqual(result.agent_count, 3)

    def test_nesting_rejected_past_max_depth(self):
        # A workflow() call from the grandchild (depth 2) exceeds the default
        # max_nesting_depth=2 and raises, surfacing as a child-agent failure up
        # the chain.
        deep = """
meta = {"name": "too-deep", "description": "depth-3 attempt"}

return await workflow({"scriptPath": args["self"]}, args)
"""
        with tempfile.TemporaryDirectory() as tmp:
            self_path = Path(tmp) / "deep.py"
            self_path.write_text(deep, encoding="utf-8")
            root = """
meta = {"name": "root", "description": "root"}

a = await workflow({"scriptPath": args["self"]}, args)
b = await workflow({"scriptPath": args["self"]}, args)
return [a, b]
"""
            # root(0) -> deep(1) -> deep(2) -> deep tries workflow() at depth 2 -> raise.
            with self.assertRaises(WorkflowRuntimeError) as ctx:
                run_workflow(
                    root,
                    WorkflowOptions(
                        args={"self": str(self_path)},
                        cwd=tmp,
                        config=PluginConfig(max_nesting_depth=2),
                        child_runner=FakeRunner(),
                    ),
                )
        self.assertIn("nested workflows are limited to 2 levels deep", str(ctx.exception))

    def test_depth_one_reproduces_single_level_limit(self):
        # max_nesting_depth=1 is the original behavior: the child (depth 1)
        # cannot call workflow() again.
        child = """
meta = {"name": "child", "description": "child"}

return await workflow({"scriptPath": args["child"]}, args)
"""
        with tempfile.TemporaryDirectory() as tmp:
            child_path = Path(tmp) / "child.py"
            child_path.write_text(child, encoding="utf-8")
            root = """
meta = {"name": "root", "description": "root"}

return await workflow({"scriptPath": args["child"]}, args)
"""
            with self.assertRaises(WorkflowRuntimeError) as ctx:
                run_workflow(
                    root,
                    WorkflowOptions(
                        args={"child": str(child_path)},
                        cwd=tmp,
                        config=PluginConfig(max_nesting_depth=1),
                        child_runner=FakeRunner(),
                    ),
                )
        self.assertIn("nested workflows are limited to 1 level deep", str(ctx.exception))

    def test_run_wide_agent_cap_binds_across_nested_frames(self):
        # SAFETY REGRESSION: the run-wide agent cap is enforced on a SHARED
        # counter across every nesting level. root(1 agent) -> child(1 agent) ->
        # grandchild tries a 3rd agent and trips max_agents=2, proving deeper
        # nesting cannot escape the run-wide ceiling.
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_chain(tmp)
            with self.assertRaises(WorkflowLimitExceeded) as ctx:
                run_workflow(
                    """
meta = {"name": "root", "description": "root"}

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10})
nested = await workflow({"scriptPath": args["child"]}, args)
return [mine, nested]
""",
                    WorkflowOptions(
                        args={"child": paths["child"], "grandchild": paths["grandchild"]},
                        cwd=tmp,
                        config=PluginConfig(max_agents=2, max_nesting_depth=2),
                        child_runner=FakeRunner(),
                    ),
                )
        self.assertIn("agent count exceeded (2)", str(ctx.exception))


class DurableAgentHandleTests(unittest.TestCase):
    _opts = {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "maxTurns": 10,
    }

    def test_handle_snapshot_round_trip_uses_wall_clock_time(self):
        record = AgentHandleRecord(handle="ah:h:1", lineage_id="h")
        snapshot = record.snapshot()
        restored = AgentHandleRecord.from_snapshot(snapshot)

        self.assertIsNotNone(restored)
        self.assertGreater(snapshot["created_at"], 1_000_000_000)
        self.assertEqual(restored.snapshot(), snapshot)

    def test_resume_rehydrates_terminal_handle_in_same_lineage(self):
        record = AgentHandleRecord(
            handle="ah:lineage-test:terminal",
            lineage_id="lineage-test",
            state="completed",
            result="persisted",
        )
        api = _durable_api(
            DurableHandleRunner(),
            agent_handles={record.handle: record.snapshot()},
            handle_lineage_id=record.lineage_id,
        )

        self.assertEqual(api.agent_status(record.handle)["status"], "completed")
        self.assertEqual(asyncio.run(api.wait_agent(record.handle)), "persisted")

    def test_resume_marks_active_handle_interrupted_without_replay(self):
        record = AgentHandleRecord(
            handle="ah:lineage-test:active",
            lineage_id="lineage-test",
            state="running",
            session_id="session-active",
        )
        runner = DurableHandleRunner()
        api = _durable_api(
            runner,
            agent_handles={record.handle: record.snapshot()},
            handle_lineage_id=record.lineage_id,
        )

        snapshot = api.agent_status(record.handle)
        self.assertEqual(snapshot["status"], "interrupted")
        self.assertIn("process/restart interrupted the turn", snapshot["error"])
        self.assertEqual(runner.calls, 0)

    def test_wait_restored_interrupted_handle_fails_without_hanging(self):
        record = AgentHandleRecord(
            handle="ah:lineage-test:interrupted",
            lineage_id="lineage-test",
            state="interrupted",
            error="process/restart interrupted the turn",
        )
        api = _durable_api(
            DurableHandleRunner(),
            agent_handles={record.handle: record.snapshot()},
            handle_lineage_id=record.lineage_id,
        )

        with self.assertRaisesRegex(WorkflowRuntimeError, "interrupted"):
            asyncio.run(api.wait_agent(record.handle))

    def test_continue_restored_handle_uses_stable_session_history(self):
        history = [{"role": "assistant", "content": "stable"}]
        runner = DurableHandleRunner()
        with tempfile.TemporaryDirectory() as workspace:
            record = AgentHandleRecord(
                handle="ah:lineage-test:continue",
                lineage_id="lineage-test",
                state="interrupted",
                session_id="session-restored",
                workspace=workspace,
                workspace_ownership="workflow-owned",
                route={**self._opts, "isolation": "worktree"},
            )
            api = _durable_api(
                runner,
                cwd=workspace,
                agent_handles={record.handle: record.snapshot()},
                handle_lineage_id=record.lineage_id,
            )

            async def scenario():
                with patch(
                    "hermes_dynamic_workflows.engine.api.SessionTranscriptReader.stable_messages",
                    return_value=history,
                ):
                    handle = await api.continue_agent(record.handle, "continue")
                    await api.wait_agent(handle)

            asyncio.run(scenario())

        self.assertEqual(runner.requests[0].session_id, "session-restored")
        self.assertIs(runner.requests[0].conversation_history, history)
        self.assertEqual(runner.requests[0].cwd, workspace)
        self.assertIsNone(runner.requests[0].isolation)

    def test_stop_running_handle_stays_stopping_until_worker_exits(self):
        runner = InterruptibleHandleRunner(acknowledge=False)
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("work", self._opts)
            self.assertTrue(await asyncio.to_thread(runner.started.wait, 1))
            await api.stop_agent(handle)
            self.assertEqual(api.agent_status(handle)["status"], "stopping")
            self.assertFalse(api.context.handle_futures[handle].done())
            runner.release.set()
            with self.assertRaises(WorkflowRuntimeError):
                await api.wait_agent(handle)
            return handle

        handle = asyncio.run(scenario())
        self.assertEqual(runner.interrupt_ids, [handle])

    def test_stop_acknowledged_handle_becomes_stopped(self):
        runner = InterruptibleHandleRunner(acknowledge=True)
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("work", self._opts)
            self.assertTrue(await asyncio.to_thread(runner.started.wait, 1))
            await api.stop_agent(handle)
            with self.assertRaisesRegex(WorkflowRuntimeError, "stopped"):
                await api.wait_agent(handle)
            return handle

        handle = asyncio.run(scenario())
        self.assertEqual(api.agent_status(handle)["status"], "stopped")
        self.assertEqual(runner.interrupt_ids, [handle])

    def test_stop_unacknowledged_handle_preserves_future_and_workspace(self):
        runner = InterruptibleHandleRunner(acknowledge=False)
        with tempfile.TemporaryDirectory() as workspace:
            api = _durable_api(runner, cwd=workspace)

            async def scenario():
                handle = await api.start_agent("work", self._opts)
                self.assertTrue(await asyncio.to_thread(runner.started.wait, 1))
                future = api.context.handle_futures[handle]
                await api.stop_agent(handle)
                self.assertIs(api.context.handle_futures[handle], future)
                self.assertEqual(api.agent_status(handle)["status"], "stopping")
                self.assertEqual(api.agent_status(handle)["workspace"], workspace)
                runner.release.set()
                with self.assertRaises(WorkflowRuntimeError):
                    await api.wait_agent(handle)

            asyncio.run(scenario())

    def test_runner_targeted_interrupt_uses_persisted_handle_task_id(self):
        runner = InterruptibleHandleRunner(acknowledge=False)
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("work", self._opts)
            self.assertTrue(await asyncio.to_thread(runner.started.wait, 1))
            await api.stop_agent(handle)
            runner.release.set()
            with self.assertRaises(WorkflowRuntimeError):
                await api.wait_agent(handle)
            return handle

        handle = asyncio.run(scenario())
        self.assertEqual(runner.requests[0].handle_id, handle)
        self.assertEqual(runner.interrupt_ids, [handle])

    def test_continue_reuses_workflow_owned_workspace_without_new_worktree(self):
        runner = DurableHandleRunner()
        with tempfile.TemporaryDirectory() as workspace:
            record = AgentHandleRecord(
                handle="ah:lineage-test:owned",
                lineage_id="lineage-test",
                state="completed",
                session_id="session-owned",
                workspace=workspace,
                workspace_ownership="workflow-owned",
                route={**self._opts, "isolation": "worktree"},
            )
            api = _durable_api(
                runner,
                cwd=workspace,
                agent_handles={record.handle: record.snapshot()},
                handle_lineage_id=record.lineage_id,
            )

            async def scenario():
                with patch(
                    "hermes_dynamic_workflows.engine.api.SessionTranscriptReader.stable_messages",
                    return_value=[],
                ):
                    handle = await api.continue_agent(record.handle, "next")
                    await api.wait_agent(handle)

            asyncio.run(scenario())

        self.assertEqual(runner.requests[0].cwd, workspace)
        self.assertIsNone(runner.requests[0].isolation)

    def test_stop_never_cleans_borrowed_workspace(self):
        runner = InterruptibleHandleRunner(acknowledge=True)
        with tempfile.TemporaryDirectory() as workspace:
            api = _durable_api(runner, cwd=workspace)

            async def scenario():
                handle = await api.start_agent("work", self._opts)
                self.assertTrue(await asyncio.to_thread(runner.started.wait, 1))
                await api.stop_agent(handle)
                with self.assertRaises(WorkflowRuntimeError):
                    await api.wait_agent(handle)

            asyncio.run(scenario())
            self.assertTrue(Path(workspace).is_dir())

    def test_start_agent_returns_lineage_scoped_handle_before_completion(self):
        runner = DurableHandleRunner(block=True)
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("work", self._opts)
            snapshot = api.agent_status(handle)
            self.assertIsInstance(handle, str)
            self.assertEqual(snapshot["status"], "queued")
            self.assertEqual(snapshot["lineage_id"], api.context.handle_lineage_id)
            runner.release.set()
            return handle

        handle = asyncio.run(scenario())
        self.assertIsInstance(handle, str)

    def test_agent_is_start_then_wait(self):
        runner = DurableHandleRunner()
        api = _durable_api(runner)

        async def scenario():
            return await api.agent("work", self._opts)

        self.assertEqual(asyncio.run(scenario()), "result:work")
        self.assertEqual(runner.calls, 1)

    def test_wait_agent_returns_completed_result(self):
        runner = DurableHandleRunner()
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("work", self._opts)
            result = await api.wait_agent(handle)
            self.assertEqual(api.agent_status(handle)["status"], "completed")
            return result

        self.assertEqual(asyncio.run(scenario()), "result:work")

    def test_agent_status_rejects_unknown_or_cross_lineage_handle(self):
        api_one = _durable_api(DurableHandleRunner())
        api_two = _durable_api(DurableHandleRunner())

        async def scenario():
            return await api_one.start_agent("work", self._opts)

        handle = asyncio.run(scenario())
        with self.assertRaisesRegex(WorkflowRuntimeError, "unknown or cross-lineage"):
            api_one.agent_status("agent-handle:unknown")
        with self.assertRaisesRegex(WorkflowRuntimeError, "unknown or cross-lineage"):
            api_two.agent_status(handle)

    def test_continue_requires_prompt_and_rejects_running_handle_before_reservation(self):
        runner = DurableHandleRunner(block=True)
        api = _durable_api(runner)

        async def scenario():
            handle = await api.start_agent("first", self._opts)
            with self.assertRaisesRegex(WorkflowRuntimeError, "non-empty prompt"):
                await api.continue_agent(handle, " ")
            await asyncio.sleep(0)
            self.assertTrue(runner.started.wait(timeout=1))
            with self.assertRaisesRegex(WorkflowRuntimeError, "queued or running"):
                await api.continue_agent(handle, "second")
            self.assertEqual(api.context.agent_count, 1)
            runner.release.set()
            await api.wait_agent(handle)

        asyncio.run(scenario())

    def test_continue_reuses_handle_session_history_and_workspace(self):
        runner = DurableHandleRunner()
        history = [{"role": "assistant", "content": "stable"}]

        with tempfile.TemporaryDirectory() as workspace:
            api = _durable_api(runner, cwd=workspace)

            async def scenario():
                handle = await api.start_agent("first", self._opts)
                await api.wait_agent(handle)
                with patch(
                    "hermes_dynamic_workflows.engine.api.SessionTranscriptReader.stable_messages",
                    return_value=history,
                ):
                    same = await api.continue_agent(handle, "second")
                    self.assertEqual(same, handle)
                    await api.wait_agent(handle)

            asyncio.run(scenario())
        self.assertEqual(runner.requests[1].session_id, runner.requests[0].session_id)
        self.assertIs(runner.requests[1].conversation_history, history)
        self.assertEqual(runner.requests[1].cwd, workspace)

    def test_fork_creates_new_handle_session_from_stable_history_without_workspace_clone(self):
        runner = DurableHandleRunner()
        history = [{"role": "user", "content": "source"}]

        with tempfile.TemporaryDirectory() as source_workspace, tempfile.TemporaryDirectory() as fork_workspace:
            api = _durable_api(runner, cwd=source_workspace)

            async def scenario():
                source = await api.start_agent("source", self._opts)
                await api.wait_agent(source)
                api.frame.cwd = fork_workspace
                with patch(
                    "hermes_dynamic_workflows.engine.api.SessionTranscriptReader.stable_messages",
                    return_value=history,
                ):
                    fork = await api.fork_agent(source, "fork", self._opts)
                    await api.wait_agent(fork)
                return source, fork

            source, fork = asyncio.run(scenario())
        self.assertNotEqual(source, fork)
        self.assertNotEqual(runner.requests[1].session_id, runner.requests[0].session_id)
        self.assertIs(runner.requests[1].conversation_history, history)
        self.assertEqual(runner.requests[1].parent_session_id, runner.requests[0].session_id)
        self.assertEqual(runner.requests[1].parent_handle_id, source)
        self.assertEqual(runner.requests[1].cwd, fork_workspace)
        self.assertNotEqual(runner.requests[1].cwd, source_workspace)


if __name__ == "__main__":
    unittest.main()
