from __future__ import annotations

import unittest

from hermes_dynamic_workflows.view import render as render_module


class BoundedProgressRenderTests(unittest.TestCase):
    def test_bounded_text_honors_exact_96_and_240_character_limits(self):
        self.assertTrue(hasattr(render_module, "_bounded_text"))
        bounded_text = render_module._bounded_text

        self.assertEqual(bounded_text("n" * 96, 96), "n" * 96)
        self.assertEqual(bounded_text("n" * 97, 96), "n" * 95 + "…")
        self.assertEqual(bounded_text("l" * 240, 240), "l" * 240)
        self.assertEqual(bounded_text("l" * 241, 240), "l" * 239 + "…")

    def test_bounded_text_collapses_whitespace_and_preserves_unicode_markup(self):
        self.assertTrue(hasattr(render_module, "_bounded_text"))

        self.assertEqual(
            render_module._bounded_text("  **αβ**\n\t<phase>   `code`  ", 96),
            "**αβ** <phase> `code`",
        )

    def test_detailed_progress_shows_only_latest_root_log_on_one_line(self):
        run = {
            "status": "running",
            "workflow": {
                "meta": {"name": "root-log"},
                "phases": [{"title": "Review"}],
                "logs": ["older", "  latest\nroot   **signal**  "],
                "agents": [{"id": 1, "label": "worker", "status": "running", "phase": "Review"}],
                "children": [
                    {
                        "meta": {"name": "nested"},
                        "logs": ["nested latest must stay hidden"],
                        "agents": [],
                        "children": [],
                    }
                ],
            },
        }

        text = render_module.render_run_progress(run, show_cost=False)

        self.assertIn("Log: latest root **signal**", text)
        self.assertNotIn("older", text)
        self.assertNotIn("nested latest", text)
        self.assertEqual(next(line for line in text.splitlines() if "Log:" in line), "   Log: latest root **signal**")

    def test_overlapping_pipeline_uses_same_current_phase_as_renderer_helper(self):
        snapshot = {
            "meta": {"name": "pipeline"},
            "phases": [
                {"title": "Review", "detail": "review changes"},
                {"title": "Verify", "detail": "verify findings"},
                {"title": "Synthesize", "detail": "write verdict"},
            ],
            "agents": [
                {"id": 1, "label": "review", "status": "running", "phase": "Review"},
                {"id": 2, "label": "verify", "status": "running", "phase": "Verify"},
                {"id": 3, "label": "synthesize", "status": "queued", "phase": "Synthesize"},
            ],
        }

        text = render_module.render_run_progress({"status": "running", "workflow": snapshot}, show_cost=False)

        self.assertEqual(render_module._current_phase(snapshot), "Verify")
        self.assertIn("Current: Verify", text)
        self.assertIn("Next: write verdict", text)

    def test_large_roster_keeps_bounded_mandatory_fields_within_telegram_limit(self):
        name = "N" * 120
        current = "C" * 120
        next_title = "T" * 120
        next_detail = "D" * 120
        agents = [
            {
                "id": index,
                "label": f"agent-{index:03d}-" + "work" * 20,
                "status": "running",
                "phase": current,
                "duration_seconds": 123.0,
                "tool_calls": 99,
            }
            for index in range(500)
        ]
        run = {
            "status": "running",
            "workflow": {
                "meta": {"name": name},
                "phases": [
                    {"title": current, "detail": "current detail"},
                    {"title": next_title, "detail": next_detail},
                ],
                "logs": ["L" * 300],
                "agents": agents,
            },
        }

        text = render_module.render_run_progress(run, show_cost=False)

        self.assertIn("N" * 95 + "…", text.splitlines()[0])
        self.assertIn("Current: " + "C" * 95 + "…", text)
        self.assertIn("Next: " + "D" * 95 + "…", text)
        self.assertIn("Log: " + "L" * 239 + "…", text)
        self.assertIn("0/500 done", text)
        self.assertLessEqual(len(text), 4096)


if __name__ == "__main__":
    unittest.main()
