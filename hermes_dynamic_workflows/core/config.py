"""Configuration helpers for the dynamic workflow plugin."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def default_concurrency() -> int:
    return min(16, max(1, (os.cpu_count() or 4) - 2))


@dataclass(frozen=True)
class PluginConfig:
    concurrency: int = field(default_factory=default_concurrency)
    max_concurrency: int = 16
    max_agents: int = 1000
    # Maximum workflow() nesting depth. depth=0 is the top-level run; each
    # nested workflow() call goes one deeper. A value of N allows the root plus
    # N nested levels (N+1 total), so the default 2 permits root -> child ->
    # grandchild. Setting 1 reproduces the original single-nested-level limit.
    # Run-wide caps (max_agents, the concurrency semaphore, the token budget,
    # the deadline) are shared across every frame regardless of depth, so a
    # deeper tree cannot exceed those ceilings — only the call tree gets taller.
    max_nesting_depth: int = 2
    # Runaway-loop backstop: caps total `while`-loop iterations across the run.
    # Generous so legitimate loop-until-budget/dry never trips it; the wall-clock
    # deadline is the primary bound (now enforced inside loops via tick_loop).
    max_loop_iterations: int = 10_000_000
    workflow_timeout_seconds: float = 900.0
    child_timeout_seconds: float = 300.0
    script_max_chars: int = 524288
    mcp_discovery_wait_seconds: float = 0.75
    default_child_toolsets: tuple[str, ...] = ("web", "file", "terminal", "skills")
    blocked_child_toolsets: tuple[str, ...] = (
        "workflow",
        "workflows",
        "delegation",
        "code_execution",
        "memory",
        "messaging",
        "clarify",
    )
    # Per-agent model override (agent(model=...)) is allowed by default,
    # matching per-agent / per-stage model routing; the default is still the
    # session model, override only when a stage wants a different tier. Provider
    # selection stays in Hermes' runtime/model configuration.
    allow_model_override: bool = True
    keep_worktrees: bool = False
    # Ask the user to approve before a top-level workflow launches (CC gates
    # every launch — a run can spawn many agents and spend real tokens). On by
    # default: CLI prompts synchronously, gateway sends approve/deny buttons,
    # and a headless/unattended context (no channel) is denied — set this False
    # (or HERMES_DYNAMIC_WORKFLOWS_REQUIRE_LAUNCH_APPROVAL=0) for automation.
    # Only top-level launches are gated; nested workflow() calls inherit the parent run.
    require_launch_approval: bool = True
    # What a child agent does when Hermes' approval engine flags a command and
    # no human is present to approve it. The engine itself (hardline blocks,
    # permanent allowlist, yolo, smart mode) still runs upstream regardless;
    # this only decides the otherwise-would-prompt case. When the policy allows
    # a flagged command, the hook also approve_session()s its pattern so the
    # decision sticks past Hermes' own context re-gating (which would otherwise
    # turn a detached gateway child's command into an unanswerable "pending").
    #   inherit -> follow Hermes' own approvals.mode (manual->ask, smart->smart,
    #              off->approve); single source of truth (default)
    #   smart   -> Hermes' _smart_approve auxiliary-LLM guardian (recommended for
    #              unattended/gateway: lets benign-but-flagged commands run, blocks
    #              the genuinely dangerous; only flagged commands hit the LLM)
    #   deny    -> refuse flagged commands
    #   approve -> allow flagged commands (hardline still blocked upstream)
    #   ask     -> route to the user if a live approval channel exists (CLI
    #              approval UI or gateway buttons); otherwise degrade to
    #              ask_fallback
    child_approval_policy: str = "inherit"
    # What `ask` falls back to when no human is reachable (the common case for a
    # detached workflow child). smart | deny | approve.
    ask_fallback: str = "smart"
    # On run completion, notify the parent session so the user does not need to
    # poll /workflows. A <task-notification> is injected into the conversation
    # through ctx.inject_message; gateway sends a concise completion message to
    # the originating chat. Result previews are truncated to
    # notify_result_preview_chars to protect context/chat length.
    notify_on_complete: bool = True
    # On launch, send a concise "workflow started" message to the origin
    # gateway chat so the user sees an auto-fired (or approved) run begin and
    # can track timing. CLI is unaffected (the launch tool result already
    # surfaces there). Best-effort; never blocks or fails the launch. Pairs
    # with notify_on_complete to bracket each run with start+end markers —
    # useful when autoflow auto-launches workflows with approval off.
    notify_on_launch: bool = True
    notify_result_preview_chars: int = 2000
    # Live, edited-in-place progress bubble in gateway chats. When on (default)
    # and the run originates from a gateway session, the plugin posts ONE
    # message at launch and EDITS it in place as phases/agents progress, then
    # finalizes it with the result on completion — so the user sees readable
    # live progress without running /workflows. Edits (not new sends) are used
    # for the mid-run updates, so this does not trip Telegram per-chat send
    # flood limits. When on and active, it replaces the separate launch marker
    # and gateway completion text (one evolving bubble instead of three
    # messages); the in-conversation <task-notification> injection is
    # unaffected. Falls back to the launch+completion markers if the bubble
    # cannot be seeded (no gateway context, adapter without edit support, or a
    # send failure). CLI sessions are unaffected (no bubble).
    notify_progress: bool = True
    # Minimum seconds between in-place edits of the progress bubble. A floor,
    # not a tick: edits only fire on a meaningful change (phase/agent count) and
    # are skipped when the rendered text is unchanged. The launch seed and the
    # final completion edit bypass this throttle.
    notify_progress_min_interval_seconds: float = 6.0
    # Render a tappable "⏹ Stop" inline button on the live gateway progress
    # bubble (Telegram) while the run is stoppable (queued/running/paused).
    # Tapping it routes through the core `gateway_callback` hook to stop_task.
    # Requires a Hermes core that supports the generic `buttons=` send/edit
    # kwarg; on older cores the button is silently omitted (no error). The
    # button is cleared automatically when the run reaches a terminal state.
    notify_progress_stop_button: bool = True
    # --- Autoflow (ultracode-style auto-workflow steering) ---------------
    # A per-session mode toggled with `/autoflow on|off` in the gateway. While
    # ON for a session, each substantive inbound message (a) bumps reasoning
    # effort to auto_workflow_effort and (b) gets a steering directive appended
    # that nudges the model to prefer the `workflow` tool. It is a NUDGE, not a
    # hard force (matching Claude Code's ultracode "Claude decides" model), it
    # is gateway-only, and launch approval still applies. Default per-session
    # state is OFF; these keys only set the behavior once a session opts in.
    #
    # auto_workflow_default_on flips that baseline: when true, EVERY gateway
    # session starts ON (substantive messages steered + effort-bumped) unless
    # that session explicitly runs `/autoflow off`. Shipped default is false —
    # enable in config.yaml only for benchmarking / always-orchestrate setups,
    # since it raises cost across every connected chat. Launch approval still
    # applies independently (require_launch_approval).
    auto_workflow_default_on: bool = False
    auto_workflow_effort: str = "xhigh"
    # Minimum stripped-text length for a message to count as "substantive" and
    # be steered/effort-bumped. Trivial replies ("ok", "thanks") fall through
    # untouched so they don't pay xhigh latency. Cheap prefilter, no LLM call.
    auto_workflow_min_chars: int = 24
    # --- Orphan reaping + auto-resume ------------------------------------
    # A workflow run executes inside the Hermes process that launched it (the
    # gateway daemon or a CLI). If that process exits — a `hermes gateway
    # restart` is the common case — the run thread is killed mid-flight and its
    # record is frozen at whatever active status it held ("running"), with no
    # chance to write a terminal state. The record then lies "running" forever.
    # reap_orphans() detects such runs on the next manager boot (their
    # controlOwner PID is dead, or they are stale past the grace window while
    # not paused) and marks them "interrupted", first harvesting completed
    # child-agent results from the run's journal into agentCache so any later
    # resume reuses them instead of re-running. Grace backstops PID recycling.
    orphan_grace_seconds: float = 900.0
    # When true, the manager also relaunches freshly-reaped orphans on boot
    # (resumeFromRunId, reusing the harvested cache) so an interrupted run
    # finishes without manual intervention. Shipped default is FALSE: resuming
    # spends tokens and a restart may have been intentional. Enable in
    # config.yaml only for unattended / benchmark setups. Only freshly-orphaned,
    # recent (auto_resume_window_seconds), in-cap (auto_resume_max) runs are
    # revived, and only when a gateway loop is present to route completion.
    auto_resume_on_boot: bool = False
    # Max orphans auto-resumed per boot — bounds a resurrection storm when many
    # runs were orphaned at once (e.g. a crash with several in flight).
    auto_resume_max: int = 3
    # Only auto-resume orphans whose last journal activity was within this
    # window. Older abandoned runs stay interrupted (no week-late resurrection).
    auto_resume_window_seconds: float = 21600.0


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _as_float(value: Any, default: float, *, minimum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value]
    else:
        return default
    cleaned = tuple(item for item in items if item)
    return cleaned or default


def _as_mode(value: Any, default: str, allowed: set[str]) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in allowed else default


def load_config() -> PluginConfig:
    """Load plugin config from Hermes config.yaml and environment variables."""
    raw: dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config as _load_hermes_config

        hermes_cfg = _load_hermes_config() or {}
        entries = ((hermes_cfg.get("plugins") or {}).get("entries") or {})
        entry = entries.get("dynamic-workflows") or entries.get("dynamic_workflows") or {}
        if isinstance(entry, dict):
            raw = entry.get("dynamic_workflows") or entry.get("config") or entry
            if not isinstance(raw, dict):
                raw = {}
    except Exception:
        raw = {}

    default = PluginConfig()
    concurrency = _as_int(
        os.getenv("HERMES_DYNAMIC_WORKFLOWS_CONCURRENCY", raw.get("concurrency")),
        default.concurrency,
        minimum=1,
        maximum=32,
    )
    max_concurrency = _as_int(raw.get("max_concurrency"), default.max_concurrency, minimum=1, maximum=32)
    max_concurrency = _as_int(
        os.getenv("HERMES_DYNAMIC_WORKFLOWS_MAX_CONCURRENCY"),
        max_concurrency,
        minimum=1,
        maximum=32,
    )

    return PluginConfig(
        concurrency=min(concurrency, max_concurrency),
        max_concurrency=max_concurrency,
        max_agents=_as_int(raw.get("max_agents"), default.max_agents, minimum=1, maximum=1000),
        max_nesting_depth=_as_int(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_MAX_NESTING_DEPTH",
                raw.get("max_nesting_depth"),
            ),
            default.max_nesting_depth,
            minimum=1,
            maximum=8,
        ),
        max_loop_iterations=_as_int(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_MAX_LOOP_ITERATIONS",
                raw.get("max_loop_iterations"),
            ),
            default.max_loop_iterations,
            minimum=1000,
        ),
        workflow_timeout_seconds=_as_float(
            raw.get("workflow_timeout_seconds"),
            default.workflow_timeout_seconds,
        ),
        child_timeout_seconds=_as_float(
            raw.get("child_timeout_seconds"),
            default.child_timeout_seconds,
        ),
        script_max_chars=_as_int(
            raw.get("script_max_chars"),
            default.script_max_chars,
            minimum=1000,
            maximum=1048576,
        ),
        mcp_discovery_wait_seconds=_as_float(
            raw.get("mcp_discovery_wait_seconds"),
            default.mcp_discovery_wait_seconds,
            minimum=0.0,
        ),
        default_child_toolsets=_as_str_tuple(
            raw.get("default_child_toolsets"),
            default.default_child_toolsets,
        ),
        blocked_child_toolsets=_as_str_tuple(
            raw.get("blocked_child_toolsets"),
            default.blocked_child_toolsets,
        ),
        allow_model_override=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_ALLOW_MODEL_OVERRIDE", raw.get("allow_model_override")),
            default.allow_model_override,
        ),
        keep_worktrees=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_KEEP_WORKTREES", raw.get("keep_worktrees")),
            default.keep_worktrees,
        ),
        require_launch_approval=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_REQUIRE_LAUNCH_APPROVAL", raw.get("require_launch_approval")),
            default.require_launch_approval,
        ),
        child_approval_policy=_as_mode(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_CHILD_APPROVAL_POLICY",
                raw.get("child_approval_policy"),
            ),
            default.child_approval_policy,
            {"deny", "smart", "approve", "ask", "inherit"},
        ),
        ask_fallback=_as_mode(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_ASK_FALLBACK", raw.get("ask_fallback")),
            default.ask_fallback,
            {"smart", "deny", "approve"},
        ),
        notify_on_complete=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_NOTIFY_ON_COMPLETE", raw.get("notify_on_complete")),
            default.notify_on_complete,
        ),
        notify_on_launch=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_NOTIFY_ON_LAUNCH", raw.get("notify_on_launch")),
            default.notify_on_launch,
        ),
        notify_progress=_as_bool(
            os.getenv("HERMES_DYNAMIC_WORKFLOWS_NOTIFY_PROGRESS", raw.get("notify_progress")),
            default.notify_progress,
        ),
        notify_progress_min_interval_seconds=_as_float(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_NOTIFY_PROGRESS_MIN_INTERVAL_SECONDS",
                raw.get("notify_progress_min_interval_seconds"),
            ),
            default.notify_progress_min_interval_seconds,
            minimum=0.0,
        ),
        notify_progress_stop_button=_as_bool(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_NOTIFY_PROGRESS_STOP_BUTTON",
                raw.get("notify_progress_stop_button"),
            ),
            default.notify_progress_stop_button,
        ),
        notify_result_preview_chars=_as_int(
            raw.get("notify_result_preview_chars"),
            default.notify_result_preview_chars,
            minimum=0,
            maximum=20000,
        ),
        auto_workflow_default_on=_as_bool(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_AUTO_WORKFLOW_DEFAULT_ON",
                raw.get("auto_workflow_default_on"),
            ),
            default.auto_workflow_default_on,
        ),
        auto_workflow_effort=_as_mode(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_AUTO_WORKFLOW_EFFORT",
                raw.get("auto_workflow_effort"),
            ),
            default.auto_workflow_effort,
            {"minimal", "low", "medium", "high", "xhigh", "max"},
        ),
        auto_workflow_min_chars=_as_int(
            raw.get("auto_workflow_min_chars"),
            default.auto_workflow_min_chars,
            minimum=1,
            maximum=10000,
        ),
        orphan_grace_seconds=_as_float(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_ORPHAN_GRACE_SECONDS",
                raw.get("orphan_grace_seconds"),
            ),
            default.orphan_grace_seconds,
            minimum=0.0,
        ),
        auto_resume_on_boot=_as_bool(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_AUTO_RESUME_ON_BOOT",
                raw.get("auto_resume_on_boot"),
            ),
            default.auto_resume_on_boot,
        ),
        auto_resume_max=_as_int(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_AUTO_RESUME_MAX",
                raw.get("auto_resume_max"),
            ),
            default.auto_resume_max,
            minimum=1,
            maximum=100,
        ),
        auto_resume_window_seconds=_as_float(
            os.getenv(
                "HERMES_DYNAMIC_WORKFLOWS_AUTO_RESUME_WINDOW_SECONDS",
                raw.get("auto_resume_window_seconds"),
            ),
            default.auto_resume_window_seconds,
            minimum=0.0,
        ),
    )
