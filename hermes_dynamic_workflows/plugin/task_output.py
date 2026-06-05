"""Claude-style task_output tool for dynamic workflow runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..engine.manager import get_run_manager

TASK_OUTPUT_SCHEMA = {
    "description": "Read output/logs from a background dynamic workflow task.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID to get output from",
            },
            "block": {
                "type": "boolean",
                "default": True,
                "description": "Whether to wait for completion",
            },
            "timeout": {
                "type": "number",
                "minimum": 0,
                "maximum": 600000,
                "default": 30000,
                "description": "Max wait time in ms",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
}

_MAX_OUTPUT_CHARS = 100_000
_POLL_INTERVAL_SECONDS = 0.1


def task_output(params: dict[str, Any], **_: Any) -> str:
    params = params or {}
    task_id = str(params.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")

    block = _as_bool(params.get("block"), True)
    timeout_ms = _as_timeout_ms(params.get("timeout"), 30_000)
    manager = get_run_manager()
    run = _wait_for_task(manager, task_id, block=block, timeout_ms=timeout_ms)
    if not run:
        raise ValueError(f"No task found with ID: {task_id}")

    status = _task_status(run.get("status"))
    if _is_terminal_task_status(status):
        retrieval_status = "success"
    elif block:
        retrieval_status = "timeout"
    else:
        retrieval_status = "not_ready"

    return _render_task_output(run, retrieval_status=retrieval_status, status=status)


def _wait_for_task(manager: Any, task_id: str, *, block: bool, timeout_ms: int) -> dict[str, Any] | None:
    run = manager.get_by_task_id(task_id)
    if not block or not run:
        return run
    if _is_terminal_task_status(_task_status(run.get("status"))):
        return run

    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
        run = manager.get_by_task_id(task_id)
        if not run or _is_terminal_task_status(_task_status(run.get("status"))):
            return run
    return manager.get_by_task_id(task_id)


def _render_task_output(run: dict[str, Any], *, retrieval_status: str, status: str) -> str:
    task_id = str(run.get("taskId") or "")
    lines = [
        f"<retrieval_status>{retrieval_status}</retrieval_status>",
        f"<task_id>{task_id}</task_id>",
        "<task_type>local_workflow</task_type>",
        f"<status>{status}</status>",
    ]
    output = _output_text(run, status=status)
    if output.strip():
        lines.append(f"<output>\n{output.rstrip()}\n</output>")
    error = str(run.get("error") or "")
    if error:
        lines.append(f"<error>{error}</error>")
    return "\n".join(lines)


def _output_text(run: dict[str, Any], *, status: str) -> str:
    if _is_terminal_task_status(status):
        output_file = str(run.get("outputFile") or "")
        if output_file:
            try:
                text = Path(output_file).read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text:
                return _truncate(text)
    text = str(run.get("display") or "")
    if text:
        return _truncate(text)
    error = str(run.get("error") or "")
    if error:
        return _truncate(error)
    return ""


def _task_status(status: Any) -> str:
    raw = str(status or "")
    if raw == "queued":
        return "pending"
    if raw in {"running", "stopping"}:
        return "running"
    if raw == "completed":
        return "completed"
    if raw in {"failed", "error"}:
        return "failed"
    if raw == "stopped":
        return "killed"
    return raw or "pending"


def _is_terminal_task_status(status: str) -> bool:
    return status in {"completed", "failed", "killed"}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"true", "1", "yes", "y", "on"}:
            return True
        if clean in {"false", "0", "no", "n", "off"}:
            return False
    return default


def _as_timeout_ms(value: Any, default: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(600_000, max(0, parsed))


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    remaining = len(text) - _MAX_OUTPUT_CHARS
    return text[:_MAX_OUTPUT_CHARS] + f"\n... (truncated {remaining} chars)"
