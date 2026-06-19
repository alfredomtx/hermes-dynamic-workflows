"""Inline-button callback handler for the live workflow progress bubble.

Consumes the core ``gateway_callback`` plugin hook (fired by a gateway adapter
when an inline-button click's ``callback_data`` matches no built-in core
prefix). We own the ``wf:`` namespace — currently just ``wf:stop:<taskId>`` from
the Stop button rendered on the progress bubble.

The handler does NO async/platform I/O. It returns a DIRECTIVE dict that the
adapter performs:

    {"handled": bool, "answer": str, "edit_text": str|None, "strip_buttons": bool}

Core does the auth CHECK and passes ``authorized``; we decide whether the action
requires it (stopping a run does). Returning ``None`` lets other plugins / the
silent-ack fallback handle the click.
"""

from __future__ import annotations

from typing import Any

from ..run.manager import get_run_manager

# Prefix this plugin owns. Anything else -> return None (not ours).
_STOP_PREFIX = "wf:stop:"


def on_gateway_callback(
    data: str | None = None,
    authorized: bool = False,
    **_: Any,
) -> dict | None:
    """Handle a ``wf:stop:<taskId>`` inline-button click.

    Returns a directive dict when this click is ours, else ``None``.
    """
    if not isinstance(data, str) or not data.startswith(_STOP_PREFIX):
        return None

    if not authorized:
        # Core already ran the auth check; refuse without touching the run.
        return {
            "handled": True,
            "answer": "⛔ Not authorized to stop workflows.",
            "edit_text": None,
            "strip_buttons": False,
        }

    task_id = data[len(_STOP_PREFIX):].strip()
    if not task_id:
        return {
            "handled": True,
            "answer": "Invalid stop request.",
            "edit_text": None,
            "strip_buttons": True,
        }

    try:
        result = get_run_manager().stop_task(task_id)
    except Exception:
        # Never let a handler exception bubble into the gateway loop; report a
        # benign toast and strip the now-untrustworthy button.
        return {
            "handled": True,
            "answer": "Could not stop the run.",
            "edit_text": None,
            "strip_buttons": True,
        }

    if result:
        # The run's own stopping/completion edit repaints the bubble body; we
        # only strip the button now so a second tap can't fire.
        return {
            "handled": True,
            "answer": "⏹ Stopping…",
            "edit_text": None,
            "strip_buttons": True,
        }

    # stop_task returned None: the run is already terminal / unknown.
    return {
        "handled": True,
        "answer": "Run already finished.",
        "edit_text": None,
        "strip_buttons": True,
    }
