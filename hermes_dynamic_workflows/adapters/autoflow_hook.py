"""pre_gateway_dispatch hook — autoflow per-session toggle + steering.

This is the gateway-coupled layer for autoflow (the ultracode-style sticky
auto-workflow mode). The pure state and decision logic live in
``core/autoflow.py``; this module wires that to Hermes' gateway.

Why the toggle lives in the hook and NOT in a plugin slash-command handler
(verified against Hermes core):

* A plugin slash-command handler receives only its args string — it is
  session-blind at dispatch time (neither the approval contextvar nor the
  session_context vars are bound yet), so it cannot resolve *which* session to
  toggle. The ``pre_gateway_dispatch`` hook, by contrast, receives ``event``
  (with ``event.source``) and ``gateway``, so it can derive the canonical
  session_key via ``gateway._session_key_for_source(event.source)`` — the same
  key the reasoning-override store uses.
* The hook result protocol honors only ``skip`` / ``rewrite`` / ``allow``
  (no ``reply`` verb), and ``invoke_hook`` is synchronous, so the hook cannot
  ``await`` a gateway send. The confirmation reply is therefore scheduled
  out-of-band as a task on the running loop, and the toggle message itself is
  suppressed from the agent with ``{"action": "skip"}``.

Behavior:

* ``/autoflow on|off|status`` (or ``!autoflow`` / bare ``autoflow``): flips or
  reports the per-session mode, sends a confirmation, and skips the LLM turn.
* While ON, a *substantive* inbound message gets the steering directive
  appended (``rewrite``). Trivial/short messages and slash commands pass
  through untouched (``allow``).
* Gateway-only; CLI/TUI are unaffected (this hook only fires in the gateway
  dispatch path).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.autoflow import (
    apply_steering,
    is_substantive,
    parse_toggle_command,
    state as autoflow_state,
)
from ..core.config import load_config

logger = logging.getLogger(__name__)


def decide(
    text: str,
    *,
    is_on: bool,
    min_chars: int,
) -> dict[str, Any]:
    """Pure decision for an inbound message. No gateway, no side effects.

    Returns a dict the handler acts on:
      {"kind": "toggle", "command": "on"|"off"|"status"}
      {"kind": "steer",  "text": "<message + directive>"}
      {"kind": "pass"}
    """
    command = parse_toggle_command(text)
    if command is not None:
        return {"kind": "toggle", "command": command}
    if is_on and is_substantive(text, min_chars):
        return {"kind": "steer", "text": apply_steering(text)}
    return {"kind": "pass"}


def _send_confirmation(gateway: Any, source: Any, message: str) -> None:
    """Schedule an out-of-band confirmation reply on the running loop.

    The hook is sync and cannot await; we fire-and-forget a task. Topic/thread
    targeting is preserved via metadata={"thread_id": ...} so the reply lands
    in the originating Telegram topic / Discord thread.
    """
    try:
        adapter = gateway.adapters.get(source.platform)
    except Exception:
        adapter = None
    if adapter is None:
        return
    metadata = {"thread_id": source.thread_id} if getattr(source, "thread_id", None) else None

    async def _do_send() -> None:
        try:
            await adapter.send(source.chat_id, message, metadata=metadata)
        except Exception as exc:  # pragma: no cover - delivery best-effort
            logger.warning("autoflow confirmation send failed: %s", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do_send())
    except RuntimeError:
        # No running loop (shouldn't happen from the gateway dispatch path).
        logger.debug("autoflow: no running loop for confirmation send")


def pre_gateway_dispatch_handler(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **_: Any,
) -> dict[str, Any] | None:
    """Hermes ``pre_gateway_dispatch`` hook entry point.

    Returns a hook-result dict (``skip`` / ``rewrite``) or ``None`` (allow).
    """
    if event is None or gateway is None:
        return None
    source = getattr(event, "source", None)
    text = getattr(event, "text", None)
    if source is None or not isinstance(text, str):
        return None

    # Resolve the canonical session_key — the same key the reasoning-override
    # store uses. Without it we cannot scope autoflow per session, so bail.
    try:
        session_key = gateway._session_key_for_source(source)
    except Exception:
        return None
    if not session_key:
        return None

    cfg = load_config()
    store = autoflow_state()
    default_on = cfg.auto_workflow_default_on
    decision = decide(
        text,
        is_on=store.is_on(session_key, default_on),
        min_chars=cfg.auto_workflow_min_chars,
    )

    kind = decision.get("kind")

    if kind == "toggle":
        command = decision.get("command")
        if command == "on":
            store.set(session_key, True)
            _send_confirmation(
                gateway,
                source,
                "autoflow ON. Substantive messages this session will be "
                "steered toward the workflow tool. Launch approval still applies. "
                "Turn off with /autoflow off.",
            )
        elif command == "off":
            store.set(session_key, False)
            _send_confirmation(gateway, source, "autoflow OFF. Back to normal turn-by-turn handling.")
        else:  # status
            on = store.is_on(session_key, default_on)
            current = "ON" if on else "OFF"
            default_note = " (default-on for all sessions)" if default_on else ""
            _send_confirmation(
                gateway,
                source,
                f"autoflow is {current}{default_note} for this session. "
                "Use /autoflow on or /autoflow off.",
            )
        return {"action": "skip", "reason": "autoflow-toggle"}

    if kind == "steer":
        steered = decision.get("text") or text
        suffix = steered[len(text):] if steered.startswith(text) else steered
        return {"action": "rewrite", "position": "append", "text": suffix}

    return None
