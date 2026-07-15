"""Tests for orphan reaping + auto-resume (crash recovery).

A workflow run executes inside the Hermes process that launched it. If that
process exits (e.g. `hermes gateway restart`) the run thread is killed and its
record is frozen at an active status forever. These tests pin the three fixes:

  1. reap_orphans() flips such runs to "interrupted" (dead PID, or stale).
  2. The reaper harvests completed agent results from the journal into
     agentCache so a resume reuses them (the keystone making resume cheap).
  3. auto_resume_orphans() relaunches fresh orphans only when enabled + in
     scope, and is a strict no-op by default.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.core.types import ChildAgentRequest, ChildAgentRunner
from hermes_dynamic_workflows.engine.cache import ResumeCache, is_cache_miss
from hermes_dynamic_workflows.run.manager import (
    WorkflowRunManager,
    _harvest_journal_cache,
    _last_activity_epoch,
    _owner_pid,
    _pid_alive,
)
from hermes_dynamic_workflows.storage.store import WorkflowStore, utc_now_iso


DEAD_PID = 2_000_000_000  # implausibly high; not a live process
ACTIVE = ("queued", "running", "paused", "stopping")


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write_run(
    store: WorkflowStore,
    run_id: str,
    *,
    status: str = "running",
    owner: str | None = f"{DEAD_PID}-deadbeef0000",
    created_epoch: float | None = None,
    journal_events: list[dict] | None = None,
    agent_cache: dict | None = None,
    script: str | None = None,
    cwd: str = "/tmp/proj",
    session_id: str = "sess-1",
    session_context: dict | None = None,
    args=None,
    token_budget=None,
) -> dict:
    """Write a synthetic run record (+ optional journal/script) to the store."""
    created = created_epoch if created_epoch is not None else time.time()
    transcript_dir = store.transcript_dir(cwd, session_id, run_id)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    journal_path = transcript_dir / "journal.jsonl"
    if journal_events is not None:
        with journal_path.open("w", encoding="utf-8") as handle:
            for event in journal_events:
                handle.write(json.dumps(event) + "\n")
    else:
        journal_path.touch()

    script_path = None
    if script is not None:
        script_path = store.save_workflow_script(
            cwd=cwd, session_id=session_id, run_id=run_id, name="orphan-test", script=script
        )

    record = {
        "runId": run_id,
        "taskId": f"task-{run_id}",
        "status": status,
        "createdAt": _iso(created),
        "startedAt": _iso(created),
        "finishedAt": None,
        "cwd": cwd,
        "workflowSessionId": session_id,
        "controlOwner": owner,
        "scriptPath": str(script_path) if script_path else "",
        "transcriptDir": str(transcript_dir),
        "journalFile": str(journal_path),
        "summary": "synthetic orphan",
        "source": {"type": "script", "ref": "inline"},
        "resumeFromRunId": None,
        "args": args,
        "tokenBudget": token_budget,
        "sessionContext": session_context,
        "result": None,
        "error": None,
        "agentCache": agent_cache if agent_cache is not None else {},
    }
    # Backdate the journal mtime so staleness reflects created_epoch.
    if journal_events is not None or created_epoch is not None:
        os.utime(journal_path, (created, created))
    store.save_run(record)
    return record


class HelperTests(unittest.TestCase):
    def test_owner_pid_parses_pid_prefix(self):
        self.assertEqual(_owner_pid("49768-6298be3b5f6a"), 49768)
        self.assertIsNone(_owner_pid(""))
        self.assertIsNone(_owner_pid(None))
        self.assertIsNone(_owner_pid("not-a-pid"))
        self.assertIsNone(_owner_pid("-12345"))  # empty head before first dash

    def test_pid_alive(self):
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(DEAD_PID))
        self.assertFalse(_pid_alive(0))
        self.assertFalse(_pid_alive(-5))

    def test_harvest_journal_cache_collects_result_events_by_fingerprint(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            record = _write_run(
                store,
                "wf_harvest1-aaa",
                journal_events=[
                    {"type": "started", "key": "v2:fp1", "agentId": "1"},
                    {"type": "result", "key": "v2:fp1", "agentId": "1", "result": {"module": "engine"}},
                    {"type": "result", "key": "v2:fp2", "agentId": "2", "result": {"module": "run"}},
                    {"type": "error", "key": "v2:fp3", "agentId": "3", "error": "boom"},
                    {"type": "result", "key": "v2:fp4", "agentId": "4", "skipped": True, "result": None},
                ],
            )
            harvested = _harvest_journal_cache(record)

        # fp1, fp2 (results) and fp4 (skip -> None) harvested; fp3 (error) not.
        self.assertEqual(set(harvested), {"fp1", "fp2", "fp4"})
        self.assertEqual(harvested["fp1"], [{"module": "engine"}])
        self.assertEqual(harvested["fp2"], [{"module": "run"}])
        self.assertEqual(harvested["fp4"], [None])

    def test_harvest_tolerates_missing_or_corrupt_journal(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            record = _write_run(store, "wf_corrupt1-bbb")
            Path(record["journalFile"]).write_text("not json\n{partial", encoding="utf-8")
            self.assertEqual(_harvest_journal_cache(record), {})
            record["journalFile"] = "/nonexistent/journal.jsonl"
            self.assertEqual(_harvest_journal_cache(record), {})


class ReapOrphansTests(unittest.TestCase):
    def test_dead_pid_active_run_is_marked_interrupted(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig())
            _write_run(store, "wf_dead0001-ccc", status="running", owner=f"{DEAD_PID}-x")

            reaped = manager.reap_orphans()

            self.assertEqual(reaped, ["wf_dead0001-ccc"])
            final = store.load_run("wf_dead0001-ccc")
            self.assertEqual(final["status"], "interrupted")
            self.assertTrue(final["finishedAt"])
            self.assertIn("interrupted", str(final["error"]).lower())

    def test_live_pid_run_is_left_alone(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            # generous grace so staleness never kicks in for this fresh run
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=10_000))
            _write_run(store, "wf_live0001-ddd", status="running", owner=f"{os.getpid()}-x")

            self.assertEqual(manager.reap_orphans(), [])
            self.assertEqual(store.load_run("wf_live0001-ddd")["status"], "running")

    def test_terminal_runs_are_never_reaped(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig())
            for status in ("completed", "failed", "stopped", "interrupted"):
                _write_run(store, f"wf_term{status[:3]}-eee", status=status, owner=f"{DEAD_PID}-x")

            self.assertEqual(manager.reap_orphans(), [])

    def test_live_pid_is_never_stale_reaped(self):
        # SAFETY INVARIANT (reviewer BLOCK fix): a parseable, LIVE owner PID is
        # never reaped, even when idle past grace. A concurrent gateway can sit
        # inside a long child-agent call with no recent journal event; stale-
        # reaping it would clobber a genuinely live run. We give up the rare
        # PID-recycling case rather than risk that false positive.
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=60))
            _write_run(
                store,
                "wf_stale001-fff",
                status="running",
                owner=f"{os.getpid()}-x",  # alive PID
                created_epoch=time.time() - 3600,  # 1h idle, well past grace
            )

            self.assertEqual(manager.reap_orphans(), [])
            self.assertEqual(store.load_run("wf_stale001-fff")["status"], "running")

    def test_staleness_backstop_reaps_when_owner_unparseable(self):
        # No parseable owner PID -> liveness unknowable -> staleness is the only
        # signal. A stale-past-grace run with a junk owner is reaped.
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=60))
            _write_run(
                store,
                "wf_noowner1-fff",
                status="running",
                owner="not-a-pid",
                created_epoch=time.time() - 3600,
            )

            self.assertEqual(manager.reap_orphans(), ["wf_noowner1-fff"])
            self.assertEqual(store.load_run("wf_noowner1-fff")["status"], "interrupted")

    def test_fresh_unparseable_owner_is_not_stale_reaped(self):
        # No parseable owner but still within grace -> not reaped.
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=10_000))
            _write_run(store, "wf_freshno1-fff", status="running", owner="not-a-pid")

            self.assertEqual(manager.reap_orphans(), [])
            self.assertEqual(store.load_run("wf_freshno1-fff")["status"], "running")

    def test_paused_run_with_live_pid_is_not_stale_reaped(self):
        # A paused run is intentionally idle; staleness must not reap it while
        # its owner is alive (only a dead PID reaps a paused run).
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=60))
            _write_run(
                store,
                "wf_paused01-ggg",
                status="paused",
                owner=f"{os.getpid()}-x",
                created_epoch=time.time() - 3600,
            )

            self.assertEqual(manager.reap_orphans(), [])
            self.assertEqual(store.load_run("wf_paused01-ggg")["status"], "paused")

    def test_paused_run_with_dead_pid_is_reaped(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig(orphan_grace_seconds=10_000))
            _write_run(store, "wf_pausdd01-hhh", status="paused", owner=f"{DEAD_PID}-x")

            self.assertEqual(manager.reap_orphans(), ["wf_pausdd01-hhh"])

    def test_reap_harvests_journal_into_agentcache_for_cheap_resume(self):
        # The keystone: a hard-killed run has results in its journal but an
        # empty agentCache. After reaping, the cache holds those results and a
        # fresh ResumeCache returns them (so resume would skip those agents).
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig())
            _write_run(
                store,
                "wf_harvest2-iii",
                status="running",
                owner=f"{DEAD_PID}-x",
                agent_cache={},  # empty, exactly like a crashed run
                journal_events=[
                    {"type": "result", "key": "v2:fpA", "agentId": "1", "result": "answerA"},
                    {"type": "result", "key": "v2:fpB", "agentId": "2", "result": "answerB"},
                ],
            )

            manager.reap_orphans()
            final = store.load_run("wf_harvest2-iii")

        self.assertEqual(set(final["agentCache"]), {"fpA", "fpB"})
        cache = ResumeCache.from_run(final)
        self.assertEqual(cache.get("fpA"), "answerA")
        self.assertEqual(cache.get("fpB"), "answerB")
        self.assertTrue(is_cache_miss(cache.get("fpZ")))

    def test_run_owned_by_this_live_manager_is_skipped(self):
        # A run still tracked in self._runs is owned by THIS live process and
        # must never be reaped, even with a dead-looking owner string on disk.
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig())
            _write_run(store, "wf_owned001-jjj", status="running", owner=f"{DEAD_PID}-x")
            # Simulate live ownership.
            manager._runs["wf_owned001-jjj"] = object()  # type: ignore[assignment]

            self.assertEqual(manager.reap_orphans(), [])
            self.assertEqual(store.load_run("wf_owned001-jjj")["status"], "running")


class _StubRunner(ChildAgentRunner):
    def run(self, request: ChildAgentRequest):
        return f"ran:{request.label}"


class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        # Auto-resume is gateway-only: it no-ops unless a gateway loop is
        # present (so a short-lived CLI/tool process can't relaunch runs onto
        # daemon threads and kill them on exit). These tests exercise the
        # resume LOGIC, so simulate "inside a gateway" by default. The
        # gateway-gate itself is covered by test_auto_resume_requires_gateway.
        self._loop_patcher = patch(
            "hermes_dynamic_workflows.run.manager._gateway_loop_present",
            return_value=True,
        )
        self._loop_patcher.start()

    def tearDown(self):
        self._loop_patcher.stop()

    def test_auto_resume_requires_gateway_loop(self):
        # Without a gateway loop (the CLI/tool-process case), auto-resume must
        # be a no-op even when enabled and the orphan is in scope.
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store,
                config=PluginConfig(auto_resume_on_boot=True, require_launch_approval=False),
            )
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn "done"\n'
            _write_run(store, "wf_nogw0001-kkk", status="interrupted", script=script)

            with patch(
                "hermes_dynamic_workflows.run.manager._gateway_loop_present",
                return_value=False,
            ):
                self.assertEqual(manager.auto_resume_orphans(["wf_nogw0001-kkk"]), [])

    def test_auto_resume_disabled_by_default_is_noop(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(store=store, config=PluginConfig())  # default OFF
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn "done"\n'
            _write_run(store, "wf_resume01-kkk", status="interrupted", script=script)

            self.assertEqual(manager.auto_resume_orphans(["wf_resume01-kkk"]), [])

    def test_auto_resume_enabled_relaunches_with_resume_from_run_id(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store,
                config=PluginConfig(auto_resume_on_boot=True, require_launch_approval=False),
            )
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn await agent("go", {"label": "w", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "medium", "maxTurns": 10, "maxToolCalls": 16, "maxToolOutputChars": 200000})\n'
            _write_run(store, "wf_resume02-lll", status="interrupted", script=script)

            captured = {}
            real_start = manager.start_from_params

            def spy(params, **kwargs):
                captured["params"] = params
                captured["kwargs"] = kwargs
                return real_start(params, **kwargs)

            with patch.object(manager, "start_from_params", side_effect=spy):
                with patch(
                    "hermes_dynamic_workflows.child.runner.HermesChildAgentRunner",
                    return_value=_StubRunner(),
                ):
                    started = manager.auto_resume_orphans(["wf_resume02-lll"])
                    for run_id in started:
                        manager.wait(run_id, timeout=3)

            self.assertEqual(len(started), 1)
            self.assertEqual(captured["params"].get("resumeFromRunId"), "wf_resume02-lll")
            self.assertTrue(captured["kwargs"].get("launch_approved"))

    def test_auto_resume_skips_out_of_window_orphans(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store,
                config=PluginConfig(auto_resume_on_boot=True, auto_resume_window_seconds=3600),
            )
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn "done"\n'
            _write_run(
                store,
                "wf_resume03-mmm",
                status="interrupted",
                script=script,
                created_epoch=time.time() - 86_400,  # 24h old, window is 1h
            )

            self.assertEqual(manager.auto_resume_orphans(["wf_resume03-mmm"]), [])

    def test_auto_resume_respects_max_cap(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store,
                config=PluginConfig(auto_resume_on_boot=True, auto_resume_max=1, require_launch_approval=False),
            )
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn "done"\n'
            ids = []
            for i in range(3):
                rid = f"wf_resumec{i}-nnn"
                _write_run(store, rid, status="interrupted", script=script)
                ids.append(rid)

            calls = []
            real_start = manager.start_from_params

            def spy(params, **kwargs):
                calls.append(params.get("resumeFromRunId"))
                return real_start(params, **kwargs)

            with patch.object(manager, "start_from_params", side_effect=spy):
                with patch(
                    "hermes_dynamic_workflows.child.runner.HermesChildAgentRunner",
                    return_value=_StubRunner(),
                ):
                    started = manager.auto_resume_orphans(ids)
                    for run_id in started:
                        manager.wait(run_id, timeout=3)

            self.assertEqual(len(started), 1)  # capped at 1
            self.assertEqual(len(calls), 1)

    def test_auto_resume_skips_missing_script(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store, config=PluginConfig(auto_resume_on_boot=True)
            )
            # no script written -> scriptPath empty -> skipped
            _write_run(store, "wf_resume04-ooo", status="interrupted", script=None)

            self.assertEqual(manager.auto_resume_orphans(["wf_resume04-ooo"]), [])

    def test_auto_resume_skips_non_interrupted(self):
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store, config=PluginConfig(auto_resume_on_boot=True)
            )
            script = 'meta = {"name": "orphan-test", "description": "x"}\nreturn "done"\n'
            _write_run(store, "wf_resume05-ppp", status="running", script=script)

            self.assertEqual(manager.auto_resume_orphans(["wf_resume05-ppp"]), [])


class SessionContextPersistenceTests(unittest.TestCase):
    def test_session_context_round_trips_on_record(self):
        ctx = {
            "platform": "telegram",
            "chat_id": "-100123",
            "thread_id": "456",
            "user_id": "789",
        }
        with TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp))
            manager = WorkflowRunManager(
                store=store, config=PluginConfig(require_launch_approval=False)
            )
            script = 'meta = {"name": "ctx-test", "description": "x"}\nreturn "ok"\n'
            with patch(
                "hermes_dynamic_workflows.child.runner.HermesChildAgentRunner",
                return_value=_StubRunner(),
            ):
                rec = manager.start_from_params(
                    {"script": script},
                    cwd=tmp,
                    host_session_id="sess-ctx",
                    session_context_override=ctx,
                )
                manager.wait(rec["runId"], timeout=2)

            persisted = store.load_run(rec["runId"])
        self.assertEqual(persisted["sessionContext"], ctx)


if __name__ == "__main__":
    unittest.main()
