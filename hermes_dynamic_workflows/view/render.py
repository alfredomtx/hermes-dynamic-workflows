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


def render_run_progress(run: dict[str, Any], *, max_chips: int = 8, verbose: bool = False, show_cost: bool = True) -> str:
    """Compact single-run progress block.

    Reused by the gateway live-progress bubble (launch / mid-run edits /
    completion) so the chat shows the same readable visual language as
    ``/workflows`` instead of raw telemetry. No id line: the launch/completion
    markers around the bubble already carry the Task ID.

    ``detailed=True`` selects the richer bubble layout (phase checklist for
    pipelines, per-agent elapsed for fan-out, aggregate tokens + estimated
    dollar cost in the header). ``show_cost`` gates the cost segment (config
    knob ``notify_progress_cost``). The ``/workflows`` multi-run overview keeps
    ``detailed=False`` so it stays a compact, glanceable one-liner per run.
    """
    return _render_run_block(run, max_chips=max_chips, verbose=verbose, include_ids=False, detailed=True, show_cost=show_cost)


def render_run_summary(run: dict[str, Any], *, show_cost: bool = True) -> str:
    """One-line collapsed completion head: emoji, name, duration, agent count,
    estimated cost, aggregate tokens. Used by the gateway bubble's final
    (completion) edit so a finished run collapses to a single summary line
    instead of leaving a wall of done-agent rows above the result.
    """
    snapshot = run.get("workflow") or {}
    meta = snapshot.get("meta") or {}
    name = meta.get("name") or run.get("source", {}).get("ref") or "workflow"
    status = run.get("status")
    totals = _totals(snapshot)
    head = f"{status_emoji(status)} {name}"
    duration = _duration(run, snapshot)
    if duration:
        head += f" · {_format_duration(duration)}"
    if totals["agents"]:
        head += f" · {totals['agents']} agent{'s' if totals['agents'] != 1 else ''}"
    if show_cost:
        cost = _format_cost(_total_cost(_all_agents(snapshot)))
        if cost:
            head += f" · {cost}"
    if totals.get("tokens"):
        head += f" · ~{_format_tokens(totals['tokens'])} tok"
    return head


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


def _render_run_block(
    run: dict[str, Any],
    *,
    max_chips: int,
    verbose: bool,
    include_ids: bool = False,
    detailed: bool = False,
    show_cost: bool = True,
) -> str:
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
    # Estimated dollar cost + aggregate tokens: bubble (detailed) only — keeps
    # the /workflows overview a compact one-liner and out of the no-telemetry
    # contract. Cost goes BEFORE tokens and is omitted when nothing is priced.
    if detailed and show_cost:
        cost = _format_cost(_total_cost(agents))
        if cost:
            head += f" · {cost}"
    if detailed and totals.get("tokens"):
        head += f" · ~{_format_tokens(totals['tokens'])} tok"
    if status and status not in {"running", "queued"}:
        head += f" · {status}"
    lines = [head]

    if not totals["agents"]:
        lines.append("   no agents started")
        return _append_ids(lines, run, include_ids)

    if detailed:
        # Richer bubble layout: phase checklist for pipelines (>=2 phases),
        # per-agent elapsed for single-phase fan-out. Cleanly REPLACES the
        # legacy phase+progress line and chip row below.
        phases = _phase_names(snapshot, recursive=False)
        if len(phases) >= 2:
            lines.extend(_phase_checklist(snapshot, agents))
            next_line = _next_phase_line(snapshot, phases)
            if next_line:
                lines.append(f"   {next_line}")
        else:
            # Fan-out: keep the phase + progress summary line, then per-agent
            # rows with their own elapsed.
            phase = _current_phase(snapshot)
            bits = [f"{totals['done']}/{totals['agents']} done"]
            if totals["running"]:
                bits.append(f"{totals['running']} running")
            if totals.get("errors"):
                bits.append(f"{totals['errors']} err")
            lines.append(f"   {phase + ' · ' if phase else ''}{' · '.join(bits)}")
            for line in _agent_lines(agents, max_rows=10):
                lines.append(f"   {line}")
        return _append_ids(lines, run, include_ids)

    # Compact overview layout (the /workflows list): phase + progress summary
    # line plus a single short chip row.
    phase = _current_phase(snapshot)
    progress = f"{totals['done']}/{totals['agents']} done"
    bits = [progress]
    if totals["running"]:
        bits.append(f"{totals['running']} running")
    if totals.get("errors"):
        bits.append(f"{totals['errors']} err")
    summary = " · ".join(bits)
    lines.append(f"   {phase + ' · ' if phase else ''}{summary}")
    chips = _agent_chips(agents, max_chips=max_chips)
    if chips:
        lines.append(f"   {chips}")

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


def _append_ids(lines: list[str], run: dict[str, Any], include_ids: bool) -> str:
    if include_ids:
        task_id = str(run.get("taskId") or "")
        run_id = str(run.get("runId") or "")
        id_bits = [bit for bit in (f"Task: {task_id}" if task_id else "", run_id) if bit]
        if id_bits:
            lines.append(f"   {' · '.join(id_bits)}")
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


# Markers for the detailed bubble layout. Distinct from _agent_marker (which
# uses ⏳ for running in the compact overview); the detailed view uses ▶ so the
# active rows read like a checklist.
_DETAIL_MARKER = {
    "queued": "◦",
    "running": "▶",
    "paused": "⏸",
    "done": "✓",
    "completed": "✓",
    "error": "✗",
    "failed": "✗",
    "skipped": "⏭",
}


def _detail_marker(status: Any) -> str:
    return _DETAIL_MARKER.get(str(status or ""), "◦")


def _agent_lines(agents: list[dict[str, Any]], *, max_rows: int = 10) -> list[str]:
    """Per-agent rows for the detailed bubble layout (fan-out and pipeline).

    One row each: ``<marker> <label> · <model>[ <effort>][ · <elapsed>][ · <N>
    tools]``. Each segment is omitted when its datum is absent. Running/errored
    agents come first (that is what a watcher cares about), then the rest in
    order, capped at ``max_rows`` with a ``… +K`` overflow tail. Elapsed
    reflects the agent's ``duration_seconds`` at snapshot time (ticks at
    snapshot cadence, not a live wall clock).
    """
    if not agents:
        return []
    active = [a for a in agents if a.get("status") in {"running", "error"}]
    rest = [a for a in agents if a.get("status") not in {"running", "error"}]
    ordered = active + rest
    shown = ordered[:max_rows]
    rows: list[str] = []
    for agent in shown:
        marker = _detail_marker(agent.get("status"))
        label = _chip_label(agent)
        segs = [f"{marker} {label}"]
        model_seg = _model_segment(agent)
        if model_seg:
            segs.append(model_seg)
        duration = agent.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            segs.append(_format_duration(duration))
        tools = agent.get("tool_calls")
        if isinstance(tools, int) and tools > 0:
            segs.append(f"{tools} tool{'s' if tools != 1 else ''}")
        rows.append(" · ".join(segs))
    hidden = len(agents) - len(shown)
    if hidden > 0:
        rows.append(f"… +{hidden}")
    return rows


def _model_segment(agent: dict[str, Any]) -> str:
    """``<short-model>[ <effort>]`` for a roster row, or "" when no model."""
    model = _short_model(agent.get("model"))
    if not model:
        return ""
    effort = str(agent.get("reasoning_effort") or "").strip()
    return f"{model} {effort}" if effort else model


def _short_model(model: Any) -> str:
    """Strip a dotted region/vendor prefix from a model id for display.

    Only strips when EVERY segment before the last dotted token is a bare alpha
    word (region/vendor like ``us``/``anthropic``/``openai``) so a version dot
    is never mistaken for a prefix:
      us.anthropic.claude-opus-4-8 -> claude-opus-4-8
      anthropic.claude-opus-4-8    -> claude-opus-4-8
      gpt-5.5                      -> gpt-5.5   (5.5 has a non-alpha segment)
      gpt-4.1-mini                 -> gpt-4.1-mini
    """
    text = str(model or "").strip()
    if not text:
        return ""
    segs = text.split(".")
    if len(segs) > 1 and all(s.isalpha() for s in segs[:-1]):
        text = segs[-1]
    return text


def _agent_cost(agent: dict[str, Any]) -> "Any":
    """Estimated dollar cost (Decimal) for one agent, or None when not priceable.

    Builds CanonicalUsage from the per-agent input/output + cache buckets
    (input_tokens is the UNCACHED bucket, so cache is not double-counted) and
    routes through Hermes core's estimate_usage_cost. Returns the amount only
    for an estimated/actual status; ``included`` (subscription, e.g. codex) and
    ``unknown`` (no pricing route) return None so they neither show $0 nor an
    n/a. Import is lazy + guarded so an older core without the API simply
    yields no cost rather than crashing the renderer.
    """
    model = str(agent.get("model") or "").strip()
    if not model:
        return None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception:
        return None
    usage = CanonicalUsage(
        input_tokens=_as_int(agent.get("input_tokens")),
        output_tokens=_as_int(agent.get("output_tokens")),
        cache_read_tokens=_as_int(agent.get("cache_read_tokens")),
        cache_write_tokens=_as_int(agent.get("cache_write_tokens")),
    )
    if usage.total_tokens <= 0:
        return None
    try:
        result = estimate_usage_cost(
            model,
            usage,
            provider=agent.get("provider") or None,
            base_url=agent.get("base_url") or None,
        )
    except Exception:
        return None
    if result.status in {"estimated", "actual"} and result.amount_usd is not None:
        return result.amount_usd
    return None


def _total_cost(agents: list[dict[str, Any]]) -> "Any":
    """Sum of per-agent estimated cost (Decimal), or None when nothing priced.

    Each agent is priced by its OWN model, so a mixed run (e.g. codex
    ``included`` + opus ``estimated``) sums correctly: codex contributes
    nothing, opus contributes its amount. A pure-included or all-unknown run
    yields None → the cost segment is omitted entirely.
    """
    total = None
    for agent in agents:
        amount = _agent_cost(agent)
        if amount is None:
            continue
        total = amount if total is None else total + amount
    return total


def _format_cost(amount: "Any") -> str:
    """``~$X.XX`` for a Decimal amount; "" when None/zero; ``<$0.01`` for a
    genuinely-known sub-cent amount (never ``$0.00``)."""
    if amount is None:
        return ""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value < 0.005:
        return "<$0.01"
    return f"~${value:.2f}"


def _phase_checklist(snapshot: dict[str, Any], agents: list[dict[str, Any]]) -> list[str]:
    """Checklist of pipeline phases for the detailed bubble layout.

    One line per ordered phase: ``<mark> <title>  <done>/<total> done[· R
    running][· E err]``. Marker precedence is all-done ✓ wins over active ▶ wins
    over pending ◦. The active phase reuses ``_current_phase`` so the bubble and
    the rest of the renderer agree; the ``phases[-1]`` fallback inside it is
    guarded here so an unstarted or fully-done pipeline never bolds a phantom
    active phase. Unphased agents collapse into a trailing ``[Other]`` row so the
    per-phase counts always reconcile with the header total.
    """
    phases = _phase_names(snapshot, recursive=False)
    by_phase: dict[str, list[dict[str, Any]]] = {phase: [] for phase in phases}
    unphased: list[dict[str, Any]] = []
    for agent in agents:
        phase = agent.get("phase")
        if phase and phase in by_phase:
            by_phase[phase].append(agent)
        elif phase and phase not in by_phase:
            # Agent-derived phase not in the declared list: keep it visible.
            by_phase.setdefault(phase, []).append(agent)
            if phase not in phases:
                phases.append(phase)
        else:
            unphased.append(agent)

    active = _current_phase(snapshot)
    any_running = any(a.get("status") == "running" for a in agents)

    def _phase_row(title: str, members: list[dict[str, Any]]) -> str:
        total = len(members)
        done = sum(1 for a in members if a.get("status") in {"done", "completed", "skipped"})
        running = sum(1 for a in members if a.get("status") == "running")
        errors = sum(1 for a in members if a.get("status") in {"error", "failed"})
        all_done = total > 0 and done == total
        phase_running = running > 0
        # Active ▶ only when this phase genuinely has work in flight, or it is
        # the resolved current phase AND something is actually running somewhere
        # (guards the phases[-1] fallback for unstarted/all-done pipelines).
        is_active = phase_running or (title == active and any_running and not all_done)
        if all_done:
            mark = "✓"
        elif is_active:
            mark = "▶"
        else:
            mark = "◦"
        bits = [f"{done}/{total} done"]
        if running:
            bits.append(f"{running} running")
        if errors:
            bits.append(f"{errors} err")
        body = f"{mark} {title}  {' · '.join(bits)}"
        return f"**{body}**" if mark == "▶" else body

    # Per-phase rows, with the running/errored agents of EACH in-flight phase
    # shown beneath their phase line. pipeline() is non-barrier, so more than
    # one phase can have work in flight at once (B5) — show them all, not just
    # the resolved "active" phase. A global budget caps total agent rows across
    # phases so a wide multi-phase fan-out can't blow the Telegram length cap.
    rows: list[str] = []
    agent_row_budget = 12
    for title in phases:
        members = by_phase.get(title, [])
        rows.append(f"   {_phase_row(title, members)}")
        if agent_row_budget <= 0:
            continue
        in_flight = [a for a in members if a.get("status") in {"running", "error", "failed"}]
        if not in_flight:
            continue
        shown = in_flight[:agent_row_budget]
        for line in _agent_lines(shown, max_rows=len(shown)):
            rows.append(f"      {line}")
        agent_row_budget -= len(shown)
        hidden = len(in_flight) - len(shown)
        if hidden > 0:
            rows.append(f"      … +{hidden}")
    if unphased:
        rows.append(f"   {_phase_row('[Other]', unphased)}")
    return rows


def _next_phase_line(snapshot: dict[str, Any], phases: list[str]) -> str:
    """Lookahead line for the pipeline bubble: ``Next: <detail or title>`` of the
    first not-yet-started phase after the active one. Empty when the active phase
    is the last, or when nothing follows. Prefers the declared
    ``snapshot["phases"][i].detail`` (top-level key), falling back to the title;
    tolerates agent-derived phases that have no declared entry.
    """
    if not phases:
        return ""
    active = _current_phase(snapshot)
    try:
        active_idx = phases.index(active)
    except ValueError:
        active_idx = -1
    if active_idx < 0 or active_idx >= len(phases) - 1:
        return ""
    next_title = phases[active_idx + 1]
    declared = snapshot.get("phases") or []
    detail = ""
    for entry in declared:
        if isinstance(entry, dict) and str(entry.get("title") or "").strip() == next_title:
            detail = str(entry.get("detail") or "").strip()
            break
    return f"Next: {preview(detail, 80) if detail else next_title}"


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
