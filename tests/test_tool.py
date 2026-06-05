from __future__ import annotations

import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_dynamic_workflows.engine.config import PluginConfig
from hermes_dynamic_workflows.engine.manager import WorkflowRunManager
from hermes_dynamic_workflows.engine.types import ChildAgentRequest, ChildAgentRunner
from hermes_dynamic_workflows.plugin.task_output import task_output
from hermes_dynamic_workflows.plugin.workflow import workflow
from hermes_dynamic_workflows.storage.store import WorkflowStore


class FakeRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        return f"done:{request.label}"


class BlockingRunner(ChildAgentRunner):
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, request: ChildAgentRequest):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test runner was not released")
        return f"done:{request.label}"


class ToolTests(unittest.TestCase):
    def test_tool_returns_claude_style_launch_text(self):
        script = """
meta = {"name": "tool-test"}

def workflow():
    return agent("do it", {"label": "worker"})
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(
                store=WorkflowStore(Path(tmp)),
                config=PluginConfig(require_launch_approval=False),
            )
            with (
                patch("hermes_dynamic_workflows.plugin.workflow.get_run_manager", return_value=manager),
                patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=FakeRunner()),
            ):
                result = workflow({"script": script, "args": ["x"]}, task_id="tool-session")
                match = re.search(r"^Run ID: (wf_[a-z0-9]{8}-[a-z0-9]{3})$", result, re.MULTILINE)
                self.assertIsNotNone(match)
                run_id = match.group(1)
                final = manager.wait(run_id, timeout=2)

        self.assertRegex(result, r"Workflow launched in background\. Task ID: wg[a-z0-9]{7}")
        self.assertIn("Summary: tool-test", result)
        self.assertIn("Transcript dir:", result)
        self.assertIn("tool-session", result)
        self.assertNotIn("(written when the workflow completes)", result)
        self.assertIn("Script file:", result)
        self.assertIn(f"Run ID: {run_id}", result)
        self.assertIn("To resume after editing the script: Workflow({scriptPath:", result)
        self.assertIn(f'resumeFromRunId: "{run_id}"', result)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "done:worker")
        self.assertEqual(final["workflow"]["meta"]["name"], "tool-test")

    def test_task_output_reports_running_and_completed_workflow(self):
        script = """
meta = {"name": "task-output-test"}

def workflow():
    return agent("do it", {"label": "worker"})
"""
        runner = BlockingRunner()
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkflowRunManager(
                store=WorkflowStore(Path(tmp)),
                config=PluginConfig(require_launch_approval=False),
            )
            with (
                patch("hermes_dynamic_workflows.plugin.task_output.get_run_manager", return_value=manager),
                patch("hermes_dynamic_workflows.agents.runner.HermesChildAgentRunner", return_value=runner),
            ):
                rec = manager.start_from_params({"script": script}, cwd=tmp, host_session_id="tool-session")
                self.assertTrue(runner.started.wait(timeout=2))

                running = task_output({"task_id": rec["taskId"], "block": False})
                self.assertIn("<retrieval_status>not_ready</retrieval_status>", running)
                self.assertIn(f"<task_id>{rec['taskId']}</task_id>", running)
                self.assertIn("<task_type>local_workflow</task_type>", running)
                self.assertIn("<status>running</status>", running)

                timeout = task_output({"task_id": rec["taskId"], "block": True, "timeout": 0})
                self.assertIn("<retrieval_status>timeout</retrieval_status>", timeout)
                self.assertIn("<status>running</status>", timeout)

                runner.release.set()
                final = manager.wait(rec["runId"], timeout=2)
                self.assertEqual(final["status"], "completed")

                completed = task_output({"task_id": rec["taskId"], "block": False})
                self.assertIn("<retrieval_status>success</retrieval_status>", completed)
                self.assertIn("<status>completed</status>", completed)
                self.assertIn("<output>\ndone:worker\n</output>", completed)


if __name__ == "__main__":
    unittest.main()
