from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_dynamic_workflows.storage.store import WorkflowStore
from hermes_dynamic_workflows.tui.app import TuiController
from hermes_dynamic_workflows.tui.model import WorkflowRepository, _JsonlTailReader
from hermes_dynamic_workflows.tui.render import RenderState, _display_width, render_screen


class FakeControlClient:
    def __init__(self):
        self.requests = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        action = kwargs["action"]
        response = {"ok": True, "message": f"{action} accepted"}
        if action == "restart":
            response["newRunId"] = "wf_fake-completed"
        return response


class TuiTests(unittest.TestCase):
    def test_repository_reads_run_journal_and_live_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _fake_store(Path(tmp))
            repository = WorkflowRepository(store)
            workflows = repository.load()
            running = next(workflow for workflow in workflows if workflow.status == "running")
            hydrated = repository.hydrate_agent_activity(running, phase_index=0, agent_index=0)

        self.assertEqual(running.name, "dynamic-workflow-research")
        self.assertEqual([phase.title for phase in running.phases], ["Search", "Summarize"])
        self.assertEqual(hydrated.agents[0].activity[-2:], ('WebSearch({"query":"dynamic workflows"})', 'Read({"path":"paper.pdf"})'))
        self.assertEqual(running.agents[1].activity, ("Agent started",))

    def test_renders_claude_style_list_workflow_and_agent_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = WorkflowRepository(_fake_store(Path(tmp)))
            workflows = repository.load()
            run_index = next(index for index, workflow in enumerate(workflows) if workflow.status == "running")
            workflows[run_index] = repository.hydrate_agent_activity(
                workflows[run_index],
                phase_index=0,
                agent_index=0,
            )
            list_text = "\n".join(render_screen(workflows, RenderState(run_index=run_index), width=120, height=28))
            workflow_text = "\n".join(
                render_screen(
                    workflows,
                    RenderState(view="workflow", run_index=run_index),
                    width=120,
                    height=28,
                )
            )
            agent_text = "\n".join(
                render_screen(
                    workflows,
                    RenderState(view="agent", run_index=run_index),
                    width=120,
                    height=32,
                )
            )

        self.assertIn("Dynamic workflows", list_text)
        self.assertIn("1 running . 1 completed", list_text)
        self.assertIn("dynamic-workflow-research", list_text)
        self.assertIn("Phases", workflow_text)
        self.assertIn("Search . 2 agents", workflow_text)
        self.assertIn("search:claude-articles", workflow_text)
        self.assertIn("Prompt .", agent_text)
        self.assertIn("Activity . last 2 of 2", agent_text)
        self.assertIn("WebSearch", agent_text)
        self.assertIn("Still running...", agent_text)

    def test_controller_navigation_save_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _fake_store(Path(tmp))
            controller = TuiController(WorkflowRepository(store))
            controller.refresh()
            running_index = next(index for index, workflow in enumerate(controller.workflows) if workflow.status == "running")
            controller.state = replace(controller.state, run_index=running_index)

            controller.handle_key("enter")
            self.assertEqual(controller.state.view, "workflow")
            controller.handle_key("enter")
            self.assertEqual(controller.state.view, "agent")
            controller.handle_key("down")
            self.assertEqual(controller.state.agent_index, 1)
            controller.handle_key("esc")
            self.assertEqual(controller.state.view, "workflow")
            controller.handle_key("s")
            self.assertIn("Saved to", controller.state.message)
            self.assertTrue((store.exports_dir / f"{controller.current_run.run_id}.md").is_file())

            record = store.load_run("wf_fake-running")
            assert record is not None
            record["status"] = "completed"
            record["workflow"]["agents"][0]["status"] = "done"
            record["workflow"]["agents"][0]["tokens"] = 44846
            record["workflow"]["totals"] = {
                "agents": 2,
                "done": 1,
                "running": 1,
                "tokens": 56846,
                "tool_calls": 15,
            }
            store.save_run(record)
            controller.refresh()

            self.assertEqual(controller.current_run.run_id, "wf_fake-running")
            self.assertEqual(controller.current_run.status, "completed")
            self.assertEqual(controller.current_run.tokens, 56846)

    def test_rendered_panels_handle_wide_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflows = WorkflowRepository(_fake_store(Path(tmp))).load()
            lines = render_screen(
                workflows,
                RenderState(view="workflow"),
                width=88,
                height=24,
            )

        self.assertTrue(all(_display_width(line) <= 88 for line in lines))

    def test_non_tty_command_prints_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fake_store(Path(tmp))
            env = dict(os.environ)
            env["HERMES_DYNAMIC_WORKFLOWS_HOME"] = tmp
            result = subprocess.run(
                [sys.executable, "-m", "hermes_dynamic_workflows.tui.app"],
                cwd=Path(__file__).resolve().parent.parent,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("Dynamic workflows", result.stdout)
        self.assertIn("dynamic-workflow-research", result.stdout)

    def test_jsonl_reader_caches_stable_files_and_reads_bounded_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.jsonl"
            path.write_text(
                "\n".join(json.dumps({"index": index, "text": "x" * 80}) for index in range(20))
                + "\n",
                encoding="utf-8",
            )
            reader = _JsonlTailReader(max_bytes=300)
            first = reader.read(path)
            second = reader.read(path)
            self.assertIs(first, second)
            self.assertLess(len(first), 20)
            self.assertEqual(first[-1]["index"], 19)

            path.write_text(json.dumps({"index": 99}) + "\n", encoding="utf-8")
            third = reader.read(path)

        self.assertIsNot(third, first)
        self.assertEqual(third, [{"index": 99}])

    def test_transcript_activity_is_loaded_only_for_selected_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = WorkflowRepository(_fake_store(Path(tmp)))
            with patch(
                "hermes_dynamic_workflows.tui.model._read_transcript_activity",
                return_value=["Read(file.py)"],
            ) as read_activity:
                workflows = repository.load()
                read_activity.assert_not_called()
                running = next(workflow for workflow in workflows if workflow.status == "running")
                hydrated = repository.hydrate_agent_activity(running, phase_index=0, agent_index=0)

        read_activity.assert_called_once()
        self.assertEqual(hydrated.agents[0].activity, ("Read(file.py)",))

    def test_controller_sends_stop_pause_resume_and_restart_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = FakeControlClient()
            store = _fake_store(Path(tmp))
            controller = TuiController(WorkflowRepository(store, control_client=control))
            controller.refresh()
            running_index = next(index for index, item in enumerate(controller.workflows) if item.status == "running")
            controller.state = replace(controller.state, run_index=running_index)

            controller.handle_key("p")
            controller.handle_key("x")
            controller.handle_key("r")

            record = store.load_run("wf_fake-running")
            assert record is not None
            record["status"] = "paused"
            store.save_run(record)
            controller.refresh()
            paused_index = next(index for index, item in enumerate(controller.workflows) if item.run_id == "wf_fake-running")
            controller.state = replace(controller.state, run_index=paused_index)
            controller.handle_key("p")

        self.assertEqual([request["action"] for request in control.requests], ["pause", "stop", "restart", "resume"])
        self.assertEqual(control.requests[0]["owner"], "fake-control-owner")
        self.assertIn("resume accepted", controller.state.message)


def _fake_store(root: Path) -> WorkflowStore:
    store = WorkflowStore(root)
    transcript_dir = root / "projects" / "-fake-project" / "fake-session" / "subagents" / "workflows" / "wf_fake-running"
    transcript_dir.mkdir(parents=True)
    journal = transcript_dir / "journal.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps({"type": "started", "agentId": "1"}),
                json.dumps({"type": "started", "agentId": "2"}),
                json.dumps({"type": "result", "agentId": "3", "result": "summary"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    transcript = transcript_dir / "agent-search-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "WebSearch",
                                        "arguments": '{"query":"dynamic workflows"}',
                                    }
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"path": "paper.pdf"},
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    running = {
        "runId": "wf_fake-running",
        "taskId": "wgfake01",
        "controlOwner": "fake-control-owner",
        "status": "running",
        "createdAt": (datetime.now(timezone.utc) - timedelta(seconds=36)).isoformat(),
        "startedAt": (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat(),
        "summary": "并行搜集 Claude dynamic workflow 文章和相关学术论文，最后汇总",
        "journalFile": str(journal),
        "transcriptDir": str(transcript_dir),
        "workflow": {
            "meta": {
                "name": "dynamic-workflow-research",
                "description": "并行搜集 Claude dynamic workflow 文章和相关学术论文，最后汇总",
            },
            "phases": [{"title": "Search"}, {"title": "Summarize"}],
            "current_phase": "Search",
            "duration_seconds": 35,
            "agents": [
                {
                    "id": 1,
                    "label": "search:claude-articles",
                    "phase": "Search",
                    "status": "running",
                    "prompt": "搜索 Claude Code dynamic workflow 官方文章并提炼关键内容。",
                    "model": "Sonnet 4.6",
                    "tokens": 12100,
                    "tool_calls": 7,
                    "transcript_path": str(transcript),
                },
                {
                    "id": 2,
                    "label": "search:academic-papers",
                    "phase": "Search",
                    "status": "running",
                    "prompt": "搜索 dynamic workflow 理论相关论文。",
                    "model": "Sonnet 4.6",
                    "tokens": 11000,
                    "tool_calls": 6,
                },
            ],
            "children": [],
            "errors": [],
            "totals": {
                "agents": 2,
                "done": 0,
                "running": 2,
                "tokens": 23100,
                "tool_calls": 13,
            },
        },
    }
    completed = {
        "runId": "wf_fake-completed",
        "taskId": "wgfake02",
        "controlOwner": "fake-control-owner",
        "status": "completed",
        "createdAt": "2026-06-05T00:00:00+00:00",
        "startedAt": "2026-06-05T00:00:00+00:00",
        "finishedAt": "2026-06-05T00:02:15+00:00",
        "summary": "完成的研究 workflow",
        "workflow": {
            "meta": {"name": "completed-research", "description": "完成的研究 workflow"},
            "phases": [{"title": "Search"}],
            "duration_seconds": 135,
            "agents": [
                {
                    "id": 3,
                    "label": "synthesis",
                    "phase": "Search",
                    "status": "done",
                    "prompt": "汇总",
                    "result_preview": "已完成汇总。",
                    "tokens": 44846,
                    "tool_calls": 22,
                }
            ],
            "children": [],
            "errors": [],
            "totals": {
                "agents": 1,
                "done": 1,
                "running": 0,
                "tokens": 44846,
                "tool_calls": 22,
            },
        },
    }
    store.save_run(completed)
    store.save_run(running)
    return store


if __name__ == "__main__":
    unittest.main()
