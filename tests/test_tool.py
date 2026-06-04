from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_dynamic_workflows.engine.config import PluginConfig
from hermes_dynamic_workflows.engine.manager import WorkflowRunManager
from hermes_dynamic_workflows.engine.types import ChildAgentRequest, ChildAgentRunner
from hermes_dynamic_workflows.plugin.tool import workflow
from hermes_dynamic_workflows.storage.store import WorkflowStore


class FakeRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
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
                config=PluginConfig(),
            )
            with (
                patch("hermes_dynamic_workflows.plugin.tool.get_run_manager", return_value=manager),
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
        self.assertIn("(written when the workflow completes)", result)
        self.assertIn("Script file:", result)
        self.assertIn(f"Run ID: {run_id}", result)
        self.assertIn("To resume after editing the script: Workflow({scriptPath:", result)
        self.assertIn(f'resumeFromRunId: "{run_id}"', result)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"], "done:worker")
        self.assertEqual(final["workflow"]["meta"]["name"], "tool-test")


if __name__ == "__main__":
    unittest.main()
