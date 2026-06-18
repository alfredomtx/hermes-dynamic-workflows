"""Text rendering for workflow status snapshots.

Two audiences, two densities:

* The compact, glanceable renderers (``render_agent_overview``,
  ``render_run_progress``) are what a human reads in a chat / the ``/workflows``
  command. They use emoji status, a phase + done/total summary, and short agent
  chips. Per-agent token/cache/tool telemetry is noise here and is hidden unless
  ``verbose=True``.
* The detailed tree (``render_workflow_text``) and the verbose row
  (``render_agent_row``) keep the full instrumentation for the saved markdown
  record and ``verbose`` callers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.text import preview

# Hard cap on an agent label inside a compact chip. Agent labels are often the
# full prompt (a paragraph), which is what made the old overview unreadable.
_CHIP_LABEL_MAX = 32
_RUNNING_STATES = {"queued", "running", "paused", "stopping"}


def render_workflow_text(snapshot: dict[str, Any], *, completed: bool = True, max_agents: int = 12) -> str:
    meta = snapshot.get("meta") or {}
    name = meta.get("name") or "dynamic-workflow"
    errors = _all_errors(snapshot)
    totals = _totals(snapshot)
    status = "completed" if completed else "running"

    header = f"- Workflow: {name} ({totals['done']}/{totals['agents']} done)"
    if totals["running"]:
        header += f", {totals['running']} running"
    if errors:
        header += f", {len(errors)} error(s)"

    parts = [f"Workflow {status}", header]
    _render_frame_tree(parts, snapshot, indent="  ", max_agents=max_agents)
    return "\n".join(parts)


def render_agent_overview(
    runs: list[dict[str, Any]],
    *,
    max_chips_per_run: int = 6,
    verbose: bool = False,
) -> str:
    """Glanceable multi-run summary for the ``/workflows`` command.

    One compact block per run: an emoji status, the workflow name, elapsed time,
    a phase + done/total/running line, a row of short agent chips, and a short
    id line (taskId/runId) because ``/workflows`` is the control surface used to
    stop/resume a run. Pass ``verbose=True`` to append the old per-agent
    telemetry rows (tokens, cache reads, tool counts, agent type) under each run.
    """
    if not runs:
        return "No workflow runs found.\n\nRun `hermes-workflows` in a terminal for live monitoring and controls."
    running = sum(1 for run in runs if run.get("status") in _RUNNING_STATES)
    completed = sum(1 for run in runs if run.get("status") == "completed")
    header_counts = []
    if running:
        header_counts.append(f"{running} running")
    if completed:
        header_counts.append(f"{completed} completed")
    other = len(runs) - running - completed
    if other > 0:
        header_counts.append(f"{other} other")
    lines = ["Dynamic workflows", " · ".join(header_counts) or f"{len(runs)} run(s)", ""]
    for run in runs:
        lines.append(_render_run_block(run, max_chips=max_chips_per_run, verbose=verbose, include_ids=True))
        lines.append("")
    lines.append("Run `hermes-workflows` in a terminal for live monitoring and controls.")
    return "\n".join(lines).rstrip()


def render_run_progress(run: dict[str, Any], *, max_chips: int = 8, verbose: bool = False) -> str:
    """Compact single-run progress block.

    Reused by the gateway live-progress bubble (launch / mid-run edits /
    completion) so the chat shows the same readable visual language as
    ``/workflows`` instead of raw telemetry. No id line: the launch/completion
    markers around the bubble already carry the Task ID.
    """
    return _render_run_block(run, max_chips=max_chips, verbose=verbose, include_ids=False)


def render_saved_markdown(run: dict[str, Any]) -> str:
    snapshot = run.get("workflow") or {}
    completed = run.get("status") not in _RUNNING_STATES
    lines = ["# Workflow Run", "", render_workflow_text(snapshot, completed=completed), ""]
    if run.get("result") is not None:
        lines.extend(["## Result", "", preview(run.get("result"), 4000), ""])
    errors = _all_errors(snapshot)
    if errors:
        lines.extend(["## Errors", ""])
        lines.extend(f"- {preview(error, 300)}" for error in errors)
        lines.append("")
    return "\n".join(lines)


def _render_run_block(run: dict[str, Any], *, max_chips: int, verbose: bool, include_ids: bool = False) -> str:
    snapshot = run.get("workflow") or {}
    meta = snapshot.get("meta") or {}
    name = meta.get("name") or run.get("source", {}).get("ref") or "workflow"
    status = run.get("status")
    totals = _totals(snapshot)
    agents = _all_agents(snapshot)

    # Line 1: status emoji, name, elapsed, terminal status word.
    head = f"{status_emoji(status)} {name}"
    duration = _duration(run, snapshot)
    if duration:
        head += f" · {_format_duration(duration)}"
    if status and status not in {"running", "queued"}:
        head += f" · {status}"
    lines = [head]

    # Line 2: phase + progress fraction + running + errors.
    if totals["agents"]:
        phase = _current_phase(snapshot)
        progress = f"{totals['done']}/{totals['agents']} done"
        bits = [progress]
        if totals["running"]:
            bits.append(f"{totals['running']} running")
        if totals.get("errors"):
            bits.append(f"{totals['errors']} err")
        summary = " · ".join(bits)
        lines.append(f"   {phase + ' · ' if phase else ''}{summary}")
        # Line 3: agent chips (done/running/errored), truncated.
        chips = _agent_chips(agents, max_chips=max_chips)
        if chips:
            lines.append(f"   {chips}")
    else:
        lines.append("   no agents started")

    if include_ids:
        task_id = str(run.get("taskId") or "")
        run_id = str(run.get("runId") or "")
        id_bits = [bit for bit in (f"Task: {task_id}" if task_id else "", run_id) if bit]
        if id_bits:
            lines.append(f"   {' · '.join(id_bits)}")

    if verbose and agents:
        for agent in agents[: max(max_chips, len(agents))]:
            lines.append(f"   - {render_agent_row(agent)}")
    return "\n".join(lines)


def _agent_chips(agents: list[dict[str, Any]], *, max_chips: int) -> str:
    """Render agents as short ``<marker> <label>`` chips.

    Running and errored agents are surfaced first (they are what a watcher cares
    about), then the rest in order, capped at ``max_chips`` with a ``… +K``
    overflow tail.
    """
    if not agents:
        return ""
    active = [a for a in agents if a.get("status") in {"running", "error"}]
    rest = [a for a in agents if a.get("status") not in {"running", "error"}]
    ordered = active + rest
    shown = ordered[:max_chips]
    chips = [f"{_agent_marker(a.get('status'))} {_chip_label(a)}" for a in shown]
    hidden = len(agents) - len(shown)
    if hidden > 0:
        chips.append(f"… +{hidden}")
    return "  ".join(chips)


def _chip_label(agent: dict[str, Any]) -> str:
    label = str(agent.get("label") or f"agent-{agent.get('id', '?')}")
    label = " ".join(label.split())  # collapse newlines/whitespace runs
    if len(label) > _CHIP_LABEL_MAX:
        label = label[: _CHIP_LABEL_MAX - 1].rstrip() + "…"
    return label


def render_agent_row(agent: dict[str, Any]) -> str:
    status = status_icon(agent.get("status"))
    label = agent.get("label") or f"agent-{agent.get('id', '?')}"
    parts = [f"#{agent.get('id')} {status} {label}"]
    if agent.get("model"):
        parts.append(str(agent.get("model")))
    if agent.get("tokens"):
        parts.append(f"{_format_tokens(agent.get('tokens'))} tok")
    if agent.get("cache_read_tokens"):
        parts.append(f"{_format_tokens(agent.get('cache_read_tokens'))} cached read")
    if agent.get("cache_write_tokens"):
        parts.append(f"{_format_tokens(agent.get('cache_write_tokens'))} cache write")
    if agent.get("tool_calls"):
        parts.append(f"{agent.get('tool_calls')} tools")
    if agent.get("agent_type"):
        parts.append(f"type:{agent.get('agent_type')}")
    if agent.get("isolation") == "worktree":
        parts.append("worktree")
    structured = agent.get("structured")
    if isinstance(structured, dict):
        structured_status = structured.get("status")
        if structured_status == "failed":
            parts.append("schema failed")
    if agent.get("error"):
        parts.append(preview(agent.get("error"), 120))
    return " . ".join(parts)


def status_emoji(status: Any) -> str:
    return {
        "queued": "🕓",
        "running": "🔄",
        "stopping": "🛑",
        "paused": "⏸",
        "completed": "✅",
        "done": "✅",
        "error": "❌",
        "failed": "❌",
        "stopped": "⏹",
        "interrupted": "⚠️",
        "skipped": "⏭",
    }.get(str(status or ""), "•")


def _agent_marker(status: Any) -> str:
    return {
        "queued": "·",
        "running": "⏳",
        "paused": "⏸",
        "done": "✓",
        "completed": "✓",
        "error": "✗",
        "failed": "✗",
        "skipped": "⏭",
    }.get(str(status or ""), "·")


def status_icon(status: Any) -> str:
    return {
        "queued": ".",
        "running": "*",
        "stopping": "~",
        "paused": "=",
        "completed": "+",
        "done": "+",
        "error": "!",
        "failed": "!",
        "stopped": "x",
        "interrupted": "#",
        "skipped": "-",
    }.get(str(status or ""), "?")


def _current_phase(snapshot: dict[str, Any]) -> str:
    """Best-effort label of the phase the run is currently working in.

    The phase of a running agent wins (that is where work is happening now);
    otherwise the phase of the most recently touched agent; otherwise the last
    declared phase. Empty string when the run has no phases at all.
    """
    agents = _all_agents(snapshot)
    for agent in reversed(agents):
        if agent.get("status") == "running" and agent.get("phase"):
            return str(agent["phase"])
    for agent in reversed(agents):
        if agent.get("phase"):
            return str(agent["phase"])
    phases = _phase_names(snapshot, recursive=True)
    return phases[-1] if phases else ""


def _render_frame_tree(parts: list[str], frame: dict[str, Any], *, indent: str, max_agents: int) -> None:
    phases = _phase_names(frame, recursive=False)
    rendered_ids: set[Any] = set()
    agents = frame.get("agents") or []
    for phase in phases:
        parts.append(f"{indent}[{phase}]")
        for agent in agents[-max_agents:]:
            if agent.get("phase") == phase:
                parts.append(_render_agent(agent, indent + "  "))
                rendered_ids.add(agent.get("id"))
    unphased = [agent for agent in agents[-max_agents:] if agent.get("id") not in rendered_ids]
    if unphased:
        if phases:
            parts.append(f"{indent}[Other]")
        for agent in unphased:
            parts.append(_render_agent(agent, indent + "  "))
    hidden = max(0, len(agents) - max_agents)
    if hidden:
        parts.append(f"{indent}... {hidden} earlier agent(s)")
    for line in (frame.get("logs") or [])[-5:]:
        parts.append(f"{indent}log: {preview(line, 120)}")
    for child in frame.get("children") or []:
        child_meta = child.get("meta") or {}
        child_name = child_meta.get("name") or child.get("source_ref") or "workflow"
        child_totals = _totals(child)
        parts.append(
            f"{indent}> {child_name} "
            f"({child_totals['done']}/{child_totals['agents']} done)"
        )
        _render_frame_tree(parts, child, indent=indent + "  ", max_agents=max_agents)


def _render_agent(agent: dict[str, Any], indent: str) -> str:
    marker = {
        "queued": ".",
        "running": "*",
        "done": "+",
        "error": "!",
        "skipped": "-",
    }.get(agent.get("status"), "?")
    label = agent.get("label") or f"agent-{agent.get('id', '?')}"
    line = f"{indent}#{agent.get('id')} {marker} {label}"
    structured = agent.get("structured")
    if isinstance(structured, dict) and structured.get("status") == "failed":
        line += f" [{structured.get('status')}]"
    if agent.get("error"):
        line += f" - {preview(agent['error'], 100)}"
    return line


def _phase_names(snapshot: dict[str, Any], *, recursive: bool) -> list[str]:
    phases: list[str] = []
    for phase in snapshot.get("phases") or []:
        if isinstance(phase, dict):
            title = str(phase.get("title") or "").strip()
        else:
            title = str(phase or "").strip()
        if title and title not in phases:
            phases.append(title)
    for agent in snapshot.get("agents") or []:
        phase = agent.get("phase")
        if phase and phase not in phases:
            phases.append(str(phase))
    if recursive:
        for child in snapshot.get("children") or []:
            for phase in _phase_names(child, recursive=True):
                if phase not in phases:
                    phases.append(phase)
    return phases


def _all_agents(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    agents = list(snapshot.get("agents") or [])
    for child in snapshot.get("children") or []:
        if isinstance(child, dict):
            agents.extend(_all_agents(child))
    return agents


def _all_errors(snapshot: dict[str, Any]) -> list[str]:
    errors = [str(error) for error in snapshot.get("errors") or []]
    for child in snapshot.get("children") or []:
        if isinstance(child, dict):
            errors.extend(_all_errors(child))
    return errors


def _duration(run: dict[str, Any], snapshot: dict[str, Any]) -> float:
    value = snapshot.get("duration_seconds")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    # Live runs may not have a snapshot duration yet; derive from wall clock so
    # the progress bubble shows a ticking elapsed time between edits.
    started = _parse_iso(run.get("startedAt"))
    if started is None:
        return 0.0
    finished = _parse_iso(run.get("finishedAt")) or datetime.now(timezone.utc)
    return max(0.0, (finished - started).total_seconds())


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _totals(snapshot: dict[str, Any]) -> dict[str, int]:
    provided = snapshot.get("totals")
    if isinstance(provided, dict):
        return {
            "agents": _as_int(provided.get("agents")),
            "done": _as_int(provided.get("done")),
            "running": _as_int(provided.get("running")),
            "errors": _as_int(provided.get("errors")),
            "tokens": _as_int(provided.get("tokens")),
            "tool_calls": _as_int(provided.get("tool_calls")),
            "cache_read_tokens": _as_int(provided.get("cache_read_tokens")),
            "cache_write_tokens": _as_int(provided.get("cache_write_tokens")),
        }
    agents = _all_agents(snapshot)
    return {
        "agents": len(agents),
        "done": sum(1 for agent in agents if agent.get("status") == "done"),
        "running": sum(1 for agent in agents if agent.get("status") == "running"),
        "errors": len(_all_errors(snapshot)) + sum(1 for agent in agents if agent.get("status") == "error"),
        "tokens": sum(_as_int(agent.get("tokens")) for agent in agents),
        "tool_calls": sum(_as_int(agent.get("tool_calls")) for agent in agents),
        "cache_read_tokens": sum(_as_int(agent.get("cache_read_tokens")) for agent in agents),
        "cache_write_tokens": sum(_as_int(agent.get("cache_write_tokens")) for agent in agents),
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_tokens(value: Any) -> str:
    number = _as_int(value)
    if number >= 1000:
        return f"{number / 1000:.1f}K"
    return str(number)


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
