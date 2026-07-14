# Hermes Dynamic Workflows

> **Claude-Code-style dynamic workflows for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**

English | [简体中文](./README.zh-CN.md) | [日本語](./README.ja-JP.md)

You can now use **Dynamic Workflows** in Hermes: have the model write a sandboxed Python
script on the fly, execute it in the background runtime, and orchestrate large numbers
of independent subagents with `agent()/parallel()/pipeline()` — ideal for codebase
audits, large-scale migrations, and cross-validated research. Inspired by
[Dynamic Workflows in Claude Code](https://claude.com/blog/introducing-dynamic-workflows-in-claude-code).

https://github.com/user-attachments/assets/06ef3d0d-4d89-48c4-9851-e1cae690e9b0

## Quick Start

Install and enable in one line:

```bash
hermes plugins install lingjiuu/hermes-dynamic-workflows --enable
```

> Gateway users: run `hermes gateway restart` after installing.

Once it's installed, just tell Hermes "run a workflow that …" and you're set.

### Live Dashboard (optional, requires a separate step)

`hermes plugins install` only clones the plugin — it does not install its console
scripts, so the dashboard command has to be installed once separately:

```bash
python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/dynamic-workflows/scripts/install-hermes-workflows.py"
# Installs to ~/.local/bin
```

Then, in **a separate terminal**, run `hermes-workflows` to open the interactive
dashboard, where you can watch the run list, per-phase/per-agent progress, and each
subagent's prompt and output in real time.

## Configuration (optional)

The plugin reads the following section from Hermes's `~/.hermes/config.yaml` (every key
can also be overridden via a `HERMES_DYNAMIC_WORKFLOWS_*` environment variable):

```yaml
plugins:
  entries:
    dynamic-workflows:
      dynamic_workflows:
        concurrency: 8                # Max concurrent agents (default: min(16, cpu-2))
        max_concurrency: 16           # Hard cap on concurrency
        max_agents: 1000              # Max total agents per run (runaway guard)
        max_nesting_depth: 2          # Max workflow() nesting depth (root + N levels); run-wide caps still bind across all levels
        workflow_timeout_seconds: 900 # Wall-clock timeout for the whole run (excludes paused time)
        child_timeout_seconds: 300    # Timeout for a single child agent
        blocked_child_toolsets: [workflow, delegation, code_execution, memory, messaging, clarify]
                                      # Toolsets child agents are forbidden to use
        default_child_toolsets: [web, file, terminal, skills]
                                      # Default toolsets for child agents (used when no agentType is given)
        keep_worktrees: false         # Whether to keep each agent's git worktree (auto-cleaned by default)
        allow_model_override: true    # Whether agent(model=...) may override the model
        missing_agent_type_policy: error # error|fallback_warn for explicit missing agentType
        require_launch_approval: true # Require confirmation before a top-level workflow launches (denied if nobody is online)
        child_approval_policy: inherit # Child agent approval policy: inherit|smart|deny|approve|ask
        ask_fallback: smart           # Fallback when "ask" has no one to reach: smart|deny|approve
        notify_on_complete: true      # Notify the originating CLI or gateway session on completion
        notify_on_launch: true        # Send a "workflow started" marker to the origin gateway chat at launch
        notify_result_preview_chars: 2000  # Truncation length (chars) for the result preview in notifications
        notify_progress_stop_button: true  # Show a tappable ⏹ Stop button on the live progress bubble (Telegram; needs a core that supports inline buttons)
        auto_workflow_default_on: false # When true, every session starts ON unless it runs /autoflow off (raises cost across all chats)
        auto_workflow_min_chars: 24    # Min message length to count as "substantive" (cheap prefilter, no LLM call)
        orphan_grace_seconds: 900      # Idle window before a run with no dead-PID signal is reaped as stale (backstops PID recycling)
        auto_resume_on_boot: false     # When true, relaunch freshly-reaped orphans on boot (resumes from cache); shipped off
        auto_resume_max: 3             # Max orphans auto-resumed per boot (bounds a resurrection storm)
        auto_resume_window_seconds: 21600 # Only auto-resume orphans whose last activity was within this window (6h)
```

## Autoflow (ultracode-style auto-workflow steering)

`/autoflow on` turns on a **sticky per-session mode** in the gateway
(Telegram/Discord/etc.). While it's on, every *substantive* message you send
that session is automatically steered toward the `workflow` tool — no need to
type "use a workflow" each time. This is Hermes' analogue of Claude Code's
`ultracode`.

```text
/autoflow on       # enable for this session (sticky until you turn it off)
/autoflow off      # back to normal turn-by-turn handling
/autoflow          # report current state
```

Set `auto_workflow_default_on: true` to make **every** gateway session start ON
without anyone typing `/autoflow on` — each session stays ON until it runs
`/autoflow off` (an explicit off is sticky and beats the default). Shipped
default is false; turning it on raises cost across every connected chat, so
it's intended for benchmarking / always-orchestrate setups.

While on, each substantive inbound message gets a steering directive appended
that tells the model the task is pre-authorized for orchestration, so it prefers
the `workflow` tool. Autoflow does not change the parent session's reasoning effort.

It is a **nudge, not a hard force** (the model still decides, matching
ultracode), it is **gateway-only** (CLI/TUI unaffected), and **launch approval
still applies** — `require_launch_approval` gates every workflow launch
regardless. Trivial messages (short acks, slash commands) pass through untouched.

When a workflow launches, `notify_on_launch` (default on) posts a concise
"🚀 Workflow started" marker to the origin chat, and `notify_on_complete` posts
the result at the end — so each run is bracketed with start/end markers in the
chat, with timing visible. Useful when autoflow auto-launches runs with
approval off and you want to keep an eye on what fired and how long it took.

## Crash recovery (orphan reaping + auto-resume)

A run executes inside the Hermes process that launched it (the gateway daemon
or a CLI). If that process exits while a run is in flight — a `hermes gateway
restart` is the usual cause — the run thread dies with it and never gets to
write a terminal status, so its record is frozen at `running` forever and
`/workflows` keeps showing it as live.

On the next manager boot the plugin **reaps** such orphans: any run still in an
active state whose owning process is gone is flipped to a new terminal status,
`interrupted`. "Gone" is detected two ways — the run's owner PID is no longer
alive (primary signal; a restart kills the old PID exactly this way), or the
run has been idle past `orphan_grace_seconds` (a backstop for PID recycling and
for records with no parseable owner). A run still owned by a live process —
another gateway, or a standalone `hermes-workflows` TUI — is never touched.

Before marking a run `interrupted`, the reaper **harvests** every completed
child-agent result from the run's journal back into its resume cache. Each
agent writes its result to the journal as it finishes, keyed by the same
fingerprint the resume cache uses, so a crash loses nothing that already
completed — those results just need to be picked back up. This makes any later
resume cheap: the finished agents are reused, only the unfinished ones re-run.

`auto_resume_on_boot` (shipped **off**) takes the next step: when on, the
manager relaunches the runs it just reaped, resuming from the harvested cache
so completed agents are skipped. It's bounded — at most `auto_resume_max` per
boot, only runs whose last activity was within `auto_resume_window_seconds`,
only when their script is still on disk, and only when a gateway loop is
present to route the completion message back to the originating chat (the
run's routing context — platform/chat/thread, never credentials — is persisted
on the record for exactly this). Leave it off for normal use (a restart is
often intentional and resuming spends tokens); turn it on for unattended /
benchmark setups where runs should always finish.

## Script API

A workflow script is just a piece of async Python whose first statement is a literal
`meta`; after that you orchestrate child agents using the sandboxed globals:

```python
meta = {
    "name": "repo-audit",
    "description": "Parallel review, then adversarial verify",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

# Each target flows through review → verify independently
# (pipeline has no barrier: A can be at verify while B is still at review)
findings = await pipeline(
    args["targets"],
    lambda t, _o, i: agent(f"Review for bugs: {t}", {"label": f"review:{i}", "phase": "Review"}),
    lambda r, _o, i: agent(f"Verify adversarially: {json.dumps(r)}", {"label": f"verify:{i}", "phase": "Verify"}),
)
return await agent("Synthesize the verified findings:\n" + json.dumps(findings))
```

- `agent(prompt, opts)` spawns a child agent; `opts` may include `schema` (enforce
  structured output), `model`, `agentType`, `isolation="worktree"`, inline
  `instructions`/`systemPrompt`, `reasoningEffort`, `toolsets`, `allowedTools`, and
  `disallowedTools`. Every child must resolve reasoning from inline
  `reasoningEffort` or its agent-type preset; missing values fail before launch.
  Current Bedrock and `codex_app_server` transports do not forward workflow reasoning
  effort, so those runtimes fail before child launch.
- `pipeline` (default, no barrier) / `parallel` (with barrier) handle concurrency;
  `phase`/`log` report progress; `workflow()` runs a named workflow inline; `args` /
  `budget` access the input arguments and the token budget.

### Agent Type and inline/runtime agents

You can choose a child agent role three ways:

1. **Inline per call** — pass role/tool options directly to `agent()`.
2. **Runtime presets** — define reusable presets in the workflow literal under `meta["agents"]`.
3. **Library presets** — reference `.md` / `.yaml` / `.json` files with `agentType`.

Inline example:

```python
await agent(
    "Review this diff for authorization bugs only.",
    {
        "instructions": "You are a read-only security reviewer. Return blockers only.",
        "reasoningEffort": "high",
        "toolsets": ["file", "terminal"],
        "allowedTools": ["read_file", "search_files", "terminal", "process"],
    },
)
```

Runtime preset example:

```python
meta = {
    "name": "review-matrix",
    "description": "Review and verify changes",
    "agents": {
        "read-only-reviewer": {
            "instructions": "Review for correctness and regression risk. Do not edit files.",
            "reasoningEffort": "high",
            "toolsets": ["file", "terminal"],
            "allowedTools": ["read_file", "search_files", "terminal", "process"],
        },
        "synthesizer": {
            "instructions": "Synthesize findings into a concise verdict.",
            "reasoningEffort": "medium",
            "toolsets": [],
        },
    },
}

findings = await agent("Review diff", {"agentType": "read-only-reviewer"})
return await agent("Synthesize: " + json.dumps(findings), {"agentType": "synthesizer"})
```

Built-in library presets:

| Type | Toolset | Description |
|------|---------|-------------|
| `general-purpose` | `*` (all safe tools) | Default; good for searching code, researching complex problems, and multi-step tasks |
| `explore` | Read-only (read_file, search_files, terminal) | Fast codebase exploration; good for locating files and searching keywords |
| `plan` | Read-only (read_file, search_files, terminal) | Software architecture design; outputs a step-by-step implementation plan |
| `verification` | web + file + terminal + browser | Verifies implementation correctness; runs build/test/lint to emit PASS/FAIL |

Named `agentType` resolution order:

1. `meta["agents"]` runtime presets in the current script
2. `<project>/.hermes/dynamic-workflows/agents/*.md`  — project level
3. `~/.hermes/dynamic-workflows/agents/*.md`          — user level
4. `<plugin>/hermes_dynamic_workflows/agents/*.md`    — built-in defaults

Explicit missing `agentType` errors before launch by default. Set
`missing_agent_type_policy: fallback_warn` to log a warning and fall back to
`general-purpose`.

Tool-surface semantics:

- Omitted `toolsets` inherits the preset/default surface.
- `toolsets: []` is intentional no tools.
- Inline/runtime `toolsets` are exact and are not widened by discoverable MCP/plugin toolsets.
- If both a preset and inline call specify `allowedTools`, the effective allowlist is their intersection.
- `allowedTools: []` denies all normal tools; schema emission still keeps `structured_output` available.
- `disallowedTools` are additive: preset denylist union inline denylist.
- Stable role text belongs in `meta["agents"]`; per-item facts belong in the normal `prompt` so resume-cache and prompt-cache reuse stay strong.

To add a reusable file-backed preset, create a new `.md` file under the project-level or user-level agent directory above:

```markdown
---
name: my-agent
description: "A short description of what this agent is for; the model uses it to automatically pick the right agent."
model: inherit
reasoning_effort: high
toolsets: [web, file, terminal]
---

Write the agent's system prompt here to guide its behavior, style, and constraints.
```

`name` and `description` are required; `model` defaults to `inherit` (inherits the
current session's model); `toolsets` defaults to the global `default_child_toolsets`;
`reasoning_effort` is required. Optional fields also include `allowed_tools`,
`disallowed_tools`, and `isolation`.

At runtime the plugin persists the script and the full execution trace (transcript) of
every child agent, and injects a `<task-notification>` into the conversation on
completion — no polling required. Use `/workflows` to view history and details.

## Deep Dive

For implementation details (core execution path, tools and full call results, prompt
cache, concurrency and limits, permission governance, rebuilding transcripts from
`state.db`, sandboxing, resume…), see [TECHNICAL.md](./TECHNICAL.md).

## License

[MIT](./LICENSE)
