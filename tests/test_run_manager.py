from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_dynamic_workflows.engine.config import PluginConfig
from hermes_dynamic_workflows.engine.manager import WorkflowRunManager
from hermes_dynamic_workflows.engine.types import ChildAgentRequest, ChildAgentResult, ChildAgentRunner
from hermes_dynamic_workflows.storage.store import WorkflowStore


class RecordingRunner(ChildAgentRunner):
    """Thread-safe runner that records each call's label and returns a stable
    per-label result, so a resume that reuses cached results makes no new
    run() calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self.labels: list[str] = []

    def run(self, request: ChildAgentRequest):
        with self._lock:
            self.labels.append(request.label)
        return f"result:{request.label}"


class BudgetRunner(ChildAgentRunner):
    def __init__(self, tokens: int):
        self.tokens = tokens

    def run(self, request: ChildAgentRequest):
        return ChildAgentResult(content=request.label, metadata={"tokens": self.tokens})


class FailingRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        raise RuntimeError("always fails")


class HalfFailingRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        if request.label == "a":
            raise RuntimeError("boom")
        return f"ok:{request.label}"


class CountingRunner(ChildAgentRunner):
    calls = 0

    def run(self, request: ChildAgentRequest):
        type(self).calls += 1
        return f"{type(self).calls}:{request.label}"


class MetadataRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        return ChildAgentResult(
            content="metadata-result",
            metadata={
                "runner": "standalone",
                "workspace": request.cwd,
                "agent_type": request.agent_type,
                "isolation": request.isolation or "shared",
                "model": "test-model",
                "tokens": 1234,
                "cache_read_tokens": 2048,
                "cache_write_tokens": 512,
                "tool_calls": 5,
            },
        )


class RunManagerTests(unittest.TestCase):
    def setUp(self):
        CountingRunner.calls = 0

    def test_script_path_run(self):
        script = """
meta = {"name": "from-path"}

def workflow():
    return agent("work", {"label": "path-agent"})
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / "workflow.py"
            script_path.write_text(script, encoding="utf-8")
            manager = WorkflowRunManager(store=WorkflowStore(root / "store"), config=PluginConfig())
            with patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=CountingRunner()):
                record = manager.start_from_params({"scriptPath": str(script_path)}, cwd=str(root))
                final = manager.wait(record["runId"], timeout=2)

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "1:path-agent")
        self.assertEqual(final["source"]["type"], "scriptPath")

    def test_resume_reuses_unchanged_prefix(self):
        script = """
meta = {"name": "resume"}

def workflow():
    return [
        agent("a", {"label": "a"}),
        agent("b", {"label": "b"}),
    ]
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(store=WorkflowStore(Path(tmp)), config=PluginConfig())
            with patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=CountingRunner()):
                first = manager.start_from_params({"script": script}, cwd=tmp)
                first_final = manager.wait(first["runId"], timeout=2)
                second = manager.start_from_params(
                    {"script": script, "resumeFromRunId": first["runId"]},
                    cwd=tmp,
                )
                second_final = manager.wait(second["runId"], timeout=2)

        self.assertEqual(first_final["result"], ["1:a", "2:b"])
        self.assertEqual(second_final["result"], ["1:a", "2:b"])
        self.assertEqual(CountingRunner.calls, 2)

    def test_formats_agent_detail_and_saves_markdown(self):
        script = """
meta = {"name": "inspectable", "phases": ["Search"]}

def workflow():
    phase("Search")
    return agent("inspect metadata", {"label": "meta-agent", "agentType": "researcher"})
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = WorkflowRunManager(store=WorkflowStore(root / "store"), config=PluginConfig())
            with patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=MetadataRunner()):
                record = manager.start_from_params({"script": script}, cwd=str(root))
                final = manager.wait(record["runId"], timeout=2)
                detail = manager.format_agent(final["runId"], "1")
                saved = manager.save_markdown(final["runId"])

        self.assertIn("meta-agent", detail)
        self.assertIn("test-model", detail)
        self.assertIn("1.2K tok", detail)
        self.assertIn("2.0K cached read", detail)
        self.assertIn("Saved workflow", saved)

    def test_save_named_workflow_writes_reusable_script(self):
        from hermes_dynamic_workflows.ui.commands import discover_named_workflows

        script = """
meta = {"name": "audit"}

def workflow():
    return agent("audit", {"label": "auditor"})
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkflowStore(root / "store")
            manager = WorkflowRunManager(store=store, config=PluginConfig())
            with patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=CountingRunner()):
                record = manager.start_from_params({"script": script}, cwd=str(root))
                final = manager.wait(record["runId"], timeout=2)

            project = manager.save_named_workflow(final["runId"], "repo-audit", scope="project", cwd=str(root))
            user = manager.save_named_workflow(final["runId"], "user-audit", scope="user", cwd=str(root))
            reserved = manager.save_named_workflow(final["runId"], "workflows", scope="project", cwd=str(root))

            self.assertTrue(project["ok"])
            self.assertEqual(project["name"], "repo-audit")
            project_path = Path(project["path"])
            self.assertEqual(project_path, root / ".hermes" / "workflows" / "repo-audit.py")
            self.assertIn("def workflow()", project_path.read_text(encoding="utf-8"))

            self.assertTrue(user["ok"])
            self.assertEqual(Path(user["path"]), store.workflows_dir / "user-audit.py")

            self.assertFalse(reserved["ok"])

            discovered = discover_named_workflows(str(root))
            self.assertIn("repo-audit", discovered)

    def test_resume_reuses_parallel_results(self):
        # Regression for the content-addressed resume cache: under the old
        # sequence-keyed cache, parallel()'s non-deterministic reserve order
        # broke resume after the first parallel block. Fingerprint keying makes
        # resume order-independent, so the second run reuses all three results
        # and issues no new child runs.
        script = """
meta = {"name": "parallel-resume"}

def workflow():
    return parallel([
        lambda: agent("alpha", {"label": "a"}),
        lambda: agent("beta", {"label": "b"}),
        lambda: agent("gamma", {"label": "c"}),
    ])
"""
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(
                store=WorkflowStore(Path(tmp)), config=PluginConfig(concurrency=3)
            )
            with patch(
                "hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner",
                return_value=runner,
            ):
                first = manager.start_from_params({"script": script}, cwd=tmp)
                first_final = manager.wait(first["runId"], timeout=3)
                self.assertEqual(len(runner.labels), 3)
                second = manager.start_from_params(
                    {"script": script, "resumeFromRunId": first["runId"]}, cwd=tmp
                )
                second_final = manager.wait(second["runId"], timeout=3)

        self.assertEqual(first_final["status"], "completed")
        self.assertEqual(second_final["result"], first_final["result"])
        # No new child runs on resume — all three came from the cache.
        self.assertEqual(len(runner.labels), 3)

    def test_token_budget_param_gates_run(self):
        script = """
meta = {"name": "budget-param"}

def workflow():
    agent("a", {"label": "a"})
    return agent("b", {"label": "b"})
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(store=WorkflowStore(Path(tmp)), config=PluginConfig())
            with patch(
                "hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner",
                return_value=BudgetRunner(tokens=20),
            ):
                record = manager.start_from_params(
                    {"script": script, "token_budget": 10}, cwd=tmp
                )
                final = manager.wait(record["runId"], timeout=2)

        self.assertEqual(record["tokenBudget"], 10)
        # First agent spends 20 > 10, so the second agent's reservation trips the
        # hard ceiling and the run errors.
        self.assertEqual(final["status"], "error")
        self.assertIn("budget", (final["error"] or "").lower())

    def test_all_agents_failed_marks_run_failed(self):
        script = """
meta = {"name": "all-fail"}

def workflow():
    return parallel([
        lambda: agent("a", {"label": "a"}),
        lambda: agent("b", {"label": "b"}),
    ])
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(
                store=WorkflowStore(Path(tmp)), config=PluginConfig(concurrency=2)
            )
            with patch(
                "hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner",
                return_value=FailingRunner(),
            ):
                rec = manager.start_from_params({"script": script}, cwd=tmp)
                final = manager.wait(rec["runId"], timeout=3)

        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["result"], [None, None])

    def test_partial_failure_stays_completed(self):
        script = """
meta = {"name": "partial"}

def workflow():
    return parallel([
        lambda: agent("a", {"label": "a"}),
        lambda: agent("b", {"label": "b"}),
    ])
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(
                store=WorkflowStore(Path(tmp)), config=PluginConfig(concurrency=2)
            )
            with patch(
                "hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner",
                return_value=HalfFailingRunner(),
            ):
                rec = manager.start_from_params({"script": script}, cwd=tmp)
                final = manager.wait(rec["runId"], timeout=3)

        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], [None, "ok:b"])


if __name__ == "__main__":
    unittest.main()
