from __future__ import annotations

import unittest

from hermes_dynamic_workflows.view.render import render_agent_overview, render_workflow_text


class DisplayTests(unittest.TestCase):
    def test_renders_unphased_agents_when_phases_exist(self):
        text = render_workflow_text(
            {
                "meta": {"name": "display"},
                "phases": ["Review"],
                "agents": [
                    {"id": 1, "label": "unphased", "status": "done", "phase": None},
                    {"id": 2, "label": "review", "status": "running", "phase": "Review"},
                ],
            },
            completed=False,
        )

        self.assertIn("[Review]", text)
        self.assertIn("review", text)
        self.assertIn("[Other]", text)
        self.assertIn("unphased", text)

    def test_renders_child_workflow_and_finds_child_agent(self):
        run = {
            "runId": "wf_test123",
            "status": "running",
            "workflow": {
                "meta": {"name": "parent"},
                "phases": [],
                "agents": [],
                "children": [
                    {
                        "meta": {"name": "child"},
                        "phases": [{"title": "Child"}],
                        "agents": [
                            {
                                "id": 2,
                                "label": "child-agent",
                                "status": "done",
                                "phase": "Child",
                                "prompt": "work",
                                "result_preview": "done",
                            }
                        ],
                        "children": [],
                        "errors": [],
                    }
                ],
                "errors": [],
            },
        }

        text = render_workflow_text(run["workflow"], completed=False)
        overview = render_agent_overview([run])

        self.assertIn("> child", text)
        self.assertIn("child-agent", text)
        self.assertIn("child-agent", overview)

    def test_renders_agent_overview_with_structured_failure(self):
        run = {
            "runId": "wf_test123",
            "status": "completed",
            "taskId": "wgtest01",
            "workflow": {
                "meta": {"name": "structured"},
                "phases": [],
                "agents": [
                    {
                        "id": 1,
                        "label": "json",
                        "status": "done",
                        "prompt": "work",
                        "result_preview": "{'ok': True}",
                        "structured": {
                            "status": "failed",
                            "mode": "tool",
                            "attempts": 2,
                        },
                    }
                ],
                "children": [],
                "errors": [],
            },
        }

        render_workflow_text(run["workflow"], completed=True)
        overview = render_agent_overview([run])

        self.assertIn("structured", overview)
        self.assertIn("wgtest01", overview)
        self.assertIn("json", overview)
        # Compact overview hides the per-agent structured/telemetry detail.
        self.assertNotIn("schema failed", overview)
        # Verbose overview surfaces the structured-output failure marker.
        verbose = render_agent_overview([run], verbose=True)
        self.assertIn("schema failed", verbose)

    def test_render_run_progress_is_compact_and_truncates_labels(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        long_label = "verify: " + ("FinalizeDraftTelemetryJob and its very long prompt " * 4)
        run = {
            "runId": "wf_progress1",
            "taskId": "wgprog001",
            "status": "running",
            "startedAt": "2026-06-18T17:00:00+00:00",
            "workflow": {
                "meta": {"name": "activix-standards-gate"},
                "phases": [{"title": "Verify"}],
                "agents": [
                    {"id": 1, "label": "standards", "status": "done", "phase": "Verify",
                     "tokens": 651500, "cache_read_tokens": 543200, "tool_calls": 23},
                    {"id": 2, "label": long_label, "status": "running", "phase": "Verify",
                     "tokens": 1122800, "cache_read_tokens": 1027600, "tool_calls": 29},
                ],
                "errors": [],
            },
        }

        text = render_run_progress(run)
        # Glanceable: name, phase, progress fraction, running count.
        self.assertIn("activix-standards-gate", text)
        self.assertIn("Verify", text)
        self.assertIn("1/2 done", text)
        self.assertIn("1 running", text)
        # No raw telemetry leaks into the compact progress block.
        self.assertNotIn("cached read", text)
        self.assertNotIn("tok", text)
        self.assertNotIn("type:", text)
        # The long agent label is truncated, not dumped verbatim.
        self.assertNotIn(long_label, text)
        self.assertIn("…", text)
        # No single rendered line is absurdly long.
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 120)

    def test_render_run_progress_no_ids_line(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = {
            "runId": "wf_noids",
            "taskId": "wgnoids01",
            "status": "running",
            "workflow": {
                "meta": {"name": "wf"},
                "phases": [],
                "agents": [{"id": 1, "label": "a", "status": "running"}],
                "errors": [],
            },
        }
        text = render_run_progress(run)
        # The bubble's launch/completion framing carries the task id; the body
        # itself omits the id line that /workflows shows.
        self.assertNotIn("wgnoids01", text)
        self.assertNotIn("wf_noids", text)


if __name__ == "__main__":
    unittest.main()
