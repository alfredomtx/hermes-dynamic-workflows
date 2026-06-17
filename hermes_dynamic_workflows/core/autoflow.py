"""Autoflow: ultracode-style auto-workflow steering state and decisions.

A per-session mode, toggled with ``/autoflow on|off`` in the gateway. While a
session is ON, the ``pre_gateway_dispatch`` hook (see
``adapters/autoflow_hook.py``) does two things to each *substantive* inbound
message:

1. bumps the session's reasoning effort to ``auto_workflow_effort`` (the
   "xhigh" half of Claude Code's ultracode), and
2. appends a steering directive to the message text that nudges the model to
   prefer the ``workflow`` tool (the "auto-workflow" half).

This module is deliberately gateway-free: it holds only the per-session state
store and the pure decision helpers, so it can be unit-tested without a live
gateway. All gateway coupling lives in the hook adapter.

Design constraints baked in here (verified against Hermes core):

* It is a NUDGE, not a hard force. There is no ``tool_choice`` available to
  plugins, so the directive *encourages* the workflow tool; the model still
  decides (matching ultracode's "Claude decides" model).
* The keyword path (``ultracode``/``use a workflow`` typed inline) is handled
  by Hermes itself and the existing opt-in; autoflow only adds the *sticky
  session mode* that needs no keyword per message.
* Launch approval is unchanged — autoflow only steers; the workflow tool's own
  ``require_launch_approval`` still gates every launch.
"""

from __future__ import annotations

import threading


# Commands that flip the session mode. Parsed from the leading token of an
# inbound message after stripping a single leading slash/bang.
_ON_WORDS = frozenset({"on", "enable", "enabled", "start", "1", "true", "yes"})
_OFF_WORDS = frozenset({"off", "disable", "disabled", "stop", "0", "false", "no"})

# The steering block appended to a substantive user message while autoflow is
# ON. Kept compact and explicit. It does NOT force the tool — it tells the
# model the task is pre-authorized for orchestration so it can skip the usual
# "ask the user first" gate the workflow tool's description imposes.
STEERING_DIRECTIVE = (
    "[autoflow on] The user has autoflow enabled for this session: they have "
    "pre-authorized multi-agent orchestration. If this task is substantive "
    "enough to benefit from decomposition, parallel coverage, adversarial "
    "verification, or scale one context can't hold, prefer the `workflow` "
    "tool (write an inline dynamic workflow or run a saved one) instead of "
    "doing it turn-by-turn. Announce the launch; do not ask permission to "
    "orchestrate. For trivial work, answer directly as usual. This is a "
    "preference, not a command — you still decide."
)


class AutoflowState:
    """Thread-safe per-session ON/OFF store.

    Keyed by the gateway's canonical session_key (the same key
    ``_set_session_reasoning_override`` / ``_resolve_session_reasoning_config``
    use). Sticky: a session's explicit `/autoflow on|off` choice persists for
    the process lifetime (not persisted to disk, matching ultracode resetting
    on a new session).

    Override semantics: the store records only EXPLICIT per-session choices. A
    session with no explicit choice resolves to the ``default`` passed to
    ``is_on`` (driven by ``auto_workflow_default_on``). So with default-on, a
    fresh session is ON until it runs ``/autoflow off``; with default-off, it
    is OFF until ``/autoflow on``. ``/autoflow off`` under default-on records an
    explicit False that survives — it is not the same as "unset".
    """

    def __init__(self) -> None:
        self._explicit: dict[str, bool] = {}
        self._lock = threading.RLock()

    def is_on(self, session_key: str, default: bool = False) -> bool:
        if not session_key:
            return False
        with self._lock:
            return self._explicit.get(session_key, default)

    def set(self, session_key: str, enabled: bool) -> bool:
        """Record an explicit per-session choice. Returns the resulting state."""
        if not session_key:
            return False
        with self._lock:
            self._explicit[session_key] = bool(enabled)
            return self._explicit[session_key]

    def clear(self, session_key: str) -> None:
        """Drop the explicit choice so the session falls back to the default."""
        with self._lock:
            self._explicit.pop(session_key, None)


# Module-level singleton: the hook and the command share one store, and they
# run in the same gateway process.
_STATE = AutoflowState()


def state() -> AutoflowState:
    return _STATE


def parse_toggle_command(text: str) -> str | None:
    """Classify an inbound message as an autoflow toggle command.

    Returns ``"on"``, ``"off"``, ``"status"``, or ``None`` (not an autoflow
    command). Accepts ``/autoflow``, ``!autoflow``, or bare ``autoflow`` as the
    first token, with an optional on/off/status argument. A bare ``/autoflow``
    with no argument is treated as a status query (non-destructive).
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    parts = stripped.split()
    head = parts[0].lstrip("/!").lower()
    if head != "autoflow":
        return None
    if len(parts) == 1:
        return "status"
    arg = parts[1].lower()
    if arg in _ON_WORDS:
        return "on"
    if arg in _OFF_WORDS:
        return "off"
    if arg == "status":
        return "status"
    # Unrecognized arg -> treat as status so we never silently misfire a flip.
    return "status"


def is_substantive(text: str, min_chars: int) -> bool:
    """Cheap prefilter: is this message worth steering/effort-bumping?

    Pure length gate on the stripped text — no LLM call. Trivial acks fall
    through untouched so they don't pay xhigh latency. A command-like message
    (starts with ``/`` or ``!``) is never substantive: slash commands route to
    their own handlers, not the agent.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in "/!":
        return False
    return len(stripped) >= max(1, int(min_chars))


def apply_steering(text: str) -> str:
    """Append the steering directive to a user message.

    Idempotent: if the directive marker is already present (e.g. a re-entrant
    dispatch), the text is returned unchanged so it is never doubled.
    """
    base = text if isinstance(text, str) else ""
    if "[autoflow on]" in base:
        return base
    if not base.strip():
        return STEERING_DIRECTIVE
    return base + "\n\n" + STEERING_DIRECTIVE
