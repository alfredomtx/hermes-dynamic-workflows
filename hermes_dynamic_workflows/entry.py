"""Hermes plugin entrypoint for dynamic workflows."""

from __future__ import annotations

import os

from .adapters.hooks import pre_tool_call_handler
from .adapters.autoflow_hook import pre_gateway_dispatch_handler
from .adapters.task_stop import TASK_STOP_SCHEMA, task_stop
from .adapters.workflow import get_dynamic_workflow_schema, workflow
from .adapters.commands import workflows_command


def register(ctx) -> None:
    """Register the workflow tool and commands with Hermes."""
    cwd = os.environ.get("TERMINAL_CWD") or os.getcwd()

    def _workflow_handler(params, **kwargs):
        return workflow(params, plugin_context=ctx, **kwargs)

    ctx.register_tool(
        name="workflow",
        toolset="workflow",
        schema=get_dynamic_workflow_schema(cwd=cwd),
        handler=_workflow_handler,
        description=(
            "Run deterministic Python workflow scripts that orchestrate "
            "multiple Hermes child agents with agent(), parallel(), and pipeline()."
        ),
    )

    def _task_stop_handler(params, **kwargs):
        return task_stop(params, **kwargs)

    ctx.register_tool(
        name="task_stop",
        toolset="workflow",
        schema=TASK_STOP_SCHEMA,
        handler=_task_stop_handler,
        description="Stop a running background task by ID.",
    )
    # Make child_approval_policy authoritative for workflow-child terminal
    # commands even in non-CLI contexts (where Hermes would otherwise
    # auto-approve/orphan). In CLI this defers to the per-thread callback.
    ctx.register_hook("pre_tool_call", pre_tool_call_handler)

    # Autoflow: ultracode-style sticky per-session auto-workflow steering.
    # The toggle (/autoflow on|off) and the steering injection both live in
    # this gateway hook because a plugin slash-command handler is session-blind
    # at dispatch time (see adapters/autoflow_hook.py for the full rationale).
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_handler)

    def _workflows_handler(raw_args: str = "", **_kwargs):
        return workflows_command(raw_args, plugin_context=ctx)

    ctx.register_command(
        name="workflows",
        handler=_workflows_handler,
        description="Show a compact overview of dynamic workflow agents.",
        args_hint="",
    )

    # Register /autoflow purely for autocomplete-menu discoverability. The
    # pre_gateway_dispatch hook intercepts and fully handles /autoflow BEFORE
    # gateway command dispatch reaches this handler, so this body is only a
    # fallback hint for surfaces where the hook did not run.
    def _autoflow_handler(raw_args: str = "", **_kwargs):
        return (
            "autoflow is a gateway session mode. Use /autoflow on or "
            "/autoflow off in a chat. If you see this text, the autoflow "
            "gateway hook is not active in this context."
        )

    ctx.register_command(
        name="autoflow",
        handler=_autoflow_handler,
        description="Toggle sticky auto-workflow steering for this session (on|off|status).",
        args_hint="on|off|status",
    )
