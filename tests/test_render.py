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

    def test_detailed_progress_renders_active_runtime_topology(self):
        cases = [
            ({"id": 1, "kind": "pipeline", "status": "active", "items": 12, "stages": 2}, "Pipeline · 12 items · 2 stages"),
            ({"id": 1, "kind": "parallel", "status": "active", "lanes": 4}, "Parallel barrier · 4 lanes"),
            ({"id": 1, "kind": "sequential", "status": "active", "steps": 3}, "Sequential · 3 steps"),
        ]

        for topology, expected in cases:
            with self.subTest(kind=topology["kind"]):
                run = {
                    "status": "running",
                    "workflow": {
                        "meta": {"name": "topology"},
                        "topologies": [topology],
                        "agents": [],
                    },
                }

                text = render_module.render_run_progress(run, show_cost=False)

                self.assertIn(f"Topology: {expected}", text)

    def test_nested_active_topology_wins_over_parent_and_latest_completed_is_fallback(self):
        snapshot = {
            "meta": {"name": "nested-topology"},
            "topologies": [
                {"id": 1, "kind": "pipeline", "status": "active", "items": 12, "stages": 2},
                {"id": 2, "kind": "parallel", "status": "active", "lanes": 4},
            ],
            "agents": [],
        }

        active_text = render_module.render_run_progress(
            {"status": "running", "workflow": snapshot},
            show_cost=False,
        )
        self.assertIn("Topology: Parallel barrier · 4 lanes", active_text)

        snapshot["topologies"][1]["status"] = "done"
        resumed_text = render_module.render_run_progress(
            {"status": "running", "workflow": snapshot},
            show_cost=False,
        )
        self.assertIn("Topology: Pipeline · 12 items · 2 stages", resumed_text)

    def test_detailed_progress_groups_member_agents_under_every_topology_with_model_and_effort(self):
        snapshot = {
            "meta": {"name": "topology-tree"},
            "topologies": [
                {"id": 1, "kind": "pipeline", "status": "done", "items": 2, "stages": 2, "agent_ids": [1, 2]},
                {"id": 2, "kind": "parallel", "status": "done", "lanes": 1, "agent_ids": [3]},
                {"id": 3, "kind": "sequential", "status": "active", "steps": 1, "agent_ids": [4]},
            ],
            "agents": [
                {"id": 1, "label": "inspect:one", "status": "done", "model": "gpt-5.6-luna", "reasoning_effort": "medium"},
                {"id": 2, "label": "verify:one", "status": "done", "model": "gpt-5.6-luna", "reasoning_effort": "high"},
                {"id": 3, "label": "lane:one", "status": "done", "model": "gpt-5.6-sol", "reasoning_effort": "medium"},
                {"id": 4, "label": "step:one", "status": "running", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
            ],
        }

        text = render_module.render_run_progress(
            {"status": "running", "workflow": snapshot},
            show_cost=False,
        )

        expected_order = [
            "✓ Pipeline · 2 items · 2 stages",
            "✓ inspect:one · gpt-5.6-luna medium",
            "✓ verify:one · gpt-5.6-luna high",
            "✓ Parallel barrier · 1 lane",
            "✓ lane:one · gpt-5.6-sol medium",
            "▶ Sequential · 1 step",
            "▶ step:one · gpt-5.6-luna max",
        ]
        positions = [text.index(expected) for expected in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("Topology: Sequential", text)

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
