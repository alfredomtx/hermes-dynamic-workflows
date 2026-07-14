"""Background workflow run manager."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..engine.cache import ResumeCache
from ..core.config import PluginConfig, load_config
from ..engine.context import PauseGate
from ..core.errors import (
    WorkflowLaunchDenied,
    WorkflowRuntimeError,
    WorkflowStopped,
    WorkflowToolUseError,
)
from ..engine.sandbox import extract_meta, parse_script
from ..core.token_budget import parse_token_budget
from ..storage.store import (
    WorkflowStore,
    new_run_id,
    new_task_id,
    resolve_workflow_source,
    sanitize_filename,
    utc_now_iso,
)
from ..storage.control import ControlListener, new_control_owner
from ..view.completion import (
    content_from_value,
    is_intentional_stop_record,
    render_completion_card,
)
from ..view.render import (
    _current_phase,
    _RUNNING_STATES,
    render_agent_overview,
    render_run_progress,
    render_saved_markdown,
    render_workflow_text,
)
from ..engine.runtime import WorkflowOptions, run_workflow
from .transcripts import (
    LiveTranscriptExporter,
    _export_child_transcripts,
    _agent_session_id,
    _is_active_agent_snapshot,
    _agent_transcript_path,
    _agent_meta_path,
    _agent_transcript_metadata,
    _append_unique,
    _iter_agent_snapshots,
)


# Max seconds _notify_completion waits for an in-flight seed send to resolve its
# message id before handing completion to the seed's done-callback. Module-level
# so tests can shrink it to exercise the slow-seed path without a 15s sleep.
_SEED_RESOLVE_WAIT_SECONDS = 15.0


@dataclass
class ManagedRun:
    run_id: str
    stop_event: threading.Event
    pause_gate: PauseGate
    record: dict[str, Any]
    thread: threading.Thread | None = None
    child_runner: Any = None
    plugin_context: Any = None
    session_context: dict[str, str] | None = None
    approval_callback: Any = field(default=None, repr=False)
    parent_runtime: dict[str, Any] | None = field(default=None, repr=False)
    transcript_exporter: "LiveTranscriptExporter | None" = None
    # --- Live progress bubble (gateway only) ----------------------------
    # Seeded once at launch with the message id of the posted progress bubble,
    # then edited in place on meaningful state changes and finalized on
    # completion. ``progress_active`` stays False when there is no gateway
    # context / no edit-capable adapter / the seed send failed, in which case
    # the run falls back to the separate launch + completion markers. The edit
    # target (adapter/loop/chat) is re-resolved per edit from the session
    # context rather than cached here, so only the bubble's message id and
    # throttle bookkeeping live on the run.
    #
    # ``progress_requested`` is set synchronously at launch (before the worker
    # thread starts) the moment we decide to seed a bubble; ``progress_active``
    # + ``progress_message_id`` are set later by the seed send's done-callback
    # once the message id is known. Completion waits for that id (bounded) so a
    # fast run can't finalize before the seed resolves and leave a stuck bubble.
    progress_requested: bool = False
    progress_active: bool = False
    progress_message_id: str | None = None
    progress_last_edit_ts: float = 0.0
    progress_last_text: str | None = field(default=None, repr=False)
    # Guard: the gateway agent-loop wake event (completion_queue) must be
    # enqueued at most once per run. Set under ``lock`` by _enqueue_gateway_wake_event.
    _wake_event_enqueued: bool = field(default=False, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock)


class WorkflowRunManager:
    def __init__(
        self,
        store: WorkflowStore | None = None,
        config: PluginConfig | None = None,
        *,
        enable_control: bool = False,
    ):
        self.store = store or WorkflowStore()
        self._static_config = config is not None
        self.config = config or load_config()
        self.control_owner = new_control_owner()
        self._runs: dict[str, ManagedRun] = {}
        self._lock = threading.RLock()
        self._control_listener: ControlListener | None = None
        if enable_control:
            self.start_control_listener()

    def start_control_listener(self) -> bool:
        with self._lock:
            if self._control_listener is not None:
                return True
            listener = ControlListener(
                store=self.store,
                owner=self.control_owner,
                handler=self._handle_control_request,
            )
            self._control_listener = listener
        try:
            listener.start()
        except Exception:
            with self._lock:
                if self._control_listener is listener:
                    self._control_listener = None
            return False
        return True

    def stop_control_listener(self) -> None:
        with self._lock:
            listener = self._control_listener
            self._control_listener = None
        if listener is not None:
            listener.stop()

    def start_from_params(
        self,
        params: dict[str, Any],
        *,
        cwd: str | None = None,
        plugin_context: Any = None,
        parent_agent: Any = None,
        host_session_id: str | None = None,
        user_task: str | None = None,
        launch_approved: bool = False,
        restart_from_run_id: str | None = None,
        token_budget_total_override: int | None = None,
        session_context_override: dict[str, str] | None = None,
        approval_callback_override: Any = None,
        parent_runtime_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = self.config if self._static_config else load_config()
        self.config = config
        cwd_value = cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()
        source = resolve_workflow_source(params, store=self.store, cwd=cwd)
        meta = extract_meta(parse_script(source.script, config))
        resume_from = str(params.get("resumeFromRunId") or "").strip() or None
        active_resume = self._active_resume_run(resume_from) if resume_from else None
        if active_resume:
            active_task_id = str(active_resume.get("taskId") or "")
            raise WorkflowToolUseError(
                f"Workflow {resume_from} is still running (task {active_task_id}). "
                f'Stop it first with task_stop({{"task_id":"{active_task_id}"}}) '
                "before resuming."
            )
        approved, reason = (True, "") if launch_approved else _approve_launch(meta, config, plugin_context)
        if not approved:
            raise WorkflowLaunchDenied(
                f'Workflow "{meta.get("name") or "workflow"}" was not launched: {reason}. '
                "Do not retry; tell the user it needs their approval."
            )
        run_id = new_run_id()
        task_id = new_task_id()
        workflow_session_id = _resolve_workflow_session_id(
            plugin_context,
            host_session_id=host_session_id,
        )
        saved_path = self._script_path_for_source(
            source,
            run_id=run_id,
            session_id=workflow_session_id,
            cwd=cwd_value,
            meta=meta,
        )
        transcript_dir = self.store.transcript_dir(cwd_value, workflow_session_id, run_id)
        journal_path = transcript_dir / "journal.jsonl"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        journal_path.touch(exist_ok=True)
        previous = self.store.load_run(resume_from) if resume_from else None
        resume_cache = ResumeCache.from_run(previous)
        args = params["args"] if "args" in params else None
        token_budget = (
            token_budget_total_override
            if token_budget_total_override is not None
            else parse_token_budget(user_task)
        )
        # Captured in the launching (parent) context, which carries the gateway
        # session vars when the run is started from a gateway session.
        session_context = (
            session_context_override
            if session_context_override is not None
            else _capture_gateway_session_context()
        )
        approval_callback = (
            approval_callback_override
            if approval_callback_override is not None
            else _capture_cli_approval_callback()
        )
        parent_runtime = (
            dict(parent_runtime_override)
            if parent_runtime_override is not None
            else _capture_parent_runtime(parent_agent, plugin_context=plugin_context)
        )

        stop_event = threading.Event()
        pause_gate = PauseGate()
        record = {
            "runId": run_id,
            "taskId": task_id,
            "status": "queued",
            "createdAt": utc_now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "cwd": cwd_value,
            "workflowSessionId": workflow_session_id,
            "controlOwner": self.control_owner if self._control_listener is not None else None,
            "scriptPath": str(saved_path),
            "transcriptDir": str(transcript_dir),
            "journalFile": str(journal_path),
            "summary": meta.get("description") or meta.get("name") or "workflow",
            "source": {
                "type": source.source_type,
                "ref": source.source_ref,
            },
            "resumeFromRunId": resume_from,
            "restartedFromRunId": restart_from_run_id,
            "args": args,
            "tokenBudget": token_budget,
            # Routing-only gateway context (platform/chat/thread/user — never
            # credentials; parent_runtime with secrets stays off-record on the
            # in-memory ManagedRun). Persisted so a run reaped+resumed in a
            # later process can still route its completion message to the
            # originating chat. None outside a gateway session.
            "sessionContext": session_context,
            "result": None,
            "error": None,
            "display": "",
            "workflow": None,
            "agentCache": {},
            "outputFile": None,
            "transcriptFiles": [],
            "transcriptMetaFiles": [],
        }
        managed = ManagedRun(
            run_id=run_id,
            stop_event=stop_event,
            pause_gate=pause_gate,
            record=record,
            plugin_context=plugin_context,
            session_context=session_context,
            approval_callback=approval_callback,
            parent_runtime=parent_runtime,
        )

        with self._lock:
            self._runs[run_id] = managed
        self.store.save_run(record)

        thread = threading.Thread(
            target=self._run_thread,
            args=(managed, source.script, args, config, resume_cache, cwd, plugin_context, token_budget, session_context),
            name=f"workflow-{run_id}",
            daemon=True,
        )
        managed.thread = thread
        # Gateway-only live progress: seed an edited-in-place bubble if enabled
        # and the run came from a gateway session with an edit-capable adapter.
        # When the bubble seeds, it subsumes the separate "started" marker (one
        # evolving message instead of two); otherwise fall back to the marker.
        # Both are best-effort and never raise into the launch.
        #
        # Seed BEFORE starting the worker thread: _seed_progress_bubble sets
        # progress_requested synchronously, so a sub-second run can never reach
        # _notify_completion (and skip the bubble wait) before the bubble has
        # been requested. The send itself is async/fire-and-forget, so seeding
        # first adds no latency to the worker.
        seeded = False
        try:
            seeded = _seed_progress_bubble(managed, config)
        except Exception:
            seeded = False
        if not seeded:
            try:
                _notify_launch(record, config, session_context)
            except Exception:
                pass
        thread.start()
        return self._public_record(record)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            managed = self._runs.get(run_id)
        if managed:
            with managed.lock:
                return self._public_record(dict(managed.record))
        record = self.store.load_run(run_id)
        return self._public_record(record) if record else None

    def get_by_task_id(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            managed_runs = list(self._runs.values())
        for managed in managed_runs:
            with managed.lock:
                if str(managed.record.get("taskId") or "") == str(task_id):
                    return self._public_record(dict(managed.record))
        record = self.store.find_run_by_task_id(str(task_id))
        return self._public_record(record) if record else None

    def _active_resume_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            managed = self._runs.get(run_id)
        if not managed:
            return None
        with managed.lock:
            if managed.record.get("status") in {"queued", "running", "paused", "stopping"}:
                return self._public_record(dict(managed.record))
        return None

    def stop_task(self, task_id: str) -> dict[str, Any] | None:
        """Stop an active background workflow by its task id.

        This intentionally only checks live managed runs. Historical runs in
        state are not stoppable tasks, so stopping a completed or already
        stopped task should return a "No task found" result.
        """
        wanted = str(task_id or "")
        if not wanted:
            return None
        with self._lock:
            managed_runs = list(self._runs.values())
        for managed in managed_runs:
            child_runner = None
            with managed.lock:
                record = managed.record
                if str(record.get("taskId") or "") != wanted:
                    continue
                if record.get("status") not in {"queued", "running", "paused"}:
                    return None
                managed.stop_event.set()
                managed.pause_gate.resume()
                child_runner = managed.child_runner
                record["status"] = "stopping"
                self.store.save_run(record)
                summary = str(record.get("summary") or record.get("runId") or wanted)
                result = {
                    "message": f"Successfully stopped task: {wanted} ({summary})",
                    "task_id": wanted,
                    "task_type": "local_workflow",
                }
            if child_runner is not None and hasattr(child_runner, "interrupt_all"):
                try:
                    child_runner.interrupt_all()
                except Exception:
                    pass
            return result
        return None

    def skip_agent(self, task_id: str, child_task_id: str) -> bool:
        """Skip one active child agent without stopping its workflow run."""
        wanted = str(task_id or "")
        child_wanted = str(child_task_id or "")
        if not wanted or not child_wanted:
            return False
        with self._lock:
            managed_runs = list(self._runs.values())
        for managed in managed_runs:
            with managed.lock:
                if str(managed.record.get("taskId") or "") != wanted:
                    continue
                if managed.record.get("status") not in {"queued", "running", "paused"}:
                    return False
                runner = managed.child_runner
            if runner is None or not hasattr(runner, "skip_child"):
                return False
            return bool(runner.skip_child(child_wanted))
        return False

    def list(self, limit: int = 20, *, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            self._public_record(run)
            for run in self.store.list_runs(limit=limit, session_id=session_id)
        ]

    # --- Orphan reaping + auto-resume ------------------------------------

    _ACTIVE_STATUSES = ("queued", "running", "paused", "stopping")

    def reap_orphans(self, *, now: float | None = None) -> list[str]:
        """Mark abandoned runs ``interrupted`` and harvest their cache.

        A run is an orphan when its record still holds an active status
        (queued/running/paused/stopping) but the process that owned it is gone:
        the run thread died with the process and never wrote a terminal state.
        Detection is two-pronged:

          1. The ``controlOwner`` PID is no longer alive (primary signal — a
             ``hermes gateway restart`` kills the old PID exactly this way).
          2. The run is stale past ``orphan_grace_seconds`` of journal
             inactivity while not paused (backstop for PID recycling, where a
             dead PID is reused by an unrelated process, and for records that
             never recorded a parseable owner).

        A run currently owned by THIS live manager (in ``self._runs``) or by
        another live process is never reaped. Before flipping to
        ``interrupted`` the completed child-agent results are harvested from
        the journal into ``agentCache`` (see ``_harvest_journal_cache``) so a
        later resume — manual or auto — reuses them instead of re-running.

        Returns the run ids reaped this call.
        """
        config = self.config if self._static_config else load_config()
        self.config = config
        clock = time.time() if now is None else now
        reaped: list[str] = []
        with self._lock:
            live_run_ids = set(self._runs)
        for record in self.store.list_runs(limit=10_000):
            run_id = str(record.get("runId") or "")
            if not run_id or run_id in live_run_ids:
                continue
            if record.get("status") not in self._ACTIVE_STATUSES:
                continue
            if not self._is_orphan(record, clock, config):
                continue
            # Re-load under no lock contention; another process could have
            # finalized it between listing and now. Skip if it did.
            fresh = self.store.load_run(run_id)
            if not fresh or fresh.get("status") not in self._ACTIVE_STATUSES:
                continue
            self._mark_interrupted(fresh, clock)
            reaped.append(run_id)
        return reaped

    def _is_orphan(self, record: dict[str, Any], clock: float, config: PluginConfig) -> bool:
        owner = str(record.get("controlOwner") or "").strip()
        owner_pid = _owner_pid(owner)
        if owner_pid is not None:
            # A parseable owner PID is the reliable signal: a dead PID is a
            # definitive orphan (a gateway restart kills the old PID exactly
            # this way). A LIVE PID is never reaped — it may be a concurrent
            # gateway busy inside a long child-agent call with no recent
            # journal event, and stale-reaping it would clobber a live run.
            # We deliberately give up the rare PID-recycling case (an unrelated
            # process inherited the number) rather than risk a false positive;
            # the only cost is a stale record lingering until that PID frees.
            return not _pid_alive(owner_pid)
        # No parseable owner (older records, hand-edited data): liveness is
        # unknowable, so staleness is the only available signal. Paused runs
        # are intentionally idle, so never stale-reap them.
        if record.get("status") == "paused":
            return False
        idle = clock - _last_activity_epoch(record)
        return idle >= max(0.0, config.orphan_grace_seconds)

    def _mark_interrupted(self, record: dict[str, Any], clock: float) -> None:
        harvested = _harvest_journal_cache(record)
        if harvested:
            cache = record.get("agentCache")
            if not isinstance(cache, dict):
                cache = {}
            for fingerprint, results in harvested.items():
                cache.setdefault(fingerprint, []).extend(results)
            record["agentCache"] = cache
        record["status"] = "interrupted"
        record["finishedAt"] = utc_now_iso()
        record.setdefault("error", None)
        if not record.get("error"):
            record["error"] = (
                "Run interrupted: the Hermes process that owned it exited "
                "before it finished (e.g. a gateway restart)."
            )
        self.store.save_run(record)

    def auto_resume_orphans(
        self,
        reaped_run_ids: list[str],
        *,
        now: float | None = None,
    ) -> list[str]:
        """Relaunch freshly-reaped orphans (gated; off by default).

        Only the runs ``reap_orphans`` just marked ``interrupted`` this boot
        are candidates — never historical interrupted runs. Each candidate must
        also be recent (within ``auto_resume_window_seconds`` of its last
        activity) and have its script still on disk. At most
        ``auto_resume_max`` are revived per call. Each relaunch reuses the
        harvested cache via ``resumeFromRunId`` so completed agents are not
        re-run, and carries the persisted ``sessionContext`` so its completion
        message routes back to the originating chat.

        Returns the new run ids started.
        """
        config = self.config if self._static_config else load_config()
        self.config = config
        if not config.auto_resume_on_boot:
            return []
        # Only a process that actually hosts the gateway loop can keep a resumed
        # run alive and route its completion back to the originating chat. Any
        # process can build a manager (a short-lived CLI/tool invocation calls
        # get_run_manager() too); if such a process relaunched runs they would
        # execute on daemon threads and be killed again the instant it exits,
        # causing interrupt/resume churn and wasted spend. Reaping is universal
        # (safe, just bookkeeping); resuming is gateway-only.
        if not _gateway_loop_present():
            return []
        clock = time.time() if now is None else now
        started: list[str] = []
        for run_id in reaped_run_ids:
            if len(started) >= max(1, config.auto_resume_max):
                break
            record = self.store.load_run(run_id)
            if not record or record.get("status") != "interrupted":
                continue
            idle = clock - _last_activity_epoch(record)
            if idle > max(0.0, config.auto_resume_window_seconds):
                continue
            script_path = str(record.get("scriptPath") or "")
            if not script_path or not Path(script_path).is_file():
                continue
            try:
                new_record = self.start_from_params(
                    {"scriptPath": script_path, "resumeFromRunId": run_id, "args": record.get("args")},
                    cwd=str(record.get("cwd") or os.getcwd()),
                    host_session_id=str(record.get("workflowSessionId") or "") or None,
                    launch_approved=True,
                    token_budget_total_override=record.get("tokenBudget"),
                    session_context_override=record.get("sessionContext") or None,
                )
            except Exception:
                continue
            new_run_id = str(new_record.get("runId") or "")
            if new_run_id:
                started.append(new_run_id)
        return started

    def reap_and_maybe_resume(self) -> dict[str, list[str]]:
        """Boot entrypoint: reap orphans, then auto-resume if enabled.

        Reaping is always safe and fast (a few file reads/writes), so it runs
        synchronously. Auto-resume is gated by ``auto_resume_on_boot`` and only
        acts on the just-reaped runs.
        """
        reaped = self.reap_orphans()
        resumed = self.auto_resume_orphans(reaped) if reaped else []
        return {"reaped": reaped, "resumed": resumed}

    def stop(self, run_id: str) -> bool:
        with self._lock:
            managed = self._runs.get(run_id)
        if not managed:
            record = self.store.load_run(run_id)
            if not record or record.get("status") not in {"queued", "running", "paused"}:
                return False
            record["status"] = "stopped"
            record["finishedAt"] = utc_now_iso()
            self.store.save_run(record)
            return True

        with managed.lock:
            if managed.record.get("status") not in {"queued", "running", "paused"}:
                return False
            managed.stop_event.set()
            managed.pause_gate.resume()
            child_runner = managed.child_runner
            managed.record["status"] = "stopping"
            self.store.save_run(managed.record)
        if child_runner is not None and hasattr(child_runner, "interrupt_all"):
            try:
                child_runner.interrupt_all()
            except Exception:
                pass
        return True

    def pause(self, run_id: str) -> bool:
        with self._lock:
            managed = self._runs.get(run_id)
        if managed is None:
            return False
        with managed.lock:
            if managed.record.get("status") not in {"queued", "running"}:
                return False
            managed.pause_gate.pause()
            managed.record["status"] = "paused"
            managed.record["pausedAt"] = utc_now_iso()
            self.store.save_run(managed.record)
        try:
            _edit_progress_bubble(managed, self.config, completed=False, force=True)
        except Exception:
            pass
        return True

    def resume(self, run_id: str) -> bool:
        with self._lock:
            managed = self._runs.get(run_id)
        if managed is None:
            return False
        with managed.lock:
            if managed.record.get("status") != "paused":
                return False
            managed.pause_gate.resume()
            managed.record["status"] = "running"
            managed.record["resumedAt"] = utc_now_iso()
            self.store.save_run(managed.record)
        try:
            _edit_progress_bubble(managed, self.config, completed=False, force=True)
        except Exception:
            pass
        return True

    def restart(self, run_id: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if record is None:
            return None
        with self._lock:
            managed = self._runs.get(run_id)
        if managed is not None and record.get("status") in {"queued", "running", "paused", "stopping"}:
            self.stop(run_id)
            final = self.wait(run_id, timeout=5)
            if final and final.get("status") not in {"stopped", "completed", "failed", "error"}:
                raise WorkflowRuntimeError(f"workflow {run_id} did not stop before restart")

        script_path = str(record.get("scriptPath") or "")
        if not script_path or not Path(script_path).is_file():
            raise WorkflowRuntimeError(f"workflow script is unavailable for restart: {script_path}")
        params: dict[str, Any] = {"scriptPath": script_path}
        if "args" in record:
            params["args"] = record.get("args")
        return self.start_from_params(
            params,
            cwd=str(record.get("cwd") or os.getcwd()),
            plugin_context=managed.plugin_context if managed is not None else None,
            host_session_id=str(record.get("workflowSessionId") or "") or None,
            launch_approved=True,
            restart_from_run_id=run_id,
            token_budget_total_override=record.get("tokenBudget"),
            session_context_override=managed.session_context if managed is not None else None,
            approval_callback_override=managed.approval_callback if managed is not None else None,
            parent_runtime_override=managed.parent_runtime if managed is not None else None,
        )

    def _handle_control_request(self, request: dict[str, Any]) -> dict[str, Any]:
        run_id = str(request.get("runId") or "")
        action = str(request.get("action") or "")
        record = self.get(run_id)
        if record is None:
            return {"ok": False, "action": action, "runId": run_id, "message": f"Workflow run not found: {run_id}"}
        if str(record.get("controlOwner") or "") != self.control_owner:
            return {"ok": False, "action": action, "runId": run_id, "message": "Workflow is owned by another Hermes process."}
        expected = str(request.get("expectedStatus") or "")
        if expected and str(record.get("status") or "") != expected:
            return {
                "ok": False,
                "action": action,
                "runId": run_id,
                "status": record.get("status"),
                "message": f"Workflow status changed from {expected} to {record.get('status')}; retry the action.",
            }
        if action == "stop":
            ok = self.stop(run_id)
            message = f"Stop requested for {run_id}." if ok else f"Workflow {run_id} is not stoppable."
            return {"ok": ok, "action": action, "runId": run_id, "status": "stopping" if ok else record.get("status"), "message": message}
        if action == "pause":
            ok = self.pause(run_id)
            message = f"Paused {run_id}; running agents may finish." if ok else f"Workflow {run_id} is not pausable."
            return {"ok": ok, "action": action, "runId": run_id, "status": "paused" if ok else record.get("status"), "message": message}
        if action == "resume":
            ok = self.resume(run_id)
            message = f"Resumed {run_id}." if ok else f"Workflow {run_id} is not paused."
            return {"ok": ok, "action": action, "runId": run_id, "status": "running" if ok else record.get("status"), "message": message}
        if action in {"restart", "rerun"}:
            restarted = self.restart(run_id)
            if restarted is None:
                return {"ok": False, "action": action, "runId": run_id, "message": f"Workflow run not found: {run_id}"}
            new_run_id = str(restarted.get("runId") or "")
            return {
                "ok": True,
                "action": action,
                "runId": run_id,
                "newRunId": new_run_id,
                "status": restarted.get("status"),
                "message": f"Restarted {run_id} as {new_run_id}.",
            }
        return {"ok": False, "action": action, "runId": run_id, "message": f"Unsupported control action: {action}"}

    def format_agent_overview(self, limit: int = 12, *, session_id: str | None = None) -> str:
        runs = self.list(limit=limit, session_id=session_id)
        return render_agent_overview(runs)

    def _script_path_for_source(
        self,
        source,
        *,
        run_id: str,
        session_id: str,
        cwd: str,
        meta: dict[str, Any],
    ) -> Path:
        if source.source_type == "script":
            return self.store.save_workflow_script(
                cwd=cwd,
                session_id=session_id,
                run_id=run_id,
                name=str(meta.get("name") or "dynamic-workflow"),
                script=source.script,
            )
        if source.saved_script_path:
            return Path(source.saved_script_path)
        return self.store.save_workflow_script(
            cwd=cwd,
            session_id=session_id,
            run_id=run_id,
            name=str(meta.get("name") or "dynamic-workflow"),
            script=source.script,
        )

    def _load_run_script(self, run: dict[str, Any], run_id: str) -> str | None:
        candidates: list[Path] = []
        script_path = run.get("scriptPath")
        if script_path:
            candidates.append(Path(script_path))
        try:
            candidates.append(self.store.script_path(run_id))
        except Exception:
            pass
        for candidate in candidates:
            try:
                if candidate and candidate.is_file():
                    return candidate.read_text(encoding="utf-8")
            except OSError:
                continue
        return None

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            managed = self._runs.get(run_id)
        if managed and managed.thread:
            managed.thread.join(timeout=timeout)
        return self.get(run_id)

    def _run_thread(
        self,
        managed: ManagedRun,
        script: str,
        args: Any,
        config: PluginConfig,
        resume_cache: ResumeCache,
        cwd: str | None,
        plugin_context: Any,
        token_budget: int | None = None,
        session_context: dict[str, str] | None = None,
    ) -> None:
        try:
            from ..child.runner import HermesChildAgentRunner

            runner_kwargs = {
                "session_context": session_context,
                "approval_session_key": _workflow_approval_session_key(managed, session_context),
                "parent_runtime": managed.parent_runtime,
            }
            if managed.approval_callback is not None:
                runner_kwargs["approval_callback"] = managed.approval_callback
            managed.child_runner = HermesChildAgentRunner(config, **runner_kwargs)
            self._update(
                managed,
                status="paused" if managed.pause_gate.is_paused else "running",
                startedAt=utc_now_iso(),
            )
            result = run_workflow(
                script,
                WorkflowOptions(
                    args=args,
                    cwd=cwd or os.environ.get("TERMINAL_CWD") or os.getcwd(),
                    config=config,
                    child_runner=managed.child_runner,
                    stop_event=managed.stop_event,
                    pause_gate=managed.pause_gate,
                    resume_cache=resume_cache,
                    on_update=lambda state: self._update_state(managed, state, config),
                    on_journal=lambda event: self._append_journal_event(managed, event),
                    plugin_context=plugin_context,
                    token_budget_total=token_budget,
                    source_ref=str(managed.record.get("scriptPath") or ""),
                    store=self.store,
                ),
            )
            snapshot = result.state.snapshot()
            self._sync_live_child_transcripts(managed, snapshot)
            if managed.stop_event.is_set():
                status = "stopped"
            else:
                status = "completed"
            self._update(
                managed,
                status=status,
                finishedAt=utc_now_iso(),
                result=result.value,
                workflow=snapshot,
                display=render_workflow_text(snapshot, completed=True),
                agentCache=resume_cache.current,
            )
        except BaseException as exc:
            # BaseException so a WorkflowHalt (stop / deadline / hard limit),
            # which derives from BaseException, is recorded as the run's final
            # status instead of dying as an unhandled thread exception.
            status = "stopped" if managed.stop_event.is_set() else "failed"
            self._update(
                managed,
                status=status,
                finishedAt=utc_now_iso(),
                error=_runtime_error_text(exc),
                agentCache=resume_cache.current,
            )
        finally:
            self._stop_live_transcript_exporter(managed)
            self._finalize_completion_artifacts(managed)
            _notify_completion(managed, plugin_context, managed.record, config, managed.session_context)

    def _finalize_completion_artifacts(self, managed: ManagedRun) -> None:
        with managed.lock:
            record = managed.record
            try:
                _write_output_file(record, self.store)
            except Exception:
                pass
            try:
                _export_child_transcripts(record, self.store)
            except Exception as exc:
                record["transcriptExportError"] = f"{type(exc).__name__}: {exc}"
            self.store.save_run(record)

    def _update_state(self, managed: ManagedRun, state, config: PluginConfig | None = None) -> None:
        snapshot = state.snapshot()
        self._sync_live_child_transcripts(managed, snapshot)
        with managed.lock:
            old_signal = _progress_signal(managed.record)
            managed.record.update(
                workflow=snapshot,
                display=render_workflow_text(snapshot, completed=False),
            )
            signal_changed = old_signal != _progress_signal(managed.record)
            self.store.save_run(managed.record)
        # Mid-run edit of the live progress bubble (throttled + change-gated
        # inside the helper). No-op when no bubble is active.
        if config is not None and config.notify_progress:
            try:
                _edit_progress_bubble(managed, config, completed=False, force=signal_changed)
            except Exception:
                pass

    def _sync_live_child_transcripts(self, managed: ManagedRun, snapshot: dict[str, Any]) -> None:
        transcript_dir_raw = managed.record.get("transcriptDir")
        if not transcript_dir_raw:
            return
        transcript_dir = Path(str(transcript_dir_raw))
        transcript_dir.mkdir(parents=True, exist_ok=True)
        targets: list[dict[str, Any]] = []
        start_exporter = False
        with managed.lock:
            transcript_files = managed.record.setdefault("transcriptFiles", [])
            meta_files = managed.record.setdefault("transcriptMetaFiles", [])
            for agent in _iter_agent_snapshots(snapshot):
                session_id = _agent_session_id(agent)
                if not session_id:
                    continue
                path = _agent_transcript_path(transcript_dir, session_id)
                meta_path = _agent_meta_path(path)
                metadata = _agent_transcript_metadata(managed.record, agent, session_id)
                agent["transcript_path"] = str(path)
                agent["transcript_meta_path"] = str(meta_path)
                _append_unique(transcript_files, str(path))
                _append_unique(meta_files, str(meta_path))
                targets.append(
                    {
                        "session_id": session_id,
                        "transcript_path": path,
                        "meta_path": meta_path,
                        "metadata": metadata,
                        "active": _is_active_agent_snapshot(agent),
                    }
                )
            if targets and managed.transcript_exporter is None:
                managed.transcript_exporter = LiveTranscriptExporter(run_id=managed.run_id)
                start_exporter = True
            exporter = managed.transcript_exporter
        if exporter is None:
            return
        if start_exporter:
            try:
                exporter.start()
            except Exception as exc:
                with managed.lock:
                    managed.record["transcriptExportError"] = f"{type(exc).__name__}: {exc}"
        for target in targets:
            try:
                exporter.upsert(**target)
            except Exception as exc:
                with managed.lock:
                    managed.record["transcriptExportError"] = f"{type(exc).__name__}: {exc}"

    def _stop_live_transcript_exporter(self, managed: ManagedRun) -> None:
        with managed.lock:
            exporter = managed.transcript_exporter
        if exporter is None:
            return
        try:
            exporter.stop(final=True)
        except Exception as exc:
            with managed.lock:
                managed.record["transcriptExportError"] = f"{type(exc).__name__}: {exc}"

    def _update(self, managed: ManagedRun, **fields: Any) -> None:
        with managed.lock:
            managed.record.update(fields)
            self.store.save_run(managed.record)

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return dict(record)

    def _append_journal_event(self, managed: ManagedRun, event: dict[str, Any]) -> None:
        with managed.lock:
            path_raw = str(managed.record.get("journalFile") or "")
            if not path_raw:
                return
            path = Path(path_raw)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            except Exception as exc:
                managed.record["journalError"] = f"{type(exc).__name__}: {exc}"


def _progress_signal(record: dict[str, Any]) -> tuple[str, str]:
    workflow = record.get("workflow") or {}
    logs = workflow.get("logs") or []
    latest_root_log = str(logs[-1]) if logs else ""
    return _current_phase(workflow), latest_root_log


def _completion_output_text(record: dict[str, Any]) -> str:
    if record.get("result") is not None:
        return content_from_value(record.get("result"))
    if record.get("error"):
        return str(record.get("error") or "")
    return ""


def _runtime_error_text(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, WorkflowStopped):
        return message
    frames = traceback.format_tb(exc.__traceback__, limit=8)
    if frames:
        return message + "\n" + "".join(frames).rstrip()
    return message


def _write_output_file(record: dict[str, Any], store: WorkflowStore) -> None:
    text = _completion_output_text(record)
    if not text:
        return
    task_id = str(record.get("taskId") or record.get("runId") or "")
    session_id = str(record.get("workflowSessionId") or "")
    cwd = str(record.get("cwd") or "")
    if not task_id or not session_id:
        return
    path = store.task_output_path(cwd, session_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    record["outputFile"] = str(path)


def _owner_pid(owner: str) -> int | None:
    """Extract the launching PID from a controlOwner string.

    ``new_control_owner()`` formats it as ``"<pid>-<uuid12>"`` (see
    storage/control.py). Returns None when the owner is empty or unparseable
    (older records, hand-edited data) so the caller falls back to staleness.
    """
    head = str(owner or "").strip().split("-", 1)[0]
    if not head.isdigit():
        return None
    try:
        pid = int(head)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a local PID via signal 0.

    ``os.kill(pid, 0)`` raises ProcessLookupError when no such process exists
    and PermissionError when it exists but is owned by another user (still
    alive). Any other OSError is treated as "assume alive" so we never reap a
    run we cannot prove is dead.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _gateway_loop_present() -> bool:
    """True only inside a process that hosts a running gateway loop.

    Mirrors how _send_gateway_text resolves the runner + loop: an auto-resumed
    run is only viable where the gateway loop lives (it keeps the run alive and
    routes its completion message). Returns False outside a gateway (CLI/TUI,
    tests, headless tool processes), where auto-resume must not act.
    """
    try:
        from ..host import gateway as host_gateway

        runner = host_gateway.gateway_runner_ref()
    except Exception:
        return False
    if runner is None:
        return False
    return getattr(runner, "_gateway_loop", None) is not None


def _parse_iso_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def _last_activity_epoch(record: dict[str, Any]) -> float:
    """Best-effort wall-clock epoch of a run's most recent activity.

    Prefers the journal file's mtime (touched on every agent event), then the
    run record's own ISO timestamps. Falls back to 0.0 (epoch) so a record with
    no usable signal reads as maximally stale and is reaped by the grace
    backstop rather than lingering active forever.
    """
    candidates: list[float] = []
    journal = str(record.get("journalFile") or "")
    if journal:
        try:
            candidates.append(Path(journal).stat().st_mtime)
        except OSError:
            pass
    for key in ("resumedAt", "pausedAt", "startedAt", "createdAt"):
        epoch = _parse_iso_epoch(record.get(key))
        if epoch is not None:
            candidates.append(epoch)
    return max(candidates) if candidates else 0.0


def _harvest_journal_cache(record: dict[str, Any]) -> dict[str, list[Any]]:
    """Rebuild the resume cache from a run's journal.

    Every completed (or cached) agent writes a ``{"type": "result", "key":
    "v2:<fingerprint>", "result": ...}`` event to the journal as it finishes
    (engine/api.py). The persisted ``agentCache``, by contrast, is only flushed
    at terminal state — so a hard-killed run has the answers in its journal but
    an empty cache. This reads those result events back into the
    ``{fingerprint: [results]}`` shape ``ResumeCache`` consumes, keyed by the
    fingerprint embedded in ``key`` (the ``v2:`` prefix stripped). Skipped
    agents (result None) are recorded too, so a resume reproduces the skip
    rather than re-running. Errors are not harvested — a failed agent should
    re-run on resume. Best-effort and tolerant of a partial/corrupt journal.
    """
    journal = str(record.get("journalFile") or "")
    if not journal:
        return {}
    path = Path(journal)
    if not path.is_file():
        return {}
    harvested: dict[str, list[Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "result":
                    continue
                key = str(event.get("key") or "")
                if not key.startswith("v2:"):
                    continue
                fingerprint = key[3:]
                if not fingerprint:
                    continue
                harvested.setdefault(fingerprint, []).append(event.get("result"))
    except OSError:
        return {}
    return harvested


def _resolve_workflow_session_id(plugin_context: Any, *, host_session_id: str | None = None) -> str:
    if host_session_id:
        return str(host_session_id)
    for attr in ("session_id", "sessionId"):
        value = getattr(plugin_context, attr, None) if plugin_context is not None else None
        if value:
            return str(value)
    for method_name in ("get_session_id", "current_session_id"):
        method = getattr(plugin_context, method_name, None) if plugin_context is not None else None
        if callable(method):
            try:
                value = method()
            except Exception:
                value = None
            if value:
                return str(value)
    cli_ref = _plugin_context_cli_ref(plugin_context)
    for value in (
        getattr(getattr(cli_ref, "agent", None), "session_id", None),
        getattr(cli_ref, "session_id", None),
    ):
        if value:
            return str(value)
    for name in ("HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
        env_value = _get_hermes_session_env(name)
        if env_value:
            return env_value
    raise WorkflowRuntimeError(
        "Hermes did not provide a session id for workflow layout. "
        "Expected task_id/session_id kwargs, plugin_context CLI session, "
        "or gateway session context."
    )


def _plugin_context_cli_ref(plugin_context: Any) -> Any:
    manager = getattr(plugin_context, "_manager", None) if plugin_context is not None else None
    if manager is not None:
        return getattr(manager, "_cli_ref", None)
    return None


def _capture_parent_runtime(parent_agent: Any, *, plugin_context: Any = None) -> dict[str, Any] | None:
    """Snapshot the launching agent runtime for child-model inheritance.

    The snapshot stays on ManagedRun only. It must never be added to the
    persisted run record because it can contain credentials and live pools.
    """
    agent = parent_agent
    if agent is None:
        cli_ref = _plugin_context_cli_ref(plugin_context)
        agent = getattr(cli_ref, "agent", None) if cli_ref is not None else None
    if agent is None:
        agent = _gateway_running_agent()
    if agent is None:
        return None

    model = str(getattr(agent, "model", "") or "").strip()
    if not model:
        return None

    runtime: dict[str, Any] = {"model": model}
    for key in (
        "provider",
        "base_url",
        "api_key",
        "api_mode",
        "acp_command",
        "reasoning_config",
        "service_tier",
        "max_tokens",
    ):
        value = getattr(agent, key, None)
        if value is not None:
            runtime[key] = value
    if not runtime.get("api_key"):
        client_kwargs = getattr(agent, "_client_kwargs", None)
        if isinstance(client_kwargs, dict) and client_kwargs.get("api_key"):
            runtime["api_key"] = client_kwargs["api_key"]

    acp_args = getattr(agent, "acp_args", None)
    if acp_args:
        runtime["acp_args"] = list(acp_args)

    credential_pool = getattr(agent, "_credential_pool", None)
    if credential_pool is not None:
        runtime["credential_pool"] = credential_pool

    fallback_chain = getattr(agent, "_fallback_chain", None)
    if fallback_chain:
        runtime["fallback_model"] = list(fallback_chain)
    else:
        fallback_model = getattr(agent, "_fallback_model", None)
        if fallback_model:
            runtime["fallback_model"] = fallback_model

    request_overrides = getattr(agent, "request_overrides", None)
    if isinstance(request_overrides, dict) and request_overrides:
        runtime["request_overrides"] = dict(request_overrides)
    return runtime


def _gateway_running_agent() -> Any:
    """Return the active or cached agent for the current gateway session."""
    session_key = _get_hermes_session_env("HERMES_SESSION_KEY")
    if not session_key:
        return None
    try:
        from ..host import gateway as host_gateway

        runner = host_gateway.gateway_runner_ref()
        if runner is None:
            return None
        running = getattr(runner, "_running_agents", None)
        if isinstance(running, dict):
            agent = running.get(session_key)
            if getattr(agent, "model", None):
                return agent
        cache = getattr(runner, "_agent_cache", None)
        cached = cache.get(session_key) if isinstance(cache, dict) else None
        if isinstance(cached, tuple):
            cached = cached[0] if cached else None
        return cached if getattr(cached, "model", None) else None
    except Exception:
        return None


def _get_hermes_session_env(name: str) -> str:
    try:
        from ..host import gateway as host_gateway

        return str(host_gateway.raw_session_env(name, "") or "").strip()
    except Exception:
        return os.getenv(name, "").strip()


def _capture_cli_approval_callback() -> Any:
    """Capture the live CLI approval UI for background workflow children."""
    if (os.environ.get("HERMES_INTERACTIVE") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from tools.terminal_tool import _get_approval_callback

        callback = _get_approval_callback()
        return callback if callable(callback) else None
    except Exception:
        return None


def _workflow_approval_session_key(
    managed: ManagedRun,
    session_context: dict[str, str] | None,
) -> str:
    return str(
        (session_context or {}).get("session_key")
        or managed.record.get("workflowSessionId")
        or managed.run_id
    )


def _approve_launch(meta: dict[str, Any], config: PluginConfig, plugin_context: Any) -> tuple[bool, str]:
    """Gate a top-level workflow launch when ``require_launch_approval`` is on.

    Runs in the launching (parent) foreground turn, so the session context is
    native — no cross-thread propagation needed. Returns ``(approved, reason)``.
    Channels: gateway -> approve/deny buttons (blocks until tapped); CLI ->
    synchronous confirm; no interactive channel (headless) -> deny.
    """
    if not config.require_launch_approval:
        return True, ""

    name = str(meta.get("name") or "workflow")
    desc = str(meta.get("description") or "")
    label = f"workflow-launch:{name}"
    human = f'Launch dynamic workflow "{name}"' + (f" - {desc}" if desc else "")

    try:
        from tools import approval as _approval
    except Exception:
        return False, "launch approval required but Hermes' approval engine is unavailable"

    # Gateway: reuse the session-keyed approve/deny flow (blocks until resolved).
    try:
        gateway_channel = _workflow_gateway_approval_channel(_approval)
        if gateway_channel is not None:
            session_key, notify_cb = gateway_channel
            decision = _await_gateway_launch_decision(
                _approval,
                session_key,
                notify_cb,
                {
                    "command": label,
                    "pattern_key": "workflow_launch",
                    "pattern_keys": ["workflow_launch"],
                    "description": human,
                },
            )
            ok = bool(decision.get("resolved")) and decision.get("choice") not in (None, "deny")
            return (True, "") if ok else (False, "workflow launch was denied or timed out")
        if _approval._is_gateway_approval_context():
            return False, "launch approval required but no gateway approval channel is registered"
    except Exception as exc:
        return False, f"launch approval failed: {type(exc).__name__}: {exc}"

    # CLI interactive: synchronous confirm via the established callback pattern.
    if (os.environ.get("HERMES_INTERACTIVE") or "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            from tools.terminal_tool import _get_approval_callback

            cb = _get_approval_callback()
        except Exception:
            cb = None
        try:
            choice = _approval.prompt_dangerous_approval(label, human, approval_callback=cb)
        except Exception as exc:
            return False, f"launch approval prompt failed: {type(exc).__name__}: {exc}"
        return (True, "") if (choice and choice != "deny") else (False, "workflow launch was denied")

    return False, (
        "launch approval required but no interactive channel "
        "(set require_launch_approval=false / HERMES_DYNAMIC_WORKFLOWS_REQUIRE_LAUNCH_APPROVAL=0 "
        "for unattended/headless use)"
    )


def _workflow_gateway_approval_channel(_approval: Any) -> tuple[str, Any] | None:
    """Return a registered gateway approval channel for workflow launch gating.

    Normal gateway turns carry the current session key in approval/session
    contextvars. Some host/tool-dispatch paths preserve the registered gateway
    notify callback plus ambient session env, but lose the contextvar, making
    launch approval fail as "headless" even though the user is sitting in
    Telegram. Recover only when the ambient session key maps to a registered
    callback; never guess from an unrelated single live gateway callback.
    """

    callbacks = getattr(_approval, "_gateway_notify_cbs", None)
    if not isinstance(callbacks, dict):
        callbacks = {}

    if _approval._is_gateway_approval_context():
        session_key = str(_approval.get_current_session_key() or "")
        notify_cb = callbacks.get(session_key)
        if notify_cb is not None:
            return session_key, notify_cb
        return None

    if (os.environ.get("HERMES_INTERACTIVE") or "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    ambient_session_key = _get_hermes_session_env("HERMES_SESSION_KEY")
    if ambient_session_key:
        notify_cb = callbacks.get(ambient_session_key)
        if notify_cb is not None:
            return ambient_session_key, notify_cb
        return None

    return None


def _await_gateway_launch_decision(_approval: Any, session_key: str, notify_cb: Any, data: dict[str, Any]) -> dict[str, Any]:
    legacy_wait = getattr(_approval, "_await_gateway_decision", None)
    if callable(legacy_wait):
        return legacy_wait(session_key, notify_cb, data, surface="gateway")

    entry_cls = getattr(_approval, "_ApprovalEntry", None)
    lock = getattr(_approval, "_lock", None)
    queues = getattr(_approval, "_gateway_queues", None)
    if entry_cls is None or lock is None or not isinstance(queues, dict):
        raise RuntimeError("Hermes gateway approval queue API is unavailable")

    entry = entry_cls(data)
    with lock:
        queues.setdefault(session_key, []).append(entry)

    fire_hook = getattr(_approval, "_fire_approval_hook", None)
    if callable(fire_hook):
        fire_hook(
            "pre_approval_request",
            command=data.get("command", ""),
            description=data.get("description", ""),
            pattern_key=data.get("pattern_key", ""),
            pattern_keys=list(data.get("pattern_keys") or []),
            session_key=session_key,
            surface="gateway",
        )

    try:
        notify_cb(data)
    except Exception:
        with lock:
            queue = queues.get(session_key, [])
            if entry in queue:
                queue.remove(entry)
            if not queue:
                queues.pop(session_key, None)
        raise

    timeout = 300
    get_config = getattr(_approval, "_get_approval_config", None)
    if callable(get_config):
        try:
            timeout = int((get_config() or {}).get("gateway_timeout", timeout))
        except (TypeError, ValueError):
            timeout = 300

    touch_activity_if_due = None
    now = time.monotonic()
    activity_state: dict[str, Any] = {"start": now, "last_touch": now}
    try:
        from tools.environments.base import touch_activity_if_due as _touch_activity_if_due

        touch_activity_if_due = _touch_activity_if_due
    except Exception:
        pass

    resolved = False
    deadline = time.monotonic() + max(0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            resolved = True
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for workflow launch approval")

    with lock:
        queue = queues.get(session_key, [])
        if entry in queue:
            queue.remove(entry)
        if not queue:
            queues.pop(session_key, None)

    choice = entry.result
    if callable(fire_hook):
        fire_hook(
            "post_approval_response",
            command=data.get("command", ""),
            description=data.get("description", ""),
            pattern_key=data.get("pattern_key", ""),
            pattern_keys=list(data.get("pattern_keys") or []),
            session_key=session_key,
            surface="gateway",
            choice=(choice if resolved and choice else "timeout"),
        )

    return {"resolved": resolved and choice is not None, "choice": choice}


def _capture_gateway_session_context() -> dict[str, str] | None:
    """Capture the launching gateway session vars (parent context only).

    Returns None outside a gateway session. Used so a child worker thread can
    re-apply them and route a flagged command to the originating user for
    mid-run approval (child_approval_policy="ask").
    """
    from ..host import gateway as host_gateway

    try:
        platform = host_gateway.raw_session_env("HERMES_SESSION_PLATFORM", "")
    except Exception:
        return None
    if not platform:
        return None  # not a gateway session
    keys = {
        "platform": "HERMES_SESSION_PLATFORM",
        "chat_id": "HERMES_SESSION_CHAT_ID",
        "chat_name": "HERMES_SESSION_CHAT_NAME",
        "thread_id": "HERMES_SESSION_THREAD_ID",
        "user_id": "HERMES_SESSION_USER_ID",
        "user_name": "HERMES_SESSION_USER_NAME",
        "session_key": "HERMES_SESSION_KEY",
        "message_id": "HERMES_SESSION_MESSAGE_ID",
    }
    return {field: host_gateway.raw_session_env(env, "") for field, env in keys.items()}


def _notify_completion(
    managed: "ManagedRun",
    plugin_context: Any,
    record: dict[str, Any],
    config: PluginConfig,
    session_context: dict[str, str] | None = None,
) -> None:
    """On terminal state, inject a <task-notification> into the
    conversation so the model can deliver the result without the user polling
    /workflows. In gateway mode, where CLI injection is unavailable after the
    parent turn returns, send a concise completion message to the origin chat.

    When a live progress bubble is active, the final edit of that bubble IS the
    gateway completion message (one evolving message instead of a trailing
    second one), so the separate gateway completion send is skipped — but the
    in-conversation <task-notification> injection still happens so the parent
    model can act on the result. The bubble is finalized regardless of
    notify_on_complete (a seeded bubble must never be left stuck on "running").
    If that finalize edit fails to deliver (e.g. a long Telegram flood wait
    returns SendResult(success=False)), the agreed completion channel broke, so
    a FRESH completion send fires as recovery EVEN WHEN notify_on_complete is
    False — otherwise the verdict would be silently lost, which is the exact
    bug this guards against.
    Best effort: any failure is swallowed so it never affects the run.
    """
    bubble_finalized = False
    bubble_pending = False
    bubble_finalize_failed = False
    if config.notify_progress:
        # A bubble may have been requested at launch but its message id might not
        # have resolved yet (async seed send). For a fast run that finishes
        # almost immediately, briefly wait for the id so the final edit lands on
        # the seeded bubble instead of racing ahead with a separate completion
        # send (which would leave the bubble stuck on "running").
        deadline = time.monotonic() + _SEED_RESOLVE_WAIT_SECONDS
        requested = False
        active = False
        while True:
            with managed.lock:
                requested = managed.progress_requested
                active = managed.progress_active and bool(managed.progress_message_id)
            if active or not requested or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if active:
            try:
                # Returns True only when the completion edit was CONFIRMED
                # delivered. On a long Telegram flood wait the adapter returns
                # SendResult(success=False) instead of raising, so a bubble that
                # froze on its last mid-run text (summary+cost, no result) leaves
                # bubble_finalized False here — and the code below falls through
                # to a FRESH completion send carrying the full result, instead
                # of silently dropping the verdict (the verdict-never-posted bug).
                bubble_finalized = _edit_progress_bubble(
                    managed, config, completed=True, force=True
                )
                bubble_finalize_failed = not bubble_finalized
            except Exception:
                bubble_finalized = False
                bubble_finalize_failed = True
        elif requested:
            # The seed send is still in flight at the deadline (the slow-seed /
            # sustained-flood case). Do NOT race ahead with a separate
            # completion send — the seed's done-callback owns completion once
            # the send resolves: it finalizes the bubble in place if the send
            # succeeded, or sends the completion message itself if it failed.
            # Either way the run gets exactly one terminal message.
            bubble_pending = True
    if not config.notify_on_complete and not bubble_finalize_failed:
        return
    notification = _render_task_notification(record, config.notify_result_preview_chars)
    # WAKE THE AGENT LOOP. Independent of the user-facing bubble/chat send.
    # inject_message() returns False in gateway mode (no CLI ref), so the parent
    # model never re-enters the loop on its own — unlike delegate_task, which
    # rides process_registry.completion_queue and is injected as a new turn by
    # the gateway's _async_delegation_watcher. We enqueue an equivalent wake
    # event so a finished workflow re-enters the conversation the same way.
    # This sits INSIDE the notify gate above (so a notify_on_complete=False run
    # stays fully quiet — no inject, no wake, no send), but is a SEPARATE channel
    # from the bubble: it must NOT be suppressed by bubble_finalized/bubble_pending
    # (delegate_task delivers a bubble AND an injected turn). Exactly-once is
    # structural: _notify_completion runs once per run (the finally at the end of
    # _execute), plus a per-run guard flag blocks any double-fire.
    inject = getattr(plugin_context, "inject_message", None) if plugin_context is not None else None
    injected = False
    try:
        if callable(inject):
            injected = bool(inject(notification))
    except Exception:
        pass
    if injected:
        # CLI mode: inject_message already re-entered the conversation (it IS the
        # wake). No queue event needed (the watcher is gateway-only).
        return
    # Gateway mode (inject returned False): enqueue the wake event so the
    # async-delegation watcher injects the result as a new turn and the loop wakes.
    _enqueue_gateway_wake_event(managed, record, notification, session_context)
    # The bubble's final edit already delivered the completion text to the
    # origin chat (bubble_finalized), or the still-pending seed callback will
    # deliver it (bubble_pending); either way, don't also send a separate
    # trailing completion message.
    if bubble_finalized or bubble_pending:
        return
    _send_gateway_completion_notification(record, config, session_context)


def _enqueue_gateway_wake_event(
    managed: "ManagedRun",
    record: dict[str, Any],
    notification: str,
    session_context: dict[str, str] | None,
) -> None:
    """Enqueue a completion_queue event so the gateway's async-delegation
    watcher injects the finished workflow as a NEW TURN (waking the agent loop).

    Rides the EXISTING, proven async_delegation rail rather than a new event
    type, because the gateway watcher + post-turn drain + both notification
    formatters already own ``type:"async_delegation"`` end to end. The event is
    shaped as a SINGLE (non-batch) delegation completion — exactly the shape
    ``async_delegation._dispatch`` already emits and the watcher already handles:
      - ``is_batch`` / ``results`` omitted, so ``_finalize_async_delegation_roster``
        and the batch formatter branch both no-op (verified against gateway/run.py
        and tools/process_registry.py).
      - ``delegation_id`` = the workflow runId (UNIQUE), so the TUI dedup key
        ``(delegation_id, type)`` never collides on ``("", ...)``.
      - the run id is NOT registered in list_async_delegations(), so the live
        roster tick ignores it.
      - ``origin:"workflow"`` flags the event so the shared formatter can label
        it ``[WORKFLOW COMPLETE]`` instead of ``[ASYNC DELEGATION COMPLETE]``.
      - ``session_key`` + a ``routing`` dict carry the origin so the injected
        turn lands in the right chat/topic.
    Best effort: any failure is swallowed so it never affects the run.
    """
    # Per-run guard: never enqueue twice (e.g. a future second completion path).
    try:
        with managed.lock:
            if getattr(managed, "_wake_event_enqueued", False):
                return
            managed._wake_event_enqueued = True
    except Exception:
        # If the guard itself fails, fall through — a missing wake is worse than
        # a (structurally near-impossible) duplicate on this single-call path.
        pass

    context = dict(session_context or {})
    session_key = str(context.get("session_key") or "").strip()
    platform = str(context.get("platform") or "").strip()
    # No gateway origin -> nothing to wake (CLI/headless run). Leave it; the
    # inject_message path (CLI) already handled, or there is no loop to wake.
    if not session_key and not platform:
        return

    run_id = str(record.get("runId") or record.get("taskId") or "").strip()
    if not run_id:
        return
    status = str(record.get("status") or "completed")
    name = ((record.get("workflow") or {}).get("meta") or {}).get("name") or "workflow"

    routing = {
        "platform": platform,
        "chat_id": str(context.get("chat_id") or "").strip(),
        "thread_id": str(context.get("thread_id") or "").strip(),
        "message_id": str(context.get("message_id") or "").strip(),
        "user_id": str(context.get("user_id") or "").strip(),
        "user_name": str(context.get("user_name") or "").strip(),
    }
    evt = {
        "type": "async_delegation",
        "origin": "workflow",
        "delegation_id": run_id,
        "session_key": session_key,
        "routing": routing,
        "goal": f'Dynamic workflow "{name}"',
        "status": status,
        # The injected turn must carry the ACTIONABLE result so the agent can act
        # without reading /workflows. The rendered task-notification block (with
        # <result>, usage, recovery) IS that content.
        "summary": notification,
        "model": "workflow",
        "role": "workflow",
        # Both formatter timestamp lines are cosmetic; the run record stores
        # startedAt as an ISO string (not an epoch float), so use now() for the
        # epoch fields the formatter expects. The result content is what matters.
        "dispatched_at": time.time(),
        "completed_at": time.time(),
    }
    try:
        from tools.process_registry import process_registry as _pr

        _pr.completion_queue.put(evt)
    except Exception:
        pass


def _send_gateway_completion_notification(
    record: dict[str, Any],
    config: PluginConfig,
    session_context: dict[str, str] | None,
) -> None:
    _send_gateway_text(
        record,
        session_context,
        _render_gateway_completion_message(record, config),
        block=True,
    )


def _send_gateway_text(
    record: dict[str, Any],
    session_context: dict[str, str] | None,
    text: str,
    *,
    block: bool = True,
) -> None:
    """Send a one-off message to the origin gateway chat for this run.

    Shared by the launch and completion notifications. Resolves the adapter,
    source, and thread/topic metadata from the captured session context so the
    message lands in the originating chat/topic. Best-effort: any failure is
    swallowed so it never affects the run.

    ``block=True`` (completion, on a background thread) waits for delivery so
    the run record reflects the send. ``block=False`` (launch, on the
    synchronous tool-return path) fires and forgets so it never adds latency to
    the workflow tool result; the loop owns the coroutine once scheduled.
    """
    context = dict(session_context or {})
    platform = str(context.get("platform") or "").strip().lower()
    chat_id = str(context.get("chat_id") or "").strip()
    if not platform or not chat_id or not text:
        return

    try:
        from agent.async_utils import safe_schedule_threadsafe

        target = _resolve_gateway_target(context)
        if target is None:
            return
        adapter, loop, chat_id, metadata = target
        future = safe_schedule_threadsafe(
            adapter.send(chat_id, text, metadata=metadata),
            loop,
        )
        if block and future is not None:
            future.result(timeout=15)
    except Exception:
        pass


def _resolve_gateway_target(
    session_context: dict[str, str] | None,
) -> tuple[Any, Any, str, dict[str, Any]] | None:
    """Resolve the gateway send/edit target for a run's origin chat.

    Returns ``(adapter, loop, chat_id, metadata)`` or ``None`` when the run did
    not originate from a live gateway session (CLI run, runner/loop absent, or
    no adapter for the platform). Shared by the one-off text notifications and
    the live progress bubble so both land in the same chat/topic with the same
    routing metadata. ``metadata`` always carries ``notify=True``.
    """
    context = dict(session_context or {})
    platform = str(context.get("platform") or "").strip().lower()
    chat_id = str(context.get("chat_id") or "").strip()
    if not platform or not chat_id:
        return None
    from ..host import gateway as host_gateway

    runner = host_gateway.gateway_runner_ref()
    if runner is None:
        return None
    adapter_key, adapter = _gateway_adapter_for_platform(runner, platform)
    if adapter is None:
        return None
    source = _gateway_source_for_context(runner, context)
    if source is not None:
        chat_id = str(getattr(source, "chat_id", chat_id) or chat_id)
        metadata = _gateway_thread_metadata(runner, source=source, adapter=adapter)
    else:
        metadata = _gateway_thread_metadata(runner, context=context, adapter_key=adapter_key, adapter=adapter)
    metadata = dict(metadata) if metadata else {}
    metadata["notify"] = True
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None:
        return None
    return adapter, loop, chat_id, metadata


def _adapter_can_edit(adapter: Any) -> bool:
    """True when the adapter exposes an awaitable ``edit_message``.

    Both the built-in Telegram and Discord adapters do; an adapter without it
    (or a degraded test stub) makes the run fall back to launch+completion
    markers instead of an edited bubble.
    """
    editor = getattr(adapter, "edit_message", None)
    return callable(editor)


def _edit_accepts_metadata(adapter: Any) -> bool:
    """Whether ``adapter.edit_message`` accepts a ``metadata`` kwarg.

    Mirrors the gateway's own probe (gateway/run.py): Telegram's edit takes
    metadata to preserve topic/thread routing on overflow splits; Discord's
    does not. Passing an unsupported kwarg would raise, so probe first.
    """
    try:
        params = inspect.signature(adapter.edit_message).parameters
    except (TypeError, ValueError):
        return False
    if "metadata" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _accepts_buttons(method: Any) -> bool:
    """Whether a send/edit_message method accepts a ``buttons`` kwarg.

    Back-compat probe: a Hermes core that predates the generic inline-button
    surface has no ``buttons`` param, so passing it would raise. Only pass
    ``buttons`` when this returns True. Mirrors ``_edit_accepts_metadata``.
    """
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    if "buttons" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# Run states where workflow controls are meaningful. Excludes "stopping": stop
# has already been requested, so buttons would render misleading duplicate taps.
_STOPPABLE_STATES = {"queued", "running", "paused"}
_ACTIVE_CONTROL_STATES = {"queued", "running", "paused"}
_TERMINAL_RERUN_STATES = {"completed", "failed", "error", "stopped", "interrupted"}


def _https_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.startswith(("https://", "http://")):
        return text
    return None


def _log_url_for(record: dict[str, Any]) -> str | None:
    for key in ("logUrl", "logURL", "outputUrl", "outputURL", "journalUrl", "journalURL"):
        url = _https_url(record.get(key))
        if url:
            return url
    return None


def _control_buttons_for(record: dict[str, Any], config: PluginConfig) -> list | None:
    """Inline button spec for live workflow controls.

    Uses the existing ``notify_progress_stop_button`` flag as the master kill
    switch for gateway progress controls. Buttons are callback_data except an
    optional HTTP(S) log URL.
    """
    if not getattr(config, "notify_progress_stop_button", True):
        return None
    status = str(record.get("status") or "")
    run_id = str(record.get("runId") or "")
    task_id = str(record.get("taskId") or "")
    rows: list[list[dict[str, str]]] = []
    controls: list[dict[str, str]] = []

    if status in {"queued", "running"} and run_id:
        controls.append({"text": "⏸ Pause", "callback_data": f"wf:pause:{run_id}"})
    if status == "paused" and run_id:
        controls.append({"text": "▶️ Resume", "callback_data": f"wf:resume:{run_id}"})
    if status in _STOPPABLE_STATES and task_id:
        controls.append({"text": "⏹ Stop", "callback_data": f"wf:stop:{task_id}"})
    if status in _ACTIVE_CONTROL_STATES and run_id:
        controls.append({"text": "🔄 Restart", "callback_data": f"wf:restart:{run_id}"})
    if status in _TERMINAL_RERUN_STATES and run_id and record.get("scriptPath"):
        controls.append({"text": "🔁 Rerun", "callback_data": f"wf:rerun:{run_id}"})

    if controls:
        rows.append(controls)

    log_url = _log_url_for(record)
    if log_url:
        rows.append([{"text": "📄 Open log", "url": log_url}])

    if not rows:
        return None
    return rows[0] if len(rows) == 1 else rows


def _stop_buttons_for(record: dict[str, Any], config: PluginConfig) -> list | None:
    """Backward-compatible Stop-only helper."""
    if not getattr(config, "notify_progress_stop_button", True):
        return None
    status = str(record.get("status") or "")
    if status not in _STOPPABLE_STATES:
        return None
    task_id = str(record.get("taskId") or "")
    if not task_id:
        return None
    return [{"text": "⏹ Stop", "callback_data": f"wf:stop:{task_id}"}]


def _seed_progress_bubble(managed: "ManagedRun", config: PluginConfig) -> bool:
    """Post the initial live-progress bubble for a gateway run.

    Schedules ONE message (the seed) on the gateway loop and records its id via
    a done-callback so later state changes edit it in place. Fire-and-forget:
    it does NOT block the synchronous launch return (the message id arrives
    asynchronously; mid-run edits are no-ops until it lands). Returns True when
    the seed was scheduled (so the caller skips the separate launch marker),
    False when it could not be (notify off / no gateway context / no
    edit-capable adapter) — the caller then falls back to the launch marker.
    If the scheduled send ultimately fails, the callback emits the launch
    marker so the run is never left with no launch notification. Best-effort;
    never raises.
    """
    if not config.notify_progress:
        return False
    try:
        from agent.async_utils import safe_schedule_threadsafe

        target = _resolve_gateway_target(managed.session_context)
        if target is None:
            return False
        adapter, loop, chat_id, metadata = target
        if not _adapter_can_edit(adapter):
            # No in-place edit support: a seed would just become a static
            # duplicate of the launch marker, so decline and let the launch
            # marker handle it.
            return False
        with managed.lock:
            text = _progress_bubble_text(managed.record, config, completed=False)
            # Mark synchronously (before the async send resolves) so completion
            # knows a bubble is pending and waits for its id rather than racing
            # ahead and sending a separate completion message.
            managed.progress_requested = True
            control_buttons = _control_buttons_for(managed.record, config)
        send_kwargs: dict[str, Any] = {"metadata": metadata}
        if control_buttons is not None and _accepts_buttons(adapter.send):
            send_kwargs["buttons"] = control_buttons
        future = safe_schedule_threadsafe(
            adapter.send(chat_id, text, **send_kwargs),
            loop,
        )
        if future is None:
            with managed.lock:
                managed.progress_requested = False
            return False

        def _on_seeded(fut: Any) -> None:
            # Runs on the gateway loop thread. Any edit/send it schedules back
            # onto that same loop MUST be non-blocking (block=False): a blocking
            # future.result() here would wait on a coroutine that can only run
            # once this callback returns -> self-deadlock of the gateway loop.
            message_id = ""
            success = False
            try:
                result = fut.result()
                success = bool(getattr(result, "success", True))
                message_id = str(getattr(result, "message_id", "") or "")
            except Exception:
                success = False
                message_id = ""
            if success and message_id:
                with managed.lock:
                    managed.progress_active = True
                    managed.progress_message_id = message_id
                    managed.progress_last_text = text
                    managed.progress_last_edit_ts = time.monotonic()
                    terminal = str(managed.record.get("status") or "") not in _RUNNING_STATES
                if terminal:
                    # The run finished while the seed send was still in flight
                    # (the slow-seed-under-flood case): _notify_completion gave
                    # up waiting for the id and suppressed its own completion
                    # send, trusting this callback to finalize. Do it now so the
                    # bubble is never left stuck on the launch render.
                    try:
                        _edit_progress_bubble(managed, config, completed=True, force=True, block=False)
                    except Exception:
                        pass
                else:
                    try:
                        _edit_progress_bubble(managed, config, completed=False, force=True, block=False)
                    except Exception:
                        pass
            else:
                # Seed send failed: there is no bubble to edit. Clear the pending
                # flag so /workflows readers and completion logic stop expecting
                # one.
                with managed.lock:
                    managed.progress_requested = False
                    terminal = str(managed.record.get("status") or "") not in _RUNNING_STATES
                if terminal:
                    # The run already finished and _notify_completion suppressed
                    # its completion send waiting on this seed. The seed failed,
                    # so deliver the completion notification now (non-blocking)
                    # — otherwise the run would be left with no terminal message.
                    try:
                        _send_gateway_text(
                            managed.record,
                            managed.session_context,
                            _render_gateway_completion_message(managed.record, config),
                            block=False,
                        )
                    except Exception:
                        pass
                else:
                    # Still running: fall back to the launch marker so the run is
                    # not left silent at launch.
                    try:
                        _notify_launch(managed.record, config, managed.session_context)
                    except Exception:
                        pass

        try:
            future.add_done_callback(_on_seeded)
        except Exception:
            with managed.lock:
                managed.progress_requested = False
            return False
        return True
    except Exception:
        return False


def _edit_progress_bubble(
    managed: "ManagedRun",
    config: PluginConfig,
    *,
    completed: bool,
    force: bool = False,
    block: bool = True,
) -> bool:
    """Edit the live-progress bubble in place for a mid-run or final update.

    Throttled by ``notify_progress_min_interval_seconds`` and skipped when the
    rendered text is unchanged, so mid-run edits stay well under platform edit
    limits. ``force=True`` (the final completion edit) bypasses the throttle.
    Edits — not sends — so this never contributes to per-chat send flood
    limits. Best-effort; never raises.

    ``block`` only applies to the completion edit: the worker thread waits
    briefly so the run record reflects the final visual. It MUST be False when
    called from the gateway loop thread (the seed done-callback finalizing a
    run that ended while its seed was in flight), because blocking on a future
    scheduled onto that same loop would self-deadlock it.

    Returns True ONLY when a blocking completion edit was CONFIRMED delivered
    (the adapter returned a truthy/`success` result). Returns False when the
    edit could not be confirmed delivered — no active bubble, no gateway
    target, an exception, or (the bug this guards) the adapter reporting
    failure such as ``SendResult(success=False, error="flood_control:N")`` on a
    long Telegram flood wait. The completion caller uses this to decide whether
    to fall back to a fresh completion SEND so the result is never silently
    lost. A non-blocking call (``block=False``, fire-and-forget) returns False
    because delivery cannot be confirmed synchronously; the seed done-callback
    owns that path's fallback instead.
    """
    with managed.lock:
        if not managed.progress_active or not managed.progress_message_id:
            return False
        message_id = managed.progress_message_id
        text = _progress_bubble_text(managed.record, config, completed=completed)
        now = time.monotonic()
        if not force:
            if text == managed.progress_last_text:
                return False
            interval = max(0.0, float(config.notify_progress_min_interval_seconds))
            if now - managed.progress_last_edit_ts < interval:
                return False
        managed.progress_last_text = text
        managed.progress_last_edit_ts = now
        # Inline workflow controls: while active show status-appropriate buttons;
        # on completion show terminal controls (for example Rerun) when possible,
        # otherwise pass [] to CLEAR active controls. None mid-run when no
        # controls are valid leaves any existing keyboard untouched.
        control_buttons = _control_buttons_for(managed.record, config)
        if completed:
            edit_buttons: list | None = control_buttons if control_buttons is not None else []
        else:
            edit_buttons = control_buttons
        if completed:
            # Stop further mid-run edits once finalized.
            managed.progress_active = False
    try:
        from agent.async_utils import safe_schedule_threadsafe

        target = _resolve_gateway_target(managed.session_context)
        if target is None:
            return False
        adapter, loop, chat_id, metadata = target
        if not _adapter_can_edit(adapter):
            return False
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "content": text,
        }
        if getattr(adapter, "REQUIRES_EDIT_FINALIZE", False) or completed:
            kwargs["finalize"] = True
        if _edit_accepts_metadata(adapter):
            kwargs["metadata"] = metadata
        # Only pass buttons when (a) the run wants to set/clear them and (b) the
        # core adapter supports the generic buttons= kwarg. Mid-run not-stoppable
        # -> None -> omit (leave keyboard as-is).
        if edit_buttons is not None and _accepts_buttons(adapter.edit_message):
            kwargs["buttons"] = edit_buttons
        future = safe_schedule_threadsafe(adapter.edit_message(**kwargs), loop)
        if future is None:
            return False
        # Completion edit blocks briefly so the run record reflects the final
        # visual; mid-run edits fire and forget to avoid latency. block=False
        # forces fire-and-forget even on completion (used when finalizing from
        # the gateway loop thread, where blocking would self-deadlock the loop).
        if completed and block:
            result = future.result(timeout=15)
            return _edit_result_ok(result)
        # Mid-run / fire-and-forget completion: delivery not confirmable here.
        return False
    except Exception:
        return False


def _edit_result_ok(result: Any) -> bool:
    """Interpret an adapter ``edit_message`` return as confirmed-delivered.

    The Telegram adapter returns a ``SendResult`` whose ``success`` is False on
    a long flood-control wait (``error="flood_control:N"``) instead of raising,
    so a caller that ignores the return mistakes a dropped edit for a delivered
    one (the verdict-never-posted bug). Treat an explicit ``success=False`` as
    NOT delivered. A result with no ``success`` attribute (a minimal adapter or
    a test stub returning None/True) is treated as delivered to preserve prior
    best-effort behavior for non-Telegram adapters.
    """
    if result is None:
        # No structured result. A None return from a real edit is ambiguous;
        # historically the call was fire-and-forget and assumed delivered, so
        # keep that for adapters that return nothing.
        return True
    success = getattr(result, "success", None)
    if success is None:
        return True
    return bool(success)


def _progress_bubble_text(record: dict[str, Any], config: PluginConfig, *, completed: bool) -> str:
    """Render live progress or the shared outcome-first completion card."""
    if not completed:
        return render_run_progress(record, show_cost=config.notify_progress_cost)
    return render_completion_card(
        record,
        preview_chars=config.notify_result_preview_chars,
        show_cost=config.notify_progress_cost,
    )


def _notify_launch(
    record: dict[str, Any],
    config: PluginConfig,
    session_context: dict[str, str] | None,
) -> None:
    """On launch, send a concise "workflow started" marker to the origin
    gateway chat. Gateway-only (no-op outside a gateway session); best-effort,
    fire-and-forget so it never delays the synchronous launch return.
    """
    if not config.notify_on_launch:
        return
    _send_gateway_text(record, session_context, _render_gateway_launch_message(record), block=False)


def _render_gateway_launch_message(record: dict[str, Any]) -> str:
    summary = str(record.get("summary") or "Dynamic workflow")
    task_id = str(record.get("taskId") or record.get("runId") or "")
    lines = [
        f"🚀 Workflow started: {summary}",
        f"Task: {task_id}",
        "Running in background. Use /workflows to watch live progress.",
    ]
    return "\n".join(lines)


def _gateway_adapter_for_platform(runner: Any, platform: str) -> tuple[Any, Any]:
    adapters = getattr(runner, "adapters", None)
    if not isinstance(adapters, dict):
        return None, None
    for key, adapter in adapters.items():
        value = str(getattr(key, "value", key) or "").lower()
        if value == platform:
            return key, adapter
    return None, None


def _gateway_source_for_context(runner: Any, context: dict[str, str]) -> Any:
    session_key = str(context.get("session_key") or "").strip()
    sources = getattr(runner, "_session_sources", None)
    if session_key and hasattr(sources, "get"):
        try:
            source = sources.get(session_key)
        except Exception:
            source = None
        if source is not None:
            return source
    return None


def _gateway_thread_metadata(
    runner: Any,
    *,
    source: Any | None = None,
    context: dict[str, str] | None = None,
    adapter_key: Any = None,
    adapter: Any = None,
) -> dict[str, Any] | None:
    if source is not None:
        method = getattr(runner, "_thread_metadata_for_source", None)
        if callable(method):
            try:
                return method(source, getattr(source, "message_id", None))
            except Exception:
                pass
    context = context or {}
    thread_id = str(context.get("thread_id") or "").strip()
    if not thread_id:
        return None
    method = getattr(runner, "_thread_metadata_for_target", None)
    if callable(method):
        try:
            return method(
                adapter_key,
                str(context.get("chat_id") or ""),
                thread_id,
                chat_type="dm",
                reply_to_message_id=str(context.get("message_id") or "") or None,
                adapter=adapter,
            )
        except Exception:
            pass
    return {"thread_id": thread_id}


def _render_gateway_completion_message(record: dict[str, Any], config: PluginConfig) -> str:
    """Render the same terminal card used by the final progress-bubble edit."""
    return render_completion_card(
        record,
        preview_chars=config.notify_result_preview_chars,
        show_cost=config.notify_progress_cost,
    )


def _render_task_notification(record: dict[str, Any], preview_chars: int) -> str:
    """Build a task-notification block adapted to a workflow run (tool_uses
    mapped to agents, plus errors)."""
    run_id = record.get("runId") or ""
    task_id = record.get("taskId") or run_id
    status = str(record.get("status") or "completed")
    snapshot = record.get("workflow") or {}
    meta = snapshot.get("meta") or {}
    name = meta.get("name") or "workflow"
    totals = snapshot.get("totals") or {}

    intentional_stop = is_intentional_stop_record(record)
    if intentional_stop:
        summary = f'Dynamic workflow "{name}" stopped intentionally'
    elif record.get("error"):
        if status == "failed":
            summary = f'Dynamic workflow "{name}" failed: {record["error"]}'
        else:
            summary = f'Dynamic workflow "{name}" {status}: {record["error"]}'
    elif status == "completed":
        summary = f'Dynamic workflow "{name}" completed'
    elif status == "stopped":
        summary = f'Dynamic workflow "{name}" was stopped'
    else:
        summary = f'Dynamic workflow "{name}" {status}'

    include_result = not record.get("error")
    result_text = _completion_output_text(record) if include_result else ""
    truncated = len(result_text) > preview_chars > 0
    if truncated:
        remaining = len(result_text) - preview_chars
        output_file = str(record.get("outputFile") or "")
        suffix = f"\n... (truncated {remaining} chars"
        if output_file:
            suffix += f", full result in {output_file}"
        suffix += ")"
        result_text = result_text[:preview_chars] + suffix

    agents = int(totals.get("agents") or 0)
    tokens = int(totals.get("tokens") or 0)
    tool_uses = int(totals.get("tool_calls") or 0)
    duration_ms = int(float(snapshot.get("duration_seconds") or 0) * 1000)

    lines = [
        "<task-notification>",
        f"<task-id>{task_id}</task-id>",
    ]
    output_file = str(record.get("outputFile") or "")
    if output_file:
        lines.append(f"<output-file>{output_file}</output-file>")
    lines.extend(
        [
            f"<status>{status}</status>",
            f"<summary>{summary}</summary>",
        ]
    )
    if result_text:
        lines.append(f"<result>{result_text}</result>")
    recovery = str(record.get("transcriptDir") or "")
    if record.get("error") and not intentional_stop and recovery:
        lines.append(f"<recovery>Agent transcripts: {recovery}</recovery>")
    lines.append(
        f"<usage><agent_count>{agents}</agent_count>"
        f"<subagent_tokens>{tokens}</subagent_tokens>"
        f"<tool_uses>{tool_uses}</tool_uses>"
        f"<duration_ms>{duration_ms}</duration_ms></usage>"
    )
    lines.append("</task-notification>")
    return "\n".join(lines)

_MANAGER: WorkflowRunManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_run_manager() -> WorkflowRunManager:
    global _MANAGER
    with _MANAGER_LOCK:
        first_build = _MANAGER is None
        if _MANAGER is None:
            _MANAGER = WorkflowRunManager(enable_control=True)
        manager = _MANAGER
    if first_build:
        # On the first manager build of this process, reap runs orphaned by a
        # prior process (e.g. the gateway we just replaced) so they stop lying
        # "running", and auto-resume the fresh orphans if enabled. Reaping is a
        # few file ops (synchronous); the resume relaunches run on their own
        # daemon threads inside start_from_params, so this whole hook is fast.
        # Best-effort: never let boot bookkeeping break manager construction.
        try:
            manager.reap_and_maybe_resume()
        except Exception:
            pass
    return manager
