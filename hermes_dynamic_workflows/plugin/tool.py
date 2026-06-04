"""Hermes tool handler for workflow."""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

from ..engine.manager import get_run_manager


def workflow(params: dict[str, Any], *, plugin_context: Any = None, **kwargs: Any) -> str:
    try:
        manager = get_run_manager()
        tool_use_id = (
            kwargs.get("tool_use_id")
            or kwargs.get("toolUseId")
            or kwargs.get("tool_call_id")
            or kwargs.get("toolCallId")
        )
        record = manager.start_from_params(
            params or {},
            cwd=os.environ.get("TERMINAL_CWD") or os.getcwd(),
            plugin_context=plugin_context,
            tool_use_id=str(tool_use_id) if tool_use_id else None,
            host_session_id=_host_session_id_from_kwargs(kwargs),
        )
        return _launch_message(record)
    except Exception as exc:
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "trace": _short_traceback(),
            },
            ensure_ascii=False,
        )


def _launch_message(record: dict[str, Any]) -> str:
    run_id = record.get("runId") or ""
    task_id = record.get("taskId") or run_id
    summary = record.get("summary") or "Dynamic workflow"
    transcript_dir = record.get("transcriptDir") or ""
    script_path = record.get("scriptPath") or ""
    return "\n".join(
        [
            f"Workflow launched in background. Task ID: {task_id}",
            f"Summary: {summary}",
            f"Transcript dir: {transcript_dir} (written when the workflow completes)",
            f"Script file: {script_path}",
            f"Run ID: {run_id}",
            (
                "To resume after editing the script: "
                f"Workflow({{scriptPath: {json.dumps(script_path, ensure_ascii=False)}, "
                f"resumeFromRunId: {json.dumps(run_id)}}})"
            ),
            "You will be notified when it completes. Use /workflows to watch live progress.",
        ]
    )


def _short_traceback() -> str:
    lines = traceback.format_exc(limit=4).strip().splitlines()
    return "\n".join(lines[-8:])


def _host_session_id_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    for key in (
        "session_id",
        "sessionId",
        "current_session_id",
        "currentSessionId",
        "task_id",
        "taskId",
    ):
        value = kwargs.get(key)
        if value:
            return str(value)
    return None
