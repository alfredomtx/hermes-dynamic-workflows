"""Background workflow run manager."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import ResumeCache
from .config import PluginConfig, load_config
from ..storage.store import WorkflowStore, new_run_id, resolve_workflow_source, utc_now_iso
from ..ui.display import (
    render_agent_detail,
    render_phase_detail,
    render_run_detail,
    render_runs_list,
    render_saved_markdown,
    render_workflow_text,
)
from .runtime import WorkflowOptions, run_workflow


@dataclass
class ManagedRun:
    run_id: str
    stop_event: threading.Event
    record: dict[str, Any]
    thread: threading.Thread | None = None
    child_runner: Any = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class WorkflowRunManager:
    def __init__(self, store: WorkflowStore | None = None, config: PluginConfig | None = None):
        self.store = store or WorkflowStore()
        self.config = config or load_config()
        self._runs: dict[str, ManagedRun] = {}
        self._lock = threading.RLock()

    def start_from_params(
        self,
        params: dict[str, Any],
        *,
        cwd: str | None = None,
        plugin_context: Any = None,
    ) -> dict[str, Any]:
        config = self.config
        source = resolve_workflow_source(params, store=self.store, cwd=cwd)
        run_id = new_run_id()
        saved_path = self.store.save_script(run_id, source.script)
        resume_from = str(params.get("resumeFromRunId") or "").strip() or None
        previous = self.store.load_run(resume_from) if resume_from else None
        resume_cache = ResumeCache.from_run(previous)
        args = params["args"] if "args" in params else None
        token_budget = _as_positive_int(params.get("token_budget"))

        stop_event = threading.Event()
        record = {
            "runId": run_id,
            "status": "queued",
            "createdAt": utc_now_iso(),
            "startedAt": None,
            "finishedAt": None,
            "cwd": cwd or os.environ.get("TERMINAL_CWD") or os.getcwd(),
            "scriptPath": str(saved_path),
            "source": {
                "type": source.source_type,
                "ref": source.source_ref,
            },
            "resumeFromRunId": resume_from,
            "args": args,
            "tokenBudget": token_budget,
            "result": None,
            "error": None,
            "display": "",
            "workflow": None,
            "agentCache": {},
        }
        managed = ManagedRun(run_id=run_id, stop_event=stop_event, record=record)

        with self._lock:
            self._runs[run_id] = managed
        self.store.save_run(record)

        thread = threading.Thread(
            target=self._run_thread,
            args=(managed, source.script, args, config, resume_cache, cwd, plugin_context, token_budget),
            name=f"workflow-{run_id}",
            daemon=True,
        )
        managed.thread = thread
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

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self._public_record(run) for run in self.store.list_runs(limit=limit)]

    def stop(self, run_id: str) -> bool:
        with self._lock:
            managed = self._runs.get(run_id)
        if not managed:
            record = self.store.load_run(run_id)
            if not record or record.get("status") not in {"queued", "running"}:
                return False
            record["status"] = "stopped"
            record["finishedAt"] = utc_now_iso()
            self.store.save_run(record)
            return True

        managed.stop_event.set()
        child_runner = managed.child_runner
        if child_runner is not None and hasattr(child_runner, "interrupt_all"):
            try:
                child_runner.interrupt_all()
            except Exception:
                pass
        with managed.lock:
            if managed.record.get("status") in {"queued", "running"}:
                managed.record["status"] = "stopping"
                self.store.save_run(managed.record)
        return True

    def format_list(self, limit: int = 10) -> str:
        runs = self.list(limit=limit)
        return render_runs_list(runs)

    def format_detail(self, run_id: str) -> str:
        run = self.get(run_id)
        if not run:
            return f"Workflow run not found: {run_id}"
        return render_run_detail(run)

    def format_phase(self, run_id: str, selector: str) -> str:
        run = self.get(run_id)
        if not run:
            return f"Workflow run not found: {run_id}"
        return render_phase_detail(run, selector)

    def format_agent(self, run_id: str, selector: str) -> str:
        run = self.get(run_id)
        if not run:
            return f"Workflow run not found: {run_id}"
        return render_agent_detail(run, selector)

    def save_markdown(self, run_id: str, path: str | None = None) -> str:
        run = self.get(run_id)
        if not run:
            return f"Workflow run not found: {run_id}"
        if path:
            target = Path(path).expanduser()
            if not target.is_absolute():
                target = Path(run.get("cwd") or os.getcwd()) / target
        else:
            target = self.store.exports_dir / f"{run_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_saved_markdown(run), encoding="utf-8")
        return f"Saved workflow {run_id} to {target}"

    def save_named_workflow(
        self,
        run_id: str,
        name: str,
        *,
        scope: str = "project",
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Save a run's script as a reusable named workflow.

        Writes to ``<cwd>/.hermes/workflows/<name>.py`` (project scope) or the
        user store's ``workflows/<name>.py`` (user scope). Either location is
        resolvable later by passing ``name`` to the workflow tool, and the
        caller can register a ``/<name>`` slash command for it.
        """
        from ..storage.store import _RESERVED_WORKFLOW_NAMES, _safe_workflow_name

        run = self.get(run_id)
        if not run:
            return {"ok": False, "message": f"Workflow run not found: {run_id}"}
        script = self._load_run_script(run, run_id)
        if not script:
            return {"ok": False, "message": f"No saved script found for run {run_id}"}
        try:
            safe = _safe_workflow_name(name)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if safe in _RESERVED_WORKFLOW_NAMES:
            return {"ok": False, "message": f"'{safe}' is reserved; choose another name"}

        if scope == "user":
            target = self.store.workflows_dir / f"{safe}.py"
        else:
            base = Path(cwd or run.get("cwd") or os.getcwd()).expanduser()
            target = base / ".hermes" / "workflows" / f"{safe}.py"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(script, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "message": f"Could not write {target}: {exc}"}
        return {"ok": True, "name": safe, "path": str(target), "scope": scope}

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
    ) -> None:
        try:
            from ..agents.runner import HermesChildAgentRunner

            managed.child_runner = HermesChildAgentRunner(config)
            self._update(managed, status="running", startedAt=utc_now_iso())
            result = run_workflow(
                script,
                WorkflowOptions(
                    args=args,
                    cwd=cwd or os.environ.get("TERMINAL_CWD") or os.getcwd(),
                    config=config,
                    child_runner=managed.child_runner,
                    stop_event=managed.stop_event,
                    resume_cache=resume_cache,
                    on_update=lambda state: self._update_state(managed, state),
                    plugin_context=plugin_context,
                    token_budget_total=token_budget,
                ),
            )
            snapshot = result.state.snapshot()
            if managed.stop_event.is_set():
                status = "stopped"
            else:
                status = _derive_run_status(snapshot)
            self._update(
                managed,
                status=status,
                finishedAt=utc_now_iso(),
                result=result.value,
                workflow=snapshot,
                display=render_workflow_text(snapshot, completed=True),
                agentCache=resume_cache.current,
            )
        except Exception as exc:
            status = "stopped" if managed.stop_event.is_set() else "error"
            self._update(
                managed,
                status=status,
                finishedAt=utc_now_iso(),
                error=f"{type(exc).__name__}: {exc}",
                agentCache=resume_cache.current,
            )
        finally:
            _notify_completion(plugin_context, managed.record, config)

    def _update_state(self, managed: ManagedRun, state) -> None:
        snapshot = state.snapshot()
        self._update(
            managed,
            workflow=snapshot,
            display=render_workflow_text(snapshot, completed=False),
        )

    def _update(self, managed: ManagedRun, **fields: Any) -> None:
        with managed.lock:
            managed.record.update(fields)
            self.store.save_run(managed.record)

    def _public_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return dict(record)


def _content_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _notify_completion(plugin_context: Any, record: dict[str, Any], config: PluginConfig) -> None:
    """On terminal state, inject a Claude-Code-style <task-notification> into the
    conversation so the model can deliver the result without the user polling
    /workflows. Best-effort and CLI-only: ctx.inject_message returns False in
    gateway mode; any failure is swallowed so it never affects the run.
    """
    if not config.notify_on_complete or plugin_context is None:
        return
    inject = getattr(plugin_context, "inject_message", None)
    if not callable(inject):
        return
    try:
        inject(_render_task_notification(record, config.notify_result_preview_chars))
    except Exception:
        pass


def _render_task_notification(record: dict[str, Any], preview_chars: int) -> str:
    """Mirror Claude Code's LocalAgentTask task-notification block, adapted to a
    workflow run (tool_uses -> agents, plus errors)."""
    run_id = record.get("runId") or ""
    status = str(record.get("status") or "completed")
    snapshot = record.get("workflow") or {}
    meta = snapshot.get("meta") or {}
    name = meta.get("name") or "workflow"
    totals = snapshot.get("totals") or {}

    if record.get("error"):
        summary = f'Workflow "{name}" {status}: {record["error"]}'
    elif status == "completed":
        summary = f'Workflow "{name}" completed'
    elif status == "failed":
        summary = f'Workflow "{name}" failed: all agents errored'
    elif status == "stopped":
        summary = f'Workflow "{name}" was stopped'
    else:
        summary = f'Workflow "{name}" {status}'

    result = record.get("result")
    result_text = result if isinstance(result, str) else _content_from_value(result)
    result_text = result_text or ""
    truncated = len(result_text) > preview_chars > 0
    if truncated:
        result_text = result_text[:preview_chars] + "…"

    done = int(totals.get("done") or 0)
    agents = int(totals.get("agents") or 0)
    errors = int(totals.get("errors") or 0)
    tokens = int(totals.get("tokens") or 0)
    duration_ms = int(float(snapshot.get("duration_seconds") or 0) * 1000)

    lines = [
        "<task-notification>",
        f"<task-id>{run_id}</task-id>",
        f"<status>{status}</status>",
        f"<summary>{summary}</summary>",
    ]
    if result_text:
        lines.append(f"<result>{result_text}</result>")
    lines.append(
        f"<usage><total_tokens>{tokens}</total_tokens>"
        f"<agents>{done}/{agents}</agents><errors>{errors}</errors>"
        f"<duration_ms>{duration_ms}</duration_ms></usage>"
    )
    lines.append("</task-notification>")
    tail = f"Use /workflows {run_id} for full details"
    tail += " (result truncated above)." if truncated else "."
    return "\n".join(lines) + "\n" + tail


def _derive_run_status(snapshot: dict[str, Any]) -> str:
    """A run that finished but where every agent errored is 'failed', not
    'completed'. Partial failures stay 'completed' (surfaced via the error
    count in the display)."""
    totals = snapshot.get("totals") or {}
    agents = int(totals.get("agents") or 0)
    done = int(totals.get("done") or 0)
    if agents > 0 and done == 0:
        return "failed"
    return "completed"


def _as_positive_int(value: Any) -> int | None:
    """Coerce a per-invocation token_budget to a positive int, or None."""
    if value in (None, "", False):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


_MANAGER: WorkflowRunManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_run_manager() -> WorkflowRunManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = WorkflowRunManager()
        return _MANAGER
