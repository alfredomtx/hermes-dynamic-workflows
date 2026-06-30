"""Inline-button callback handler for the live workflow progress bubble.

Consumes the core ``gateway_callback`` plugin hook (fired by a gateway adapter
when an inline-button click's ``callback_data`` matches no built-in core
prefix). We own the ``wf:`` namespace for workflow controls:
``wf:stop:<taskId>``, ``wf:pause:<runId>``, ``wf:resume:<runId>``,
``wf:restart:<runId>``, and ``wf:rerun:<runId>``.

The handler does NO async/platform I/O. It returns a DIRECTIVE dict that the
adapter performs:

    {"handled": bool, "answer": str, "edit_text": str|None, "strip_buttons": bool}

Core does the auth CHECK and passes ``authorized``; we decide whether the action
requires it. Returning ``None`` lets other plugins / the silent-ack fallback
handle the click.
"""

from __future__ import annotations

from typing import Any

from ..run.manager import get_run_manager

# Prefix this plugin owns. Anything else -> return None (not ours).
_PREFIX = "wf:"
_ACTIONS = {"stop", "pause", "resume", "restart", "rerun"}


def on_gateway_callback(
    data: str | None = None,
    authorized: bool = False,
    **_: Any,
) -> dict | None:
    """Handle a ``wf:<action>:<id>`` inline-button click."""
    if not isinstance(data, str) or not data.startswith(_PREFIX):
        return None

    action, target = _parse_action(data)
    if action not in _ACTIONS:
        return None

    if not authorized:
        # Core already ran the auth check; refuse without touching the run.
        return {
            "handled": True,
            "answer": "⛔ Not authorized to control workflows.",
            "edit_text": None,
            "strip_buttons": False,
        }

    if not target:
        return {
            "handled": True,
            "answer": f"Invalid {action} request.",
            "edit_text": None,
            "strip_buttons": True,
        }

    try:
        manager = get_run_manager()
        if action == "stop":
            ok = bool(manager.stop_task(target))
            if ok:
                return _directive("⏹ Stopping…", strip_buttons=True)
            return _directive("Run already finished.", strip_buttons=True)
        if action == "pause":
            ok = bool(manager.pause(target))
            return _directive("⏸ Paused." if ok else "Workflow is not pausable.")
        if action == "resume":
            ok = bool(manager.resume(target))
            return _directive("▶️ Resumed." if ok else "Workflow is not paused.")
        if action in {"restart", "rerun"}:
            restarted = manager.restart(target)
            new_run_id = str((restarted or {}).get("runId") or "").strip()
            if restarted and new_run_id:
                verb = "Rerun" if action == "rerun" else "Restart"
                return _directive(f"🔄 {verb} started: {new_run_id}", strip_buttons=True)
            return _directive("Workflow could not be restarted.")
    except Exception:
        # Never let a handler exception bubble into the gateway loop; report a
        # benign toast and strip the now-untrustworthy button set.
        return {
            "handled": True,
            "answer": "Could not control the run.",
            "edit_text": None,
            "strip_buttons": True,
        }

    return None


def _parse_action(data: str) -> tuple[str, str]:
    rest = data[len(_PREFIX):]
    action, sep, target = rest.partition(":")
    if not sep:
        return action.strip(), ""
    return action.strip(), target.strip()


def _directive(answer: str, *, strip_buttons: bool = False) -> dict:
    return {
        "handled": True,
        "answer": answer,
        "edit_text": None,
        "strip_buttons": strip_buttons,
    }
