"""Tool schema and model-facing guidelines."""

from __future__ import annotations

import json
import os
import traceback
from typing import Any

from ..engine.manager import get_run_manager


def workflow(params: dict[str, Any], *, plugin_context: Any = None, **kwargs: Any) -> str:
    try:
        manager = get_run_manager()
        tool_use_id = (
            kwargs.get("tool_use_id")
            or kwargs.get("toolUseId")
            or kwargs.get("tool_call_id")
            or kwargs.get("toolCallId")
        )
        record = manager.start_from_params(
            params or {},
            cwd=os.environ.get("TERMINAL_CWD") or os.getcwd(),
            plugin_context=plugin_context,
            tool_use_id=str(tool_use_id) if tool_use_id else None,
            host_session_id=_host_session_id_from_kwargs(kwargs),
        )
        return _launch_message(record)
    except Exception as exc:
        return json.dumps(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "trace": _short_traceback(),
            },
            ensure_ascii=False,
        )


def _launch_message(record: dict[str, Any]) -> str:
    run_id = record.get("runId") or ""
    task_id = record.get("taskId") or run_id
    summary = record.get("summary") or "Dynamic workflow"
    transcript_dir = record.get("transcriptDir") or ""
    script_path = record.get("scriptPath") or ""
    return "\n".join(
        [
            f"Workflow launched in background. Task ID: {task_id}",
            f"Summary: {summary}",
            f"Transcript dir: {transcript_dir}",
            f"Script file: {script_path}",
            f"Run ID: {run_id}",
            (
                "To resume after editing the script: "
                f"Workflow({{scriptPath: {json.dumps(script_path, ensure_ascii=False)}, "
                f"resumeFromRunId: {json.dumps(run_id)}}})"
            ),
            "You will be notified when it completes. Use /workflows to watch live progress.",
        ]
    )


def _short_traceback() -> str:
    lines = traceback.format_exc(limit=4).strip().splitlines()
    return "\n".join(lines[-8:])


def _host_session_id_from_kwargs(kwargs: dict[str, Any]) -> str | None:
    for key in (
        "session_id",
        "sessionId",
        "current_session_id",
        "currentSessionId",
        "task_id",
        "taskId",
    ):
        value = kwargs.get(key)
        if value:
            return str(value)
    return None


_DESCRIPTION = """Execute a Python workflow script that orchestrates multiple Hermes child agents deterministically. The tool starts a background run and returns immediately with a runId and scriptPath — it is asynchronous. When the run finishes, a <task-notification> carrying the status and (truncated) result is delivered back into the conversation, so you can report the outcome without polling; you can also use /workflows to list runs, /workflows <runId> to inspect progress/results, and /workflow-stop <runId> to stop a run.

A workflow structures work across many agents: to be comprehensive (decompose and cover in parallel), to be confident (independent perspectives and adversarial checks before committing), or to take on scale one context cannot hold (audits, broad sweeps, large reviews). The script encodes what fans out, what verifies, and what synthesizes.

ONLY call this tool when the user explicitly opted into multi-agent orchestration. Explicit opt-in means the user directly asked to run a workflow, use multi-agent orchestration, fan out agents, orchestrate with subagents, or invoked a skill/slash command whose instructions call for a workflow. For ordinary tasks that would merely benefit from parallelism, do not call this tool; use ordinary tools/subagents or ask whether the user wants a workflow. By default (require_launch_approval) a launch is gated: the user is asked to approve before the run starts, so this tool may return a "not launched" message if they decline or no approval channel is available — report that to the user and do not retry.

When calling this tool, the right move is often hybrid: scout inline first (list files, inspect the diff, discover the work-list), then call workflow to pipeline over the discovered items. You do not need to know the whole shape before the task, only before the orchestration step.

Pass the script inline via script, or pass scriptPath to rerun a saved script, or pass name to run a predefined workflow from .hermes/workflows/<name>.py or ~/.hermes/dynamic-workflows/workflows/<name>.py. Every inline invocation automatically persists the script under ~/.hermes/projects/<sanitized-cwd>/<sessionId>/workflows/scripts/<meta-name>-<runId>.py and returns that path. A later scriptPath invocation reuses that same file instead of creating a new one. To iterate, edit that file and call workflow with scriptPath instead of resending the full script. The tool result also reports a Transcript dir under ~/.hermes/projects/<sanitized-cwd>/<sessionId>/subagents/workflows/<runId> (written when the workflow completes).

Every Python workflow script should define a literal meta dict near the top:

  meta = {
      "name": "review-changes",
      "description": "Review changed files across dimensions and verify findings",
      "phases": [
          {"title": "Review", "detail": "parallel review dimensions"},
          {"title": "Verify", "detail": "adversarially verify findings"},
          {"title": "Synthesize"},
      ],
  }

Then define an entrypoint:

  def workflow():
      phase("Review")
      results = pipeline(...)
      phase("Synthesize")
      return agent("Synthesize these results: " + json.dumps(results), {"label": "synthesis"})

The meta dict must be a PURE LITERAL. No variables, function calls, spreads, f-strings, or computed values. Required: name. Recommended: description and phases. meta["phases"] may contain strings or {"title", "detail", "model"} objects. Use the same phase titles in phase() calls; titles match exactly.

Available Python globals:

- agent(prompt: str, opts?: dict) -> any: spawn a standalone Hermes AIAgent child. This plugin does not call Hermes' native delegation tool. Without schema, returns final text. With opts["schema"] containing a JSON Schema, the child submits its final answer through a dedicated workflow_submit_structured_output tool that is validated at the tool layer; on a schema mismatch the child receives the error and retries, so agent() returns the parsed/validated object (or None if the child never produced valid output). If the child cannot use the tool, it falls back to parsing the final message. opts.label overrides the display label. opts.phase explicitly assigns the agent to a progress group; use this inside parallel()/pipeline() stages to avoid races on global phase() state. opts.toolsets chooses child toolsets; default is configured by the plugin, normally web/file/terminal. Use ["all"] only when the workflow truly needs broad tool access; blocked child toolsets still stay disabled. Hermes ToolSearch/MCP tools are exposed through Hermes' normal tool-search bridge when available. opts.agentType loads a Hermes skill or workflow agent-type preset for the child. opts.isolation may be "worktree" to run the child in a per-agent git worktree. opts.model routes this agent to a specific model (like Claude Code's per-agent model option) — default to omitting it so the agent inherits the session model, and set it only when a stage clearly wants a cheaper/stronger tier. opts.provider (switching provider entirely) stays disabled unless the plugin config opts in. opts.timeout_seconds overrides child timeout for this call. opts.retries (integer 0-5, default 0) retries this agent up to N more times if the whole child fails, on top of Hermes' native per-API-call retry/fallback; timeouts are not retried, and each attempt's tokens count toward the budget. A run whose every agent errors is reported with status "failed".
- pipeline(items, stage1, stage2, ...) -> list: run each item through all stages independently. There is NO barrier between stages: item A can be in stage 3 while item B is still in stage 1. This is the DEFAULT for multi-stage work. Each stage receives (prev_result, original_item, index). A stage that fails drops that item to None.
- parallel(thunks: list[callable]) -> list: run callables concurrently and wait for all results. This is a BARRIER. Use only when you genuinely need all results together. Pass callables, not direct agent calls: parallel([lambda: agent("A"), lambda: agent("B")]).
- phase(title: str) -> None: start a progress phase. Subsequent agent() calls are grouped under this title unless opts.phase is set.
- log(message) -> None: append a workflow-level progress log.
- args: any: the JSON value passed as this tool's args input, verbatim. Pass arrays/objects as actual JSON values, NOT as JSON-encoded strings. Use args for target files, research questions, or config values.
- budget: object: exposes Claude-style token budget fields: total, spent(), and remaining(). Set total per run via the token_budget tool input (preferred, dynamic like Claude Code's per-turn target), or a literal meta["token_budget"], or plugin config/HERMES_DYNAMIC_WORKFLOWS_TOKEN_BUDGET; precedence is token_budget input > meta > config. total is None when none is set. spent() counts completed child-agent tokens (input+output+reasoning) in this workflow run; remaining() returns Infinity when total is None. Once spent reaches total, further agent() calls fail (a hard ceiling). Use for loop-until-budget: while budget.total and budget.remaining() > 50000: ...
- subworkflow(name_or_ref, args=None) -> any: run another workflow synchronously as a sub-step. Pass a workflow name or {"scriptPath": "..."}. Child workflows share the parent run's agent counter, stop signal, deadline, token budget, resume cache, and global concurrency slots. Nesting is limited to one level.
- cwd: str, json, math: current working directory string and safe standard helpers.

Scripts are Python, not JavaScript and not TypeScript. Use lambda callables for parallel thunks. Normal control flow is allowed — if/for/while plus try/except — so loop-until-budget and loop-until-dry work as written. Catch a specific type or Exception (e.g. except Exception:), never a bare except: or except BaseException:: run-level halts (user stop, the workflow deadline, and the token/agent/loop-iteration limits) are not catchable and will stop the run no matter what you wrap in try. Do not import modules; json and math are already provided. Do not read files directly, shell out, call open/eval/exec/compile/input, or access private/dunder attributes. Child agents should use Hermes tools for repository access.

DEFAULT TO pipeline(). Only use a barrier with parallel() when stage N needs cross-item context from all of stage N-1: dedup/merge across the full result set, early exit when total count is zero, or prompts comparing all prior findings. A barrier is not justified by conceptual stage boundaries or cleaner code.

Canonical review pattern:

  meta = {"name": "review-changes", "description": "Review and verify", "phases": [{"title": "Review"}, {"title": "Verify"}, {"title": "Synthesize"}]}
  FINDINGS_SCHEMA = {"type": "object", "required": ["findings"]}
  VERDICT_SCHEMA = {"type": "object", "required": ["isReal", "reason"]}
  DIMENSIONS = [
      {"key": "bugs", "prompt": "..."},
      {"key": "security", "prompt": "..."},
  ]
  def workflow():
      results = pipeline(
          DIMENSIONS,
          lambda d, original, i: agent(d["prompt"], {"label": "review:" + d["key"], "phase": "Review", "schema": FINDINGS_SCHEMA}),
          lambda review, original, i: parallel([
              lambda f=f: agent("Adversarially verify: " + json.dumps(f), {"label": "verify", "phase": "Verify", "schema": VERDICT_SCHEMA})
              for f in (review or {}).get("findings", [])
          ]),
      )
      phase("Synthesize")
      return agent("Synthesize confirmed findings: " + json.dumps(results), {"label": "synthesis"})

Quality patterns:
- Adversarial verify: spawn independent skeptics per finding, each prompted to refute.
- Perspective-diverse verify: assign correctness/security/performance/repro lenses.
- Judge panel: generate independent approaches, score with parallel judges, synthesize from the winner.
- Loop-until-dry: keep spawning finders until consecutive rounds return nothing new, bounded by budget.total and budget.remaining() when a token budget is configured.
- Completeness critic: final agent asks what is missing before synthesis.

Concurrent agent() calls share one workflow-wide cap. By default the cap is min(16, cpu cores - 2), with plugin config/env overrides available. Excess agent() calls queue until a slot frees. Total agent count across the workflow run is capped at 1000 by default as a runaway-loop backstop.

Resume: pass resumeFromRunId to reuse cached agent() results from the unchanged prefix of a previous run. Same script + same args should produce cache hits until the first changed agent prompt/options. Nested workflow agent calls participate in the same global cache sequence.

Save for reuse: after a run does what the user wanted, the user can save its script as a reusable named workflow with /workflows <runId> save <name> [user|project]. That writes the script to .hermes/workflows/<name>.py (project) or the user store and registers a /<name> slash command, so the same orchestration reruns via /<name> or the workflow tool's name input. /workflows <runId> export [path] writes a markdown transcript of a run instead.

Current Hermes plugin behavior: child agents are created directly as standalone Hermes AIAgent instances. They do not use Hermes' native delegation path and do not appear in Hermes' native /agents delegation tree. agentType first tries Hermes' own skill loader, then falls back to workflow agent-type files from .hermes/workflow-agent-types, ~/.hermes/dynamic-workflows/agent-types, or bundled agent-types. Worktree isolation creates a per-child git worktree and binds the child's Hermes file/terminal tools to that workspace with a task-specific cwd override.
"""

DYNAMIC_WORKFLOW_SCHEMA = {
    "description": _DESCRIPTION,
    "parameters": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "script": {
                "type": "string",
                "maxLength": 524288,
                "description": (
                    "Self-contained Python workflow script. Define a literal meta dict "
                    "and a def workflow(): entrypoint using agent()/parallel()/pipeline()/phase()."
                ),
            },
            "scriptPath": {
                "type": "string",
                "description": (
                    "Path to a workflow Python script on disk. Takes precedence over script and name. "
                    "Use this to rerun or iterate on a script saved by an earlier invocation; "
                    "the same file is reused rather than copied to a new run-specific path."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Name of a predefined workflow. Resolves from .hermes/workflows/<name>.py, "
                    "~/.hermes/dynamic-workflows/workflows/<name>.py, or bundled workflows."
                ),
            },
            "args": {
                "description": (
                    "Optional JSON value exposed to the script as global args, verbatim. "
                    "Pass arrays/objects directly, not as JSON-encoded strings."
                ),
            },
            "token_budget": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Optional hard token ceiling for this run, exposed to the script as "
                    "budget.total. Once budget.spent() reaches it, further agent() calls "
                    "fail. Overrides meta['token_budget'] and plugin config."
                ),
            },
            "resumeFromRunId": {
                "type": "string",
                "pattern": "^wf_[a-z0-9-]{6,}$",
                "description": (
                    "Run ID of a previous workflow invocation. Unchanged-prefix "
                    "agent() calls return cached results; edited/new calls run live."
                ),
            },
            "description": {
                "type": "string",
                "description": "Ignored. Set workflow description in the script's meta dict.",
            },
            "title": {
                "type": "string",
                "description": "Ignored. Set workflow name/title in the script's meta dict.",
            },
        },
    },
}
