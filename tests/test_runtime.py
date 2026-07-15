from __future__ import annotations

import os
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from hermes_dynamic_workflows.engine.cache import ResumeCache, agent_fingerprint, is_cache_miss
from hermes_dynamic_workflows.core.config import PluginConfig, load_config
from hermes_dynamic_workflows.core.errors import (
    ChildAgentError,
    ChildAgentSkipped,
    WorkflowLimitExceeded,
    WorkflowParseError,
    WorkflowRuntimeError,
)
from hermes_dynamic_workflows.engine.runtime import WorkflowOptions, run_workflow
from hermes_dynamic_workflows.core.types import ChildAgentRequest, ChildAgentResult, ChildAgentRunner
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
            request.on_start({"task_id": "workflow-live", "session_id": "workflow-live"})
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


class SkippingRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        raise ChildAgentSkipped("skipped by user")


class RuntimeTests(unittest.TestCase):
    def test_live_child_updates_refresh_snapshot_and_journal(self):
        events = []
        result = run_workflow(
            'meta = {"name": "live", "description": "live"}\nreturn await agent("work", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})',
            WorkflowOptions(
                config=PluginConfig(),
                child_runner=LiveUpdateRunner(),
                on_journal=events.append,
            ),
        )

        agent = result.state.snapshot()["agents"][0]
        self.assertEqual(agent["tokens"], 321)
        self.assertEqual(agent["tool_calls"], 2)
        self.assertEqual([event["type"] for event in events], ["started", "activity", "result"])
        self.assertIn("pwd", events[1]["activity"])

    def test_runs_strict_async_script_body(self):
        script = """
meta = {"name": "simple", "description": "Test workflow", "phases": ["scan"]}

phase("scan")
return await agent("inspect repo", {"label": "scan-agent", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
return await agent("inspect repo", {"label": "top-agent", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        self.assertEqual(result.value, "top-agent:inspect repo")
        self.assertEqual(result.agent_count, 1)
        self.assertEqual(runner.requests[0].label, "top-agent")
        self.assertEqual(result.state.current_phase, "scan")

    def test_parallel_preserves_order(self):
        script = """
meta = {"name": "parallel", "description": "Test workflow"}

return await parallel([
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda: agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
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
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000}),
    lambda: agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000}),
])
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(concurrency=3), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "parallel", "status": "done", "lanes": 3}],
        )

    def test_pipeline_records_items_and_stages_without_counting_inner_agents_as_sequential(self):
        script = """
meta = {"name": "pipeline-topology", "description": "Test workflow"}

async def inspect(value, original, index):
    return await agent("inspect " + value, {"label": "inspect:" + value, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})

async def verify(value, original, index):
    return await agent("verify " + value, {"label": "verify:" + original, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})

return await pipeline(["a", "b"], inspect, verify)
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(concurrency=4), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "pipeline", "status": "done", "items": 2, "stages": 2}],
        )

    def test_direct_agents_record_observed_sequential_steps(self):
        script = """
meta = {"name": "sequential-topology", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})
await agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})
return await agent("c", {"label": "c", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})
"""
        result = run_workflow(
            script,
            WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()),
        )

        self.assertEqual(
            result.state.snapshot()["topologies"],
            [{"id": 1, "kind": "sequential", "status": "done", "steps": 3}],
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
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000, "schema": {"type": "object", "required": ["ok"]}},
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
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000, "schema": {"type": "object", "required": ["ok"]}},
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
    {"label": "json", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000, "schema": {"type": 123}},
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
        self.assertIn("maxToolCalls", str(ctx.exception))
        self.assertIn("maxToolOutputChars", str(ctx.exception))
        self.assertNotIn("maxToolCalls, maxToolOutputChars, and retry policy belong", str(ctx.exception))

    def test_agent_rejects_auto_provider(self):
        script = """
meta = {"name": "auto-provider", "description": "Test workflow"}
return await agent("go", {"provider": "auto", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        with self.assertRaisesRegex(Exception, "provider must be explicit"):
            run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=FakeRunner()))

    def test_agent_rejects_configured_model_alias(self):
        script = """
meta = {"name": "model-alias", "description": "Test workflow"}
return await agent("go", {"provider": "bedrock", "model": "sonnet", "reasoningEffort": "high", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
        "maxToolCalls": 16,
        "maxToolOutputChars": 200000,
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

return await agent("go", {"label": "empty", "toolsets": [], "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("go", {"label": "none", "toolsets": None, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("go", {"agentType": "reader", "label": "reader", "allowedTools": None, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("go", {"agentType": "reader", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
            "maxToolCalls": 16,
            "maxToolOutputChars": 200000,
        }
        for missing in complete:
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
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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
    "reasoningEffort": "high",
    "maxTurns": 10,
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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

return await agent("go", {"agentType": "reader", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("go", {"agentType": "missing-reader", "label": "fallback", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
return await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})""", "meta.agents must be an object"),
            ("""meta = {"name":"bad","description":"bad","agents": {"../bad": {"instructions":"x"}}}
return await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})""", "invalid runtime agent name"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": []}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})""", "meta.agents.reader must be an object"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": {"instructions":"x", "toolsets": 12}}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})""", "toolsets must be"),
            ("""meta = {"name":"bad","description":"bad","agents": {"reader": {"instructions":"x", "isolation": "bad"}}}
return await agent("x", {"agentType":"reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})""", "isolation must be"),
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

return await agent("go", {"label": "deny", "allowedTools": [], "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("fail", {"label": "direct", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        with self.assertRaises(ChildAgentError) as ctx:
            run_workflow(script, WorkflowOptions(child_runner=FailingRunner()))
        self.assertIn("failed:direct", str(ctx.exception))

    def test_pipeline_agent_failure_drops_item_and_skips_remaining_stages(self):
        script = """
meta = {"name": "pipeline-failure", "description": "Test workflow"}

return await pipeline(
    ["a", "b"],
    lambda item, original, index: agent(item, {"label": item, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda prior, original, index: agent("after-" + original, {"label": "after-" + original, "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
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
    lambda: agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda: agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
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

return await agent("skip me", {"label": "skipped", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        result = run_workflow(script, WorkflowOptions(child_runner=SkippingRunner()))
        self.assertIsNone(result.value)
        agent_state = result.state.snapshot()["agents"][0]
        self.assertEqual(agent_state["status"], "skipped")
        self.assertEqual(agent_state["error"], "")

    def test_unknown_agent_type_raises_before_child_launch(self):
        script = """
meta = {"name": "missing-agent-type", "description": "Test workflow"}

return await agent("work", {"agentType": "definitely-missing", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
    "reasoningEffort": "high",
    "maxTurns": 10,
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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
    "maxToolCalls": 16,
    "maxToolOutputChars": 200000,
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

return await agent("work", {"isolation": "shared", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
first = await agent("root", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
child = await workflow({"scriptPath": args["child"]})
last = await agent("after", {"label": "after", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
return [first, child, last]
"""
        child = """
meta = {"name": "child", "description": "Test workflow", "phases": [{"title": "Child"}]}

phase("Child")
return await agent("child", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
                {"id": 1, "kind": "sequential", "status": "done", "steps": 1},
                {"id": 2, "kind": "sequential", "status": "done", "steps": 1},
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

return await agent("child", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 3, "maxToolCalls": 4, "maxToolOutputChars": 20000})
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
        self.assertEqual(snapshot["topologies"][0]["kind"], "pipeline")
        self.assertEqual(
            snapshot["children"][0]["topologies"],
            [{"id": 1, "kind": "sequential", "status": "done", "steps": 1}],
        )

    def test_budget_is_token_budget(self):
        script = """
meta = {"name": "budget", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

    def test_token_budget_blocks_further_agents(self):
        script = """
meta = {"name": "budget-stop", "description": "Test workflow"}

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
return await agent("b", {"label": "b", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

await agent("a", {"label": "a", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("grand", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("child", {"label": "private-child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("same prompt", {"label": "first", "phase": "One", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        second_script = """
meta = {"name": "cache-display-two", "description": "Test workflow"}

return await agent("same prompt", {"label": "second", "phase": "Two", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("same prompt", {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("same prompt", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
    {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000},
)
"""
        runner = FakeRunner()
        result = run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))

        request = runner.requests[0]
        self.assertEqual(request.reasoning_effort, "high")
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertEqual(request.resolved.reasoning_effort, "high")
        self.assertEqual(result.state.snapshot()["agents"][0]["reasoning_effort"], "high")

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
return await agent("go", {"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
return await agent("go", {"agentType": "researcher", "label": "reader", "provider": "openai-codex", "model": "gpt-5.6-luna", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
                f'return await agent("go", {{"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": {value!r}, "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}})'
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
            script = f"meta = {meta!r}\nreturn await agent('go', {{'agentType': 'researcher', 'provider': 'openai-codex', 'model': 'gpt-5.6-luna', 'reasoningEffort': 'medium', 'maxTurns': 10, 'maxToolCalls': 16, 'maxToolOutputChars': 200000}})"
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
                f'return await agent("same prompt", {{"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "{effort}", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}})'
            )

        low_cache = ResumeCache()
        run_workflow(
            script("low"),
            WorkflowOptions(child_runner=FakeRunner(), resume_cache=low_cache),
        )

        high_runner = FakeRunner()
        run_workflow(
            script("high"),
            WorkflowOptions(
                child_runner=high_runner,
                resume_cache=ResumeCache(low_cache.current),
            ),
        )
        self.assertEqual(len(high_runner.requests), 1)

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
        "maxToolCalls": 16,
        "maxToolOutputChars": 200000,
    }

    def _script(self, options):
        return (
            'meta = {"name": "budget-contract", "description": "Test workflow"}\n'
            f"return await agent(\"go\", {options!r})"
        )

    def test_inline_budgets_are_recorded_and_resolved(self):
        runner = FakeRunner()
        result = run_workflow(
            self._script({**self._base, "maxTurns": 3, "maxToolCalls": 7, "maxToolOutputChars": 1234}),
            WorkflowOptions(config=PluginConfig(), child_runner=runner),
        )
        request = runner.requests[0]
        self.assertEqual(request.max_turns, 3)
        self.assertEqual(request.max_tool_calls, 7)
        self.assertEqual(request.max_tool_output_chars, 1234)
        self.assertIsNotNone(request.resolved)
        assert request.resolved is not None
        self.assertEqual(request.resolved.max_turns, 3)
        self.assertEqual(request.resolved.max_tool_calls, 7)
        self.assertEqual(request.resolved.max_tool_output_chars, 1234)
        agent = result.state.snapshot()["agents"][0]
        self.assertEqual(agent["max_turns"], 3)
        self.assertEqual(agent["max_tool_calls"], 7)
        self.assertEqual(agent["max_tool_output_chars"], 1234)

    def test_inline_budget_boundaries_are_accepted(self):
        for key, values in {
            "maxTurns": (1, 1000),
            "maxToolCalls": (1, 10000),
            "maxToolOutputChars": (1, 20_000_000),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    runner = FakeRunner()
                    run_workflow(
                        self._script({**self._base, key: value}),
                        WorkflowOptions(config=PluginConfig(), child_runner=runner),
                    )
                    self.assertEqual(getattr(runner.requests[0], _request_field(key)), value)

    def test_missing_budget_fields_fail_before_launch(self):
        for key in ("maxTurns", "maxToolCalls", "maxToolOutputChars"):
            options = dict(self._base)
            options.pop(key)
            runner = FakeRunner()
            with self.subTest(key=key), self.assertRaises(Exception) as ctx:
                run_workflow(self._script(options), WorkflowOptions(config=PluginConfig(), child_runner=runner))
            self.assertIn(f"agent() {key} is required", str(ctx.exception))
            self.assertEqual(runner.requests, [])

    def test_invalid_inline_budget_values_fail_before_launch(self):
        cases = {
            "maxTurns": (None, True, 1.5, "2", 0, -1, 1001),
            "maxToolCalls": (None, True, 1.5, "2", 0, -1, 10001),
            "maxToolOutputChars": (None, True, 1.5, "2", 0, -1, 20_000_001),
        }
        for key, values in cases.items():
            for value in values:
                options = {**self._base, key: value}
                runner = FakeRunner()
                with self.subTest(key=key, value=value), self.assertRaises(Exception) as ctx:
                    run_workflow(self._script(options), WorkflowOptions(config=PluginConfig(), child_runner=runner))
                self.assertIn(f"agent() {key} must be an integer", str(ctx.exception))
                self.assertEqual(runner.requests, [])

    def test_preset_budgets_are_rejected(self):
        for key in ("maxTurns", "maxToolCalls", "maxToolOutputChars"):
            script = f"""
meta = {{
    "name": "preset-budget",
    "description": "Test workflow",
    "agents": {{"researcher": {{"instructions": "Research.", "{key}": 10}}}},
}}
return await agent("go", {{"agentType": "researcher", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}})
"""
            runner = FakeRunner()
            with self.subTest(key=key), self.assertRaises(Exception) as ctx:
                run_workflow(script, WorkflowOptions(config=PluginConfig(), child_runner=runner))
            self.assertIn(f"{key} is not supported", str(ctx.exception))
            self.assertEqual(runner.requests, [])

    @patch("hermes_dynamic_workflows.child.runner._discoverable_child_toolsets", return_value=[])
    def test_budget_changes_cache_identity(self, _toolsets):
        def script(tool_calls):
            return self._script({**self._base, "maxToolCalls": tool_calls})

        first_cache = ResumeCache()
        run_workflow(script(16), WorkflowOptions(child_runner=FakeRunner(), resume_cache=first_cache))
        changed_runner = FakeRunner()
        run_workflow(
            script(8),
            WorkflowOptions(child_runner=changed_runner, resume_cache=ResumeCache(first_cache.current)),
        )
        cached_runner = FakeRunner()
        run_workflow(
            script(16),
            WorkflowOptions(child_runner=cached_runner, resume_cache=ResumeCache(first_cache.current)),
        )
        self.assertEqual(len(changed_runner.requests), 1)
        self.assertEqual(cached_runner.requests, [])


def _request_field(key):
    return {
        "maxTurns": "max_turns",
        "maxToolCalls": "max_tool_calls",
        "maxToolOutputChars": "max_tool_output_chars",
    }[key]


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
    results.append(await agent("x" + str(i), {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}))
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
await agent("a", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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
        out.append(await agent("x", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000}))
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

await agent("a", {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

return await agent("gc-work", {"label": "gc", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
"""
        child = """
meta = {"name": "child", "description": "child"}

inner = await workflow({"scriptPath": args["grandchild"]}, args)
mine = await agent("child-work", {"label": "child", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
return [inner, mine]
"""
        root = """
meta = {"name": "root", "description": "root"}

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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

mine = await agent("root-work", {"label": "root", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})
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


if __name__ == "__main__":
    unittest.main()
