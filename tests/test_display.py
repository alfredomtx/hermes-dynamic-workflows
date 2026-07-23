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
        # Glanceable: name, phase progress, running count.
        self.assertIn("activix-standards-gate", text)
        self.assertIn("Verify", text)
        self.assertIn("1/2 done", text)
        self.assertIn("1 running", text)
        # Single-phase fan-out: per-agent rows carry the running marker.
        self.assertIn("▶", text)
        # Aggregate tokens DO show in the detailed bubble header (~K tok), but
        # raw per-agent telemetry must not leak.
        self.assertIn("tok", text)
        self.assertNotIn("cached read", text)
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

    # --- Detailed bubble layout (Claude-Code-style progress UX) -------------

    def _fanout_run(self):
        return {
            "runId": "wf_fan",
            "status": "running",
            "workflow": {
                "meta": {"name": "audit"},
                "phases": [{"title": "Audit"}],
                "agents": [
                    {"id": 1, "label": "structural", "status": "done", "phase": "Audit",
                     "duration_seconds": 22.0, "tokens": 12000},
                    {"id": 2, "label": "wireframe", "status": "running", "phase": "Audit",
                     "duration_seconds": 72.0, "tokens": 9000},
                    {"id": 3, "label": "code", "status": "running", "phase": "Audit",
                     "duration_seconds": 69.0, "tokens": 8000},
                ],
                "errors": [],
            },
        }

    def test_fanout_shows_per_agent_elapsed_and_header_tokens(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        text = render_run_progress(self._fanout_run())
        # Header carries aggregate tokens.
        self.assertIn("tok", text)
        # Each agent row carries its own elapsed.
        self.assertIn("1m 12s", text)  # 72s running agent
        self.assertIn("1m 9s", text)   # 69s running agent
        self.assertIn("22s", text)     # 22s done agent
        # Running agents (marker ▶) sort before the done agent (✓) in the body.
        self.assertIn("▶", text)
        self.assertIn("✓", text)
        body = text.split("\n", 1)[1]
        self.assertLess(body.index("wireframe"), body.index("structural"))

    def test_fanout_shows_all_rows_no_overflow_tail(self):
        """Alfredo's request: the detailed roster shows ALL agents, no '… +N'
        collapse on a normal run (15 agents used to trim to 10 + '… +5')."""
        from hermes_dynamic_workflows.view.render import render_run_progress

        agents = [
            {"id": i, "label": f"a{i}", "status": "running", "phase": "Audit",
             "duration_seconds": float(i)}
            for i in range(15)
        ]
        run = {
            "runId": "wf_big",
            "status": "running",
            "workflow": {"meta": {"name": "big"}, "phases": [{"title": "Audit"}],
                         "agents": agents, "errors": []},
        }
        text = render_run_progress(run)
        self.assertNotIn("… +", text)  # no collapse — every agent shows
        for i in range(15):
            self.assertIn(f"a{i}", text)

    def test_fanout_char_budget_backstop_trims_pathological_run(self):
        """A pathological multi-hundred-agent fan-out still trims a tail via the
        char-budget backstop so the bubble stays under Telegram's 4096 limit."""
        from hermes_dynamic_workflows.view.render import render_run_progress

        agents = [
            {"id": i, "label": f"agent-task-{i:03d}-doing-some-work",
             "status": "running", "phase": "Sweep", "duration_seconds": 100.0,
             "model": "us.anthropic.claude-opus-4-8", "reasoning_effort": "xhigh",
             "tool_calls": 5}
            for i in range(300)
        ]
        run = {
            "runId": "wf_mega",
            "status": "running",
            "workflow": {"meta": {"name": "mega"}, "phases": [{"title": "Sweep"}],
                         "agents": agents, "errors": []},
        }
        text = render_run_progress(run)
        self.assertIn("… +", text)       # backstop trimmed a tail
        self.assertLess(len(text), 4096)  # stays under Telegram's message cap

    # --- Per-subtask cost breakdown (Alfredo: "how much did each verify cost") -

    def _priced_agents(self):
        # opus on bedrock with real-ish token buckets -> a non-None per-agent cost
        return [
            {"id": i, "label": f"verify:AC-{i}", "status": "running", "phase": "Verify",
             "duration_seconds": 300.0, "model": "us.anthropic.claude-opus-4-8",
             "provider": "bedrock", "reasoning_effort": "xhigh", "tool_calls": 4,
             "input_tokens": 100000 + i * 15000, "output_tokens": 8000,
             "cache_read_tokens": 400000, "cache_write_tokens": 120000}
            for i in range(1, 4)
        ]

    def test_fanout_rows_carry_per_agent_cost_when_show_cost(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = {"runId": "wf_cost", "status": "running",
               "workflow": {"meta": {"name": "verify-matrix"},
                            "phases": [{"title": "Verify"}],
                            "agents": self._priced_agents(), "errors": []}}
        text = render_run_progress(run, show_cost=True)
        # Each priced row carries its OWN dollar amount (not just the header sum).
        self.assertGreaterEqual(text.count("~$"), 3 + 1)  # 3 rows + header total

    def test_fanout_rows_omit_cost_when_show_cost_false(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = {"runId": "wf_nocost", "status": "running",
               "workflow": {"meta": {"name": "verify-matrix"},
                            "phases": [{"title": "Verify"}],
                            "agents": self._priced_agents(), "errors": []}}
        text = render_run_progress(run, show_cost=False)
        self.assertNotIn("~$", text)

    def test_pipeline_phase_rows_carry_cost_subtotal(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        agents = self._priced_agents()
        for a in agents[:1]:
            a["phase"] = "Review"
        run = {"runId": "wf_pcost", "status": "running",
               "workflow": {"meta": {"name": "review-matrix"},
                            "phases": [{"title": "Review"}, {"title": "Verify"}],
                            "agents": agents, "errors": []}}
        text = render_run_progress(run, show_cost=True)
        # Both phase header lines carry a ~$ subtotal segment.
        review_line = next(l for l in text.splitlines() if "Review" in l)
        verify_line = next(l for l in text.splitlines() if "Verify" in l and "verify:" not in l)
        self.assertIn("~$", review_line)
        self.assertIn("~$", verify_line)

    def test_cost_breakdown_block_groups_and_subtotals(self):
        from hermes_dynamic_workflows.view.render import render_cost_breakdown

        agents = []
        for i in range(1, 4):
            a = self._priced_agents()[i - 1]
            a["phase"] = "Review" if i <= 2 else "Verify"
            a["status"] = "done"
            agents.append(a)
        run = {"status": "completed",
               "workflow": {"meta": {"name": "m"},
                            "phases": [{"title": "Review"}, {"title": "Verify"}],
                            "agents": agents, "errors": []}}
        text = render_cost_breakdown(run)
        self.assertIn("Cost by subtask", text)
        self.assertIn("Review", text)
        self.assertIn("Verify", text)
        for i in range(1, 4):
            self.assertIn(f"verify:AC-{i}", text)
        self.assertGreaterEqual(text.count("~$"), 3)  # at least one per agent

    def test_cost_breakdown_uses_topology_headers_and_includes_reasoning_effort(self):
        from hermes_dynamic_workflows.view.render import render_cost_breakdown

        agents = self._priced_agents()
        for agent in agents:
            agent["status"] = "done"
        run = {
            "status": "completed",
            "workflow": {
                "meta": {"name": "topology-cost"},
                "phases": [{"title": "Pipeline"}, {"title": "Parallel"}],
                "agents": agents,
                "topologies": [
                    {
                        "id": 1,
                        "kind": "pipeline",
                        "status": "done",
                        "items": 1,
                        "stages": 2,
                        "agent_ids": [1, 2],
                    },
                    {
                        "id": 2,
                        "kind": "parallel",
                        "status": "done",
                        "lanes": 1,
                        "agent_ids": [3],
                    },
                ],
                "errors": [],
            },
        }

        text = render_cost_breakdown(run)

        self.assertIn("Pipeline · 1 item · 2 stages", text)
        self.assertIn("Parallel barrier · 1 lane", text)
        self.assertIn("verify:AC-1 · claude-opus-4-8 xhigh", text)
        self.assertIn("verify:AC-3 · claude-opus-4-8 xhigh", text)
        self.assertNotIn("   Verify  ", text)

    def test_cost_breakdown_empty_when_nothing_priceable(self):
        from hermes_dynamic_workflows.view.render import render_cost_breakdown

        # codex/included model -> no pricing route -> no fake $0 block
        agents = [
            {"id": i, "label": f"verify:x{i}", "status": "done", "phase": "Verify",
             "model": "openai/gpt-5-codex", "provider": "openai",
             "input_tokens": 100000, "output_tokens": 5000}
            for i in range(3)
        ]
        run = {"status": "completed",
               "workflow": {"meta": {"name": "c"}, "phases": [{"title": "Verify"}],
                            "agents": agents, "errors": []}}
        self.assertEqual(render_cost_breakdown(run), "")


    def _pipeline_run(self, statuses):
        """statuses: dict phase -> list of agent statuses."""
        agents = []
        aid = 0
        for phase, sts in statuses.items():
            for s in sts:
                aid += 1
                agents.append({"id": aid, "label": f"{phase}-{aid}", "status": s,
                               "phase": phase, "duration_seconds": 5.0})
        return {
            "runId": "wf_pipe",
            "status": "running",
            "workflow": {
                "meta": {"name": "review-changes"},
                "phases": [
                    {"title": "Review", "detail": "scan diff dimensions"},
                    {"title": "Verify", "detail": "adversarially verify findings"},
                    {"title": "Synthesize", "detail": "synthesize confirmed findings"},
                ],
                "agents": agents,
                "errors": [],
            },
        }

    def test_pipeline_phase_checklist_one_active(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["done", "done"],
            "Verify": ["done", "running", "running"],
            "Synthesize": ["queued"],
        })
        text = render_run_progress(run)
        # One line per phase.
        self.assertIn("Review", text)
        self.assertIn("Verify", text)
        self.assertIn("Synthesize", text)
        # Exactly one active PHASE (bolded ▶ phase line). Agent rows under an
        # in-flight phase also carry a ▶ marker (B5), so count the phase markers
        # specifically, not every ▶ in the blob.
        self.assertEqual(text.count("**▶"), 1)
        # Done phase marked ✓; pending phase marked ◦.
        self.assertIn("✓ Review", text)
        self.assertIn("◦ Synthesize", text)
        # Active phase is bolded.
        self.assertIn("**▶ Verify", text)
        # B5: the running agents of the active phase are listed beneath it.
        self.assertIn("Verify-", text)

    def test_pipeline_next_lookahead(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["done", "done"],
            "Verify": ["running"],
            "Synthesize": ["queued"],
        })
        text = render_run_progress(run)
        # Active is Verify -> Next points at Synthesize's detail.
        self.assertIn("Next: synthesize confirmed findings", text)

    def test_pipeline_no_next_on_last_phase(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["done"],
            "Verify": ["done"],
            "Synthesize": ["running"],
        })
        text = render_run_progress(run)
        self.assertNotIn("Next:", text)

    def test_pipeline_all_done_has_no_active_marker(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["done"],
            "Verify": ["done"],
            "Synthesize": ["done"],
        })
        text = render_run_progress(run)
        self.assertNotIn("▶", text)  # every phase ✓, none bolded active

    def test_pipeline_unstarted_no_phantom_active(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["queued"],
            "Verify": ["queued"],
            "Synthesize": ["queued"],
        })
        text = render_run_progress(run)
        # Nothing running -> no phase bolded active despite phases[-1] fallback.
        self.assertNotIn("▶", text)

    def test_pipeline_unphased_agents_bucketed(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        run = self._pipeline_run({
            "Review": ["done"],
            "Verify": ["running"],
            "Synthesize": ["queued"],
        })
        run["workflow"]["agents"].append(
            {"id": 99, "label": "loose", "status": "running", "phase": None,
             "duration_seconds": 3.0})
        text = render_run_progress(run)
        self.assertIn("[Other]", text)

    def test_completion_collapses_to_summary(self):
        from hermes_dynamic_workflows.run.manager import _progress_bubble_text
        from hermes_dynamic_workflows.core.config import PluginConfig

        run = self._fanout_run()
        run["status"] = "completed"
        run["result"] = "4 confirmed blockers, 2 dismissed"
        text = _progress_bubble_text(run, PluginConfig(), completed=True)
        # Collapsed head: emoji + name + agent count, no per-agent rows.
        self.assertIn("✅", text)
        self.assertIn("Audit completed", text)
        self.assertIn("3 agents", text)
        self.assertNotIn("wireframe", text)  # per-agent rows gone
        # Result preserved.
        self.assertIn("4 confirmed blockers", text)

    def test_completion_summary_uses_status_emoji_for_stopped(self):
        from hermes_dynamic_workflows.view.render import render_run_summary

        run = self._fanout_run()
        run["status"] = "stopped"
        text = render_run_summary(run)
        self.assertIn("⏹", text)

    def test_completion_truncates_long_result(self):
        from hermes_dynamic_workflows.run.manager import _progress_bubble_text
        from hermes_dynamic_workflows.core.config import PluginConfig

        run = self._fanout_run()
        run["status"] = "completed"
        run["result"] = "x" * 5000
        cfg = PluginConfig()
        text = _progress_bubble_text(run, cfg, completed=True)
        self.assertIn("chars omitted", text)
        self.assertLessEqual(len(text), cfg.notify_result_preview_chars + 300)

    def test_overview_stays_compact_no_header_tokens(self):
        run = self._fanout_run()
        run["taskId"] = "wgfan0001"
        overview = render_agent_overview([run])
        # Overview (detailed=False) keeps the compact summary line, NOT the
        # per-agent elapsed rows or the aggregate-token header.
        self.assertIn("1/3 done", overview)
        self.assertNotIn("tok", overview)
        self.assertNotIn("1m 12s", overview)

    def test_running_agent_without_ended_at_shows_elapsed(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # Snapshot-cadence elapsed: an agent still running carries a nonzero
        # duration_seconds computed at snapshot time.
        run = {
            "runId": "wf_live",
            "status": "running",
            "workflow": {
                "meta": {"name": "live"},
                "phases": [{"title": "Go"}],
                "agents": [{"id": 1, "label": "worker", "status": "running",
                            "phase": "Go", "duration_seconds": 41.0}],
                "errors": [],
            },
        }
        text = render_run_progress(run)
        self.assertIn("41s", text)


class CostAndCompletionTests(unittest.TestCase):
    """Cost-in-header, per-agent model/effort/tools rows, and the readable
    completion message (fenced result, no temp-path line)."""

    def _bedrock_agent(self, **over):
        agent = {
            "id": 1, "label": "scope: stack topology", "status": "running",
            "phase": "Audit", "duration_seconds": 72.0,
            "model": "us.anthropic.claude-opus-4-8", "provider": "bedrock",
            "base_url": "https://bedrock-runtime.ca-central-1.amazonaws.com",
            "input_tokens": 1_000_000, "output_tokens": 200_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "tokens": 1_200_000, "tool_calls": 4, "reasoning_effort": "high",
        }
        agent.update(over)
        return agent

    def _run_with(self, agents, status="running", phases=None):
        return {
            "runId": "wf_cost", "status": status,
            "workflow": {
                "meta": {"name": "rebase-tooling-risk-audit"},
                "phases": phases or [{"title": "Audit"}],
                "agents": agents, "errors": [],
            },
        }

    # --- C1/C2/C3 cost in header ---

    def test_cost_in_header_before_tokens(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        text = render_run_progress(self._run_with([self._bedrock_agent()]))
        # 1M in × $5/M + 200K out × $25/M = $5 + $5 = $10.00,
        # with the Bedrock geo-profile correction applied by the shared pricer.
        self.assertIn("~$11.00", text)
        head = text.split("\n", 1)[0]
        # Cost segment comes BEFORE the token segment.
        self.assertLess(head.index("~$11.00"), head.index("tok"))

    def test_cost_omitted_when_no_priced_agent(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # Unknown model -> unknown route -> no cost; tokens still show.
        agent = self._bedrock_agent(model="us.meta.llama4-maverick", provider="bedrock")
        text = render_run_progress(self._run_with([agent]))
        self.assertNotIn("$", text)
        self.assertIn("tok", text)

    def test_cost_subcent_floor_never_zero(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # Tiny but genuinely-known usage rounds below a cent -> "<$0.01", not $0.00.
        agent = self._bedrock_agent(input_tokens=100, output_tokens=10,
                                    cache_read_tokens=0, cache_write_tokens=0,
                                    tokens=110)
        text = render_run_progress(self._run_with([agent]))
        self.assertIn("<$0.01", text)
        self.assertNotIn("$0.00", text)

    def test_cost_no_cache_double_count(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # input 100K×$5 + output 20K×$25 + cacheR 500K×$0.50 + cacheW 40K×$6.25
        # = 0.50 + 0.50 + 0.25 + 0.25 = $1.50 before Bedrock geo-profile correction
        # (cache is not billed at the input rate).
        agent = self._bedrock_agent(
            input_tokens=100_000, output_tokens=20_000,
            cache_read_tokens=500_000, cache_write_tokens=40_000, tokens=660_000,
        )
        text = render_run_progress(self._run_with([agent]))
        self.assertIn("~$1.65", text)

    def test_mixed_model_sum_codex_included_plus_opus(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        opus = self._bedrock_agent(id=1, label="opus", status="done")
        codex = self._bedrock_agent(
            id=2, label="codex", status="done",
            model="gpt-5.5", provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            input_tokens=500_000, output_tokens=100_000, tokens=600_000,
        )
        text = render_run_progress(self._run_with([opus, codex]))
        # Codex company OAuth is real API-priced; sum includes Bedrock-corrected opus + Codex.
        self.assertIn("~$24.75", text)

    def test_cost_hidden_when_show_cost_false(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        text = render_run_progress(self._run_with([self._bedrock_agent()]), show_cost=False)
        self.assertNotIn("$", text)
        self.assertIn("tok", text)  # tokens still present

    # --- C4/C5 per-agent rows: model, effort, tools ---

    def test_agent_row_shows_model_effort_tools(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        text = render_run_progress(self._run_with([self._bedrock_agent()]))
        # Short model (region/vendor prefix stripped) + effort + tool count.
        self.assertIn("claude-opus-4-8 high", text)
        self.assertIn("4 tools", text)
        self.assertNotIn("us.anthropic.claude-opus-4-8", text)  # prefix stripped

    def test_agent_row_omits_effort_when_absent(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        agent = self._bedrock_agent(reasoning_effort=None)
        text = render_run_progress(self._run_with([agent]))
        self.assertIn("claude-opus-4-8", text)
        self.assertNotIn("claude-opus-4-8 high", text)

    def test_short_model_keeps_version_dot(self):
        from hermes_dynamic_workflows.view.render import _short_model

        # A version dot must NOT be mistaken for a region/vendor prefix.
        self.assertEqual(_short_model("gpt-5.5"), "gpt-5.5")
        self.assertEqual(_short_model("gpt-4.1-mini"), "gpt-4.1-mini")
        self.assertEqual(_short_model("us.anthropic.claude-opus-4-8"), "claude-opus-4-8")
        self.assertEqual(_short_model("anthropic.claude-opus-4-8"), "claude-opus-4-8")

    def test_pipeline_lists_running_agents_across_concurrent_phases(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # pipeline() is non-barrier: agents running in TWO phases at once must
        # BOTH be listed (B5), not just the resolved "active" phase.
        a1 = self._bedrock_agent(id=1, label="rev-A", status="running", phase="Review")
        a2 = self._bedrock_agent(id=2, label="ver-B", status="running", phase="Verify")
        run = self._run_with(
            [a1, a2], phases=[{"title": "Review"}, {"title": "Verify"}, {"title": "Synthesize"}],
        )
        text = render_run_progress(run)
        self.assertIn("rev-A", text)
        self.assertIn("ver-B", text)

    # --- C7 readable completion ---

    def test_completion_dict_result_is_bounded_plain_text_no_temp_path(self):
        from hermes_dynamic_workflows.run.manager import _progress_bubble_text
        from hermes_dynamic_workflows.core.config import PluginConfig

        run = self._run_with([self._bedrock_agent(status="done")], status="completed")
        run["result"] = {"audit_scopes": [], "total_findings": 0, "register": []}
        run["outputFile"] = "/var/folders/mp/abc/tasks/wg432juun.output"
        text = _progress_bubble_text(run, PluginConfig(), completed=True)
        self.assertIn('"audit_scopes": []', text)
        self.assertNotIn("```", text)
        self.assertNotIn("/var/folders", text)
        self.assertNotIn("Full output", text)


    def test_completion_string_result_is_plain_not_fenced(self):
        from hermes_dynamic_workflows.run.manager import _progress_bubble_text
        from hermes_dynamic_workflows.core.config import PluginConfig

        run = self._run_with([self._bedrock_agent(status="done")], status="completed")
        run["result"] = "4 confirmed blockers, 2 dismissed"
        text = _progress_bubble_text(run, PluginConfig(), completed=True)
        self.assertIn("4 confirmed blockers", text)
        self.assertNotIn("```", text)
        self.assertNotIn("/var/folders", text)

    def test_bubble_under_telegram_cap_pathological_labels(self):
        from hermes_dynamic_workflows.view.render import render_run_progress

        # A wide multi-phase fan-out with long labels must stay under the cap.
        agents = []
        for i in range(40):
            agents.append(self._bedrock_agent(
                id=i, label="x" * 300, status="running",
                phase="Audit" if i % 2 == 0 else "Verify",
            ))
        run = self._run_with(agents, phases=[{"title": "Audit"}, {"title": "Verify"}])
        text = render_run_progress(run)
        self.assertLess(len(text), 4000)

    def test_stable_workflow_header_is_shared_shape_for_active_and_terminal_progress(self):
        from hermes_dynamic_workflows.view.render import render_run_progress, render_workflow_header

        run = {
            "status": "running",
            "workflow": {
                "meta": {"name": "consolidate-delegation-policy"},
                "duration_seconds": 597,
                "totals": {"agents": 2, "done": 1, "running": 1, "errors": 0, "tokens": 1_860_000},
                "agents": [
                    {"id": 1, "label": "policy", "status": "done", "model": "gpt-5.6-luna", "tokens": 930_000},
                    {"id": 2, "label": "delegation", "status": "running", "model": "gpt-5.6-luna", "tokens": 930_000},
                ],
            },
        }

        from unittest.mock import patch

        header = render_workflow_header(run, show_cost=False)
        progress_header = render_run_progress(run, show_cost=False).splitlines()[0]
        with patch("hermes_dynamic_workflows.view.render._format_cost", return_value="~$1.05"):
            priced_header = render_workflow_header(run, show_cost=True)

        self.assertEqual(header, "🔄 consolidate-delegation-policy · 9m 57s · ~1.86M tok")
        self.assertEqual(progress_header, header)
        self.assertEqual(priced_header, "🔄 consolidate-delegation-policy · 9m 57s · ~$1.05 · ~1.86M tok")
        self.assertFalse(progress_header.startswith("✅"))
        self.assertFalse(progress_header.startswith("❌"))

    def test_terminal_task_snapshot_uses_tree_glyphs_and_has_no_result_card(self):
        from hermes_dynamic_workflows.view.render import render_terminal_task_snapshot

        run = {
            "status": "failed",
            "workflow": {
                "meta": {"name": "glyph-canary"},
                "duration_seconds": 2,
                "totals": {"agents": 2, "done": 1, "running": 0, "errors": 1, "tokens": 0},
                "agents": [
                    {"id": 1, "label": "completed task", "status": "done"},
                    {"id": 2, "label": "failed task", "status": "failed"},
                ],
            },
        }

        text = render_terminal_task_snapshot(run, show_cost=False)

        self.assertTrue(text.startswith("🔄 glyph-canary · 2s"))
        self.assertIn("✓ completed task", text)
        self.assertIn("✗ failed task", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("❌", text)
        self.assertNotIn("**", text)

    def test_failed_topology_row_uses_tree_failure_glyph(self):
        from hermes_dynamic_workflows.view.render import render_terminal_task_snapshot

        run = {
            "status": "failed",
            "workflow": {
                "meta": {"name": "topology-glyph-canary"},
                "duration_seconds": 1,
                "totals": {"agents": 0, "done": 0, "running": 0, "errors": 1, "tokens": 0},
                "agents": [],
                "topologies": [{"kind": "pipeline", "status": "failed", "items": 1, "stages": 1, "agent_ids": []}],
            },
        }

        self.assertIn("✗ Pipeline", render_terminal_task_snapshot(run, show_cost=False))


if __name__ == "__main__":
    unittest.main()
