"""One-command full-screen workflow monitor."""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from typing import Any

from .model import WorkflowRepository, WorkflowView
from .render import RenderState, render_screen


REFRESH_SECONDS = 0.5


class TuiController:
    def __init__(self, repository: WorkflowRepository | None = None):
        self.repository = repository or WorkflowRepository()
        self.workflows: list[WorkflowView] = []
        self.state = RenderState()
        self.should_exit = False

    def refresh(self) -> None:
        selected_run_id = self.current_run.run_id if self.current_run else ""
        self.workflows = self.repository.load()
        run_index = self.state.run_index
        if selected_run_id:
            for index, workflow in enumerate(self.workflows):
                if workflow.run_id == selected_run_id:
                    run_index = index
                    break
        self.state = replace(self.state, run_index=_clamp(run_index, len(self.workflows)))
        self._normalize_nested_selection()

    @property
    def current_run(self) -> WorkflowView | None:
        if not self.workflows:
            return None
        return self.workflows[_clamp(self.state.run_index, len(self.workflows))]

    def handle_key(self, key: str) -> None:
        if key in {"q", "Q"}:
            self.should_exit = True
            return
        if key == "up":
            self._move(-1)
        elif key == "down":
            self._move(1)
        elif key in {"enter", "right"}:
            self._enter()
        elif key in {"esc", "left", "backspace"}:
            self._back()
        elif key in {"s", "S"}:
            self._save()
        elif key in {"x", "X"}:
            self._control("stop")
        elif key in {"p", "P"}:
            self._control("resume" if self.current_run and self.current_run.status == "paused" else "pause")
        elif key in {"r", "R"}:
            self._control("restart")

    def frame(self, width: int, height: int) -> list[str]:
        workflows = self.workflows
        if self.state.view == "agent" and self.current_run:
            hydrated = self.repository.hydrate_agent_activity(
                self.current_run,
                phase_index=self.state.phase_index,
                agent_index=self.state.agent_index,
            )
            workflows = list(self.workflows)
            workflows[_clamp(self.state.run_index, len(workflows))] = hydrated
        return render_screen(workflows, self.state, width=width, height=height)

    def _move(self, delta: int) -> None:
        if self.state.view == "list":
            self.state = replace(
                self.state,
                run_index=_clamp(self.state.run_index + delta, len(self.workflows)),
                message="",
            )
        elif self.state.view == "workflow":
            count = len(self.current_run.phases) if self.current_run else 0
            self.state = replace(
                self.state,
                phase_index=_clamp(self.state.phase_index + delta, count),
                agent_index=0,
                message="",
            )
        elif self.state.view == "agent":
            self.state = replace(
                self.state,
                agent_index=_clamp(self.state.agent_index + delta, len(self._current_phase_agents())),
                message="",
            )

    def _enter(self) -> None:
        if not self.current_run:
            return
        if self.state.view == "list":
            self.state = replace(
                self.state,
                view="workflow",
                phase_index=self._active_phase_index(),
                agent_index=0,
                message="",
            )
        elif self.state.view == "workflow":
            agents = self._current_phase_agents()
            message = "" if agents else "This phase has no agents yet."
            self.state = replace(self.state, view="agent" if agents else "workflow", agent_index=0, message=message)

    def _back(self) -> None:
        if self.state.view == "agent":
            self.state = replace(self.state, view="workflow", message="")
        elif self.state.view == "workflow":
            self.state = replace(self.state, view="list", message="")
        else:
            self.should_exit = True

    def _save(self) -> None:
        if not self.current_run:
            self.state = replace(self.state, message="No workflow selected.")
            return
        try:
            path = self.repository.save_markdown(self.current_run)
            self.state = replace(self.state, message=f"Saved to {path}")
        except OSError as exc:
            self.state = replace(self.state, message=f"Save failed: {exc}")

    def _control(self, action: str) -> None:
        if not self.current_run:
            self.state = replace(self.state, message="No workflow selected.")
            return
        response = self.repository.request_control(self.current_run, action)
        self.state = replace(self.state, message=str(response.get("message") or "Control request sent."))
        self.refresh()
        new_run_id = str(response.get("newRunId") or "")
        if new_run_id:
            for index, workflow in enumerate(self.workflows):
                if workflow.run_id == new_run_id:
                    self.state = replace(self.state, run_index=index, phase_index=0, agent_index=0)
                    break

    def _current_phase_agents(self):
        workflow = self.current_run
        if not workflow:
            return ()
        if not workflow.phases:
            return workflow.agents
        phase = workflow.phases[_clamp(self.state.phase_index, len(workflow.phases))]
        return phase.agents

    def _active_phase_index(self) -> int:
        workflow = self.current_run
        if not workflow or not workflow.current_phase:
            return 0
        for index, phase in enumerate(workflow.phases):
            if phase.title == workflow.current_phase:
                return index
        return 0

    def _normalize_nested_selection(self) -> None:
        workflow = self.current_run
        phase_count = len(workflow.phases) if workflow else 0
        phase_index = _clamp(self.state.phase_index, phase_count)
        agent_count = len(workflow.phases[phase_index].agents) if workflow and workflow.phases else 0
        self.state = replace(
            self.state,
            phase_index=phase_index,
            agent_index=_clamp(self.state.agent_index, agent_count),
        )


def main() -> int:
    controller = TuiController()
    controller.refresh()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        for line in controller.frame(width=120, height=max(12, len(controller.workflows) * 2 + 8)):
            print(line.rstrip())
        return 0
    try:
        import curses
    except ImportError:
        print("hermes-workflows needs terminal curses support.", file=sys.stderr)
        return 1
    try:
        curses.wrapper(lambda screen: _run_curses(screen, controller, curses))
    except KeyboardInterrupt:
        return 0
    return 0


def _run_curses(screen: Any, controller: TuiController, curses: Any) -> None:
    _configure_curses(screen, curses)
    last_refresh = 0.0
    while not controller.should_exit:
        now = time.monotonic()
        if now - last_refresh >= REFRESH_SECONDS:
            controller.refresh()
            last_refresh = now
        height, width = screen.getmaxyx()
        _draw(screen, controller.frame(width, height), curses)
        key = screen.getch()
        if key != -1:
            controller.handle_key(_key_name(key, curses))


def _configure_curses(screen: Any, curses: Any) -> None:
    screen.keypad(True)
    screen.timeout(100)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)


def _draw(screen: Any, lines: list[str], curses: Any) -> None:
    screen.erase()
    height, width = screen.getmaxyx()
    for row, line in enumerate(lines[:height]):
        attr = 0
        stripped = line.strip()
        if stripped == "Dynamic workflows":
            attr = curses.color_pair(1) | curses.A_BOLD
        elif "✓" in line:
            attr = curses.color_pair(2)
        elif stripped.startswith(">") or "│>" in line:
            attr = curses.A_BOLD
        elif "!" in line:
            attr = curses.color_pair(3)
        try:
            screen.addnstr(row, 0, line, max(0, width - 1), attr)
        except curses.error:
            pass
    screen.refresh()


def _key_name(key: int, curses: Any) -> str:
    mapping = {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right",
        curses.KEY_ENTER: "enter",
        curses.KEY_BACKSPACE: "backspace",
        10: "enter",
        13: "enter",
        27: "esc",
        127: "backspace",
    }
    if key in mapping:
        return mapping[key]
    try:
        return chr(key)
    except (ValueError, OverflowError):
        return ""


def _clamp(index: int, length: int) -> int:
    if length <= 0:
        return 0
    return max(0, min(index, length - 1))


if __name__ == "__main__":
    raise SystemExit(main())
