"""Slash commands for workflow run inspection and control."""

from __future__ import annotations

import os
from typing import Any

from ..run.manager import get_run_manager
from ..view.render import render_agent_overview


def workflows_command(raw_args: str = "", *, plugin_context: Any = None) -> str:
    arg = (raw_args or "").strip()
    if arg:
        return "Usage: /workflows\nFor live monitoring and controls, run `hermes-workflows` in a terminal."
    manager = get_run_manager()
    session_id = _current_session_id(plugin_context) or None
    # Session-scoped view first. The gateway mints a fresh session id on each
    # restart / new-session window, while a run is tagged with the session id
    # active at launch — so after a restart the strict filter hides every prior
    # run ("No workflow runs found." despite live runs existing). Fall back to
    # the recent-runs view (unfiltered) so runs stay visible across restarts.
    if session_id:
        scoped = manager.list(limit=12, session_id=session_id)
        if scoped:
            return render_agent_overview(scoped)
    return render_agent_overview(manager.list(limit=12, session_id=None))


def _current_session_id(plugin_context: Any = None) -> str:
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
        value = _session_env(name)
        if value:
            return value
    return ""


def _plugin_context_cli_ref(plugin_context: Any) -> Any:
    manager = getattr(plugin_context, "_manager", None) if plugin_context is not None else None
    if manager is not None:
        return getattr(manager, "_cli_ref", None)
    return None


def _session_env(name: str) -> str:
    try:
        from ..host import gateway as host_gateway

        value = str(host_gateway.raw_session_env(name, "") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return os.getenv(name, "").strip()
