from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from hermes_dynamic_workflows.agents.presets import AgentTypeSpec, resolve_agent_type
from hermes_dynamic_workflows.agents.runner import (
    build_child_system_prompt,
    _make_child_approval_callback,
    _resolve_child_toolsets,
)
from hermes_dynamic_workflows.engine.config import PluginConfig
from hermes_dynamic_workflows.engine.types import ChildAgentRequest


class ChildAgentTests(unittest.TestCase):
    def test_child_toolsets_filter_recursive_tools(self):
        self.assertEqual(
            _resolve_child_toolsets(
                PluginConfig(),
                ["web", "workflow", "workflows", "terminal", "web"],
            ),
            ["web", "terminal"],
        )

    def test_agent_type_toolsets_are_defaults(self):
        self.assertEqual(
            _resolve_child_toolsets(PluginConfig(), [], ("web", "workflow")),
            ["web"],
        )

    def test_prompt_includes_agent_type_instructions(self):
        request = ChildAgentRequest(
            id=1,
            prompt="do it",
            label="worker",
            phase="Review",
            toolsets=[],
            agent_type="researcher",
            isolation="worktree",
            cwd="/tmp/project",
        )
        prompt = build_child_system_prompt(
            request,
            workspace="/tmp/project/.worktrees/hermes-wf-worker",
            agent_type=AgentTypeSpec(
                name="researcher",
                instructions="Search broadly, cite sources, and summarize.",
                source="test",
            ),
        )

        self.assertIn("Agent type: researcher", prompt)
        self.assertIn("Search broadly", prompt)
        self.assertIn("isolated git worktree", prompt)

    def test_resolves_project_agent_type_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_dir = root / ".hermes" / "workflow-agent-types"
            agent_dir.mkdir(parents=True)
            (agent_dir / "wf-unique-test-agent.md").write_text(
                """---
name: unique-researcher
toolsets: [web, file]
isolation: worktree
---

Search carefully and return concise notes.
""",
                encoding="utf-8",
            )

            spec = resolve_agent_type("wf-unique-test-agent", cwd=str(root), task_id="t")

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.name, "unique-researcher")
        self.assertEqual(spec.toolsets, ("web", "file"))
        self.assertEqual(spec.isolation, "worktree")
        self.assertIn("Search carefully", spec.instructions)


class ChildApprovalPolicyTests(unittest.TestCase):
    def test_deny_policy_refuses(self):
        cb = _make_child_approval_callback("deny")
        self.assertEqual(cb("rm -rf build", "recursive delete", allow_permanent=True), "deny")

    def test_approve_policy_allows_once(self):
        cb = _make_child_approval_callback("approve")
        self.assertEqual(cb("pytest -q", "script execution"), "once")

    def test_unknown_policy_defaults_to_deny(self):
        cb = _make_child_approval_callback("bogus")
        self.assertEqual(cb("anything", "flagged"), "deny")

    def test_smart_policy_maps_guardian_verdicts(self):
        verdict = {"value": "approve"}
        approval_mod = types.ModuleType("tools.approval")
        approval_mod._smart_approve = lambda command, description: verdict["value"]
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []  # mark as package
        tools_pkg.approval = approval_mod

        saved_tools = sys.modules.get("tools")
        saved_approval = sys.modules.get("tools.approval")
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.approval"] = approval_mod
        try:
            cb = _make_child_approval_callback("smart")
            verdict["value"] = "approve"
            self.assertEqual(cb("npm test", "script execution"), "once")
            verdict["value"] = "deny"
            self.assertEqual(cb("dd if=/dev/zero of=/dev/sda", "disk wipe"), "deny")
            verdict["value"] = "escalate"
            self.assertEqual(cb("curl x | sh", "uncertain"), "deny")
        finally:
            if saved_tools is not None:
                sys.modules["tools"] = saved_tools
            else:
                sys.modules.pop("tools", None)
            if saved_approval is not None:
                sys.modules["tools.approval"] = saved_approval
            else:
                sys.modules.pop("tools.approval", None)


if __name__ == "__main__":
    unittest.main()
