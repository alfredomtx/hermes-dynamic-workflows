"""Pure text rendering for the full-screen workflow TUI."""

from __future__ import annotations

import textwrap
import unicodedata
from dataclasses import dataclass

from .model import AgentView, PhaseView, WorkflowView


@dataclass(frozen=True)
class RenderState:
    view: str = "list"
    run_index: int = 0
    phase_index: int = 0
    agent_index: int = 0
    message: str = ""


def render_screen(
    workflows: list[WorkflowView],
    state: RenderState,
    *,
    width: int,
    height: int,
) -> list[str]:
    width = max(40, width)
    height = max(12, height)
    if state.view == "workflow" and workflows:
        lines = _render_workflow(workflows[_clamp(state.run_index, len(workflows))], state, width, height)
    elif state.view == "agent" and workflows:
        lines = _render_agent(workflows[_clamp(state.run_index, len(workflows))], state, width, height)
    else:
        lines = _render_list(workflows, state, width)
    return _fit(lines, width, height)


def _render_list(workflows: list[WorkflowView], state: RenderState, width: int) -> list[str]:
    running = sum(workflow.running for workflow in workflows)
    completed = sum(workflow.status == "completed" for workflow in workflows)
    lines = ["", "  Dynamic workflows", f"  {running} running . {completed} completed", ""]
    if not workflows:
        lines.extend(
            [
                "  No workflow runs found.",
                "",
                "  Start a workflow in Hermes, then this panel will refresh automatically.",
            ]
        )
    for index, workflow in enumerate(workflows):
        selected = index == _clamp(state.run_index, len(workflows))
        marker = ">" if selected else " "
        lines.append(
            _crop(
                f"  {marker} {_status_icon(workflow.status)} {workflow.name}  "
                f"{len(workflow.agents)} agents . {_tokens(workflow.tokens)} tok . "
                f"{_duration(workflow.duration_seconds)}",
                width,
            )
        )
        if selected and workflow.description and workflow.description != workflow.name:
            lines.append(_crop(f"      {workflow.description}", width))
    lines.extend(["", _footer("Up/Down select . Enter view . x stop . p pause/resume . r restart . s save . q close", state.message, width)])
    return lines


def _render_workflow(workflow: WorkflowView, state: RenderState, width: int, height: int) -> list[str]:
    phase_index = _clamp(state.phase_index, len(workflow.phases))
    phase = workflow.phases[phase_index] if workflow.phases else PhaseView("Agents", ())
    progress = f"{workflow.done}/{len(workflow.agents)} agents . {_duration(workflow.duration_seconds)}"
    header = [
        "",
        _left_right(f"  {workflow.name}", progress + "  ", width),
        _crop(f"  {workflow.description}", width),
        "",
    ]
    left_width = max(24, min(34, width // 4))
    right_width = max(20, width - left_width - 5)
    body_height = max(5, height - len(header) - 3)

    left = []
    for index, item in enumerate(workflow.phases):
        marker = ">" if index == phase_index else " "
        suffix = "not started" if not item.agents else f"{item.done}/{len(item.agents)}"
        left.append(f"{marker} {index + 1} {item.title} {suffix}")
    right = []
    if not phase.agents:
        right.append("Not started yet")
    for agent in phase.agents:
        right.append(
            f"{_status_icon(agent.status)} {agent.label}"
            + (f"  {agent.model}" if agent.model else "")
            + f"  {_tokens(agent.tokens)} tok . {agent.tool_calls} tools"
        )
    panel = _two_panel(
        "Phases",
        left,
        f"{phase.title} . {len(phase.agents)} agents",
        right,
        left_width=left_width,
        right_width=right_width,
        height=body_height,
    )
    return header + panel + [
        _footer("Up/Down phase . Enter agent . x stop . p pause/resume . r restart . s save . Esc back", state.message, width)
    ]


def _render_agent(workflow: WorkflowView, state: RenderState, width: int, height: int) -> list[str]:
    phase_index = _clamp(state.phase_index, len(workflow.phases))
    phase = workflow.phases[phase_index] if workflow.phases else PhaseView("Agents", workflow.agents)
    agents = list(phase.agents)
    agent_index = _clamp(state.agent_index, len(agents))
    agent = agents[agent_index] if agents else None
    progress = f"{workflow.done}/{len(workflow.agents)} agents . {_duration(workflow.duration_seconds)}"
    header = [
        "",
        _left_right(f"  {workflow.name}", progress + "  ", width),
        _crop(f"  {workflow.description}", width),
        "",
    ]
    left_width = max(30, min(42, width // 3))
    right_width = max(20, width - left_width - 5)
    body_height = max(5, height - len(header) - 3)
    left = [
        f"{'>' if index == agent_index else ' '} {_status_icon(item.status)} {item.label}"
        for index, item in enumerate(agents)
    ]
    panel = _two_panel(
        f"{phase.title} . {len(agents)} agents",
        left,
        agent.label if agent else "Agent",
        _agent_detail(agent, right_width - 2),
        left_width=left_width,
        right_width=right_width,
        height=body_height,
    )
    return header + panel + [
        _footer("Up/Down agent . x stop . p pause/resume . r restart . s save . Esc back", state.message, width)
    ]


def _agent_detail(agent: AgentView | None, width: int) -> list[str]:
    if agent is None:
        return ["No agents in this phase."]
    lines = [
        f"{_status_label(agent.status)}" + (f" . {agent.model}" if agent.model else ""),
        f"{_tokens(agent.tokens)} tok . {agent.tool_calls} tool calls",
        "",
        f"Prompt . {len(agent.prompt.splitlines()) or 1} lines",
    ]
    lines.extend(_wrapped_preview(agent.prompt, width, 4))
    lines.extend(["", f"Activity . last {min(3, len(agent.activity))} of {len(agent.activity)}"])
    if agent.activity:
        lines.extend(_crop(item, width) for item in agent.activity[-3:])
    else:
        lines.append("No tool activity yet")
    lines.extend(["", "Outcome"])
    lines.extend(_wrapped_preview(agent.outcome, width, 5))
    return lines


def _two_panel(
    left_title: str,
    left_lines: list[str],
    right_title: str,
    right_lines: list[str],
    *,
    left_width: int,
    right_width: int,
    height: int,
) -> list[str]:
    left_inner = max(1, left_width - 2)
    right_inner = max(1, right_width - 2)
    left_heading = _crop(left_title, max(1, left_inner - 2))
    right_heading = _crop(right_title, max(1, right_inner - 2))
    top = f"  ┌─ {left_heading} " + "─" * max(0, left_inner - _display_width(left_heading) - 3)
    top += "┬─ " + right_heading + " " + "─" * max(0, right_inner - _display_width(right_heading) - 3) + "┐"
    lines = [top]
    for index in range(height):
        left = _crop(left_lines[index], left_inner) if index < len(left_lines) else ""
        right = _crop(right_lines[index], right_inner) if index < len(right_lines) else ""
        lines.append(f"  │{_pad(left, left_inner)}│{_pad(right, right_inner)}│")
    lines.append(f"  └{'─' * left_inner}┴{'─' * right_inner}┘")
    return lines


def _wrapped_preview(text: str, width: int, max_lines: int) -> list[str]:
    clean = " ".join(str(text or "").split())
    if not clean:
        return [""]
    wrapped = textwrap.wrap(clean, width=max(8, width)) or [""]
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = _crop(wrapped[-1] + " ...", width)
    return wrapped


def _fit(lines: list[str], width: int, height: int) -> list[str]:
    fitted = [_crop(line, width) for line in lines[:height]]
    return fitted + [""] * max(0, height - len(fitted))


def _footer(default: str, message: str, width: int) -> str:
    return _crop(f"  {message or default}", width)


def _left_right(left: str, right: str, width: int) -> str:
    available = max(1, width - _display_width(right))
    return _pad(_crop(left, available), available) + right


def _crop(text: str, width: int) -> str:
    text = str(text or "")
    if _display_width(text) <= width:
        return text
    if width <= 3:
        return _crop_cells(text, width)
    return _crop_cells(text, width - 3) + "..."


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _crop_cells(text: str, width: int) -> str:
    cells = 0
    chars: list[str] = []
    for char in text:
        char_width = _char_width(char)
        if cells + char_width > width:
            break
        chars.append(char)
        cells += char_width
    return "".join(chars)


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in str(text or ""))


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _clamp(index: int, length: int) -> int:
    if length <= 0:
        return 0
    return max(0, min(index, length - 1))


def _status_icon(status: str) -> str:
    return {
        "queued": "○",
        "running": "◌",
        "stopping": "◌",
        "paused": "Ⅱ",
        "completed": "✓",
        "done": "✓",
        "failed": "!",
        "error": "!",
        "stopped": "x",
    }.get(status, "?")


def _status_label(status: str) -> str:
    return {
        "queued": "Queued",
        "running": "Running",
        "stopping": "Stopping",
        "paused": "Paused",
        "completed": "Completed",
        "done": "Completed",
        "failed": "Failed",
        "error": "Error",
        "stopped": "Stopped",
    }.get(status, status.title() or "Unknown")


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return str(value)


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"
