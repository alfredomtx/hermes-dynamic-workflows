"""Workflow globals exposed to generated Python scripts."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .cache import agent_fingerprint, is_cache_miss
from ..core.text import preview
from ..core.errors import (
    ChildAgentError,
    ChildAgentSkipped,
    WorkflowHalt,
    WorkflowParseError,
    WorkflowRuntimeError,
)
from ..core.schema import StructuredOutputError, validate_json_schema
from ..core.types import (
    AgentRecord,
    ChildAgentRequest,
    ChildAgentResult,
    ResolvedAgentSpec,
    WorkflowFrame,
)

MAX_VM_ARRAY_ITEMS = 4096


class BudgetView:
    def __init__(self, context: Any):
        self._context = context

    @property
    def total(self) -> int | None:
        return self._context.token_budget_total

    def spent(self) -> int:
        return self._context.spent_tokens

    def remaining(self) -> float:
        return self._context.remaining_tokens


class WorkflowAPI:
    def __init__(
        self,
        *,
        context: Any,
        frame: WorkflowFrame,
        depth: int = 0,
    ):
        self.context = context
        self.frame = frame
        self.runner = context.runner
        self.config = context.config
        self.resume_cache = context.resume_cache
        self.depth = depth
        self._lock = threading.RLock()
        self.budget = BudgetView(context)

    def globals(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "parallel": self.parallel,
            "pipeline": self.pipeline,
            "phase": self.phase,
            "log": self.log,
            "args": self.frame.args,
            "budget": self.budget,
            "workflow": self.workflow,
        }

    async def agent(self, prompt: str, opts: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._agent_sync, prompt, opts)

    def _agent_sync(self, prompt: str, opts: dict[str, Any] | None = None) -> Any:
        self._check_deadline()
        opts = opts or {}
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowRuntimeError("agent() expects a non-empty prompt string")
        if not isinstance(opts, dict):
            raise WorkflowRuntimeError("agent() options must be a dict")
        _validate_agent_opts(opts)

        schema = opts.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise WorkflowRuntimeError("agent() schema option must be a dict")
        if schema is not None:
            try:
                validate_json_schema(schema)
            except StructuredOutputError as exc:
                raise WorkflowRuntimeError(str(exc)) from exc
        phase_name = str(opts.get("phase") or self.frame.current_phase or "") or None
        resolved = _resolve_agent_spec(
            opts,
            cwd=self.frame.cwd,
            config=self.config,
            structured_output=schema is not None,
            phase_model=_phase_model(self.frame, phase_name),
            runtime_agents=_runtime_agent_specs(self.frame.meta),
        )
        for warning in resolved.warnings:
            self.log(f"⚠️ {warning}")

        with self._lock:
            agent_id = self.context.reserve_agent()
            label = str(opts.get("label") or f"agent-{agent_id}")
            record = AgentRecord(
                id=agent_id,
                label=label,
                phase=phase_name,
                prompt=prompt,
                prompt_preview=preview(prompt, 160),
                agent_type=resolved.agent_type_name,
                isolation=resolved.isolation or "shared",
                model=resolved.model,
            )
            self.frame.agents.append(record)
            self._notify()

        fingerprint = agent_fingerprint(
            prompt,
            {
                "schema": schema,
                **resolved.cache_inputs(),
            },
        )
        journal_key = f"v2:{fingerprint}"
        cached = self.resume_cache.get(fingerprint)
        if not is_cache_miss(cached):
            record.status = "done"
            record.result_preview = f"(cached) {preview(cached, 170)}"
            if schema:
                record.structured = {"status": "cached", "mode": "tool", "attempts": 0}
            record.started_at = monotonic()
            record.ended_at = record.started_at
            self.resume_cache.put(fingerprint, cached)
            self._journal(
                {
                    "type": "result",
                    "key": journal_key,
                    "agentId": str(agent_id),
                    "cached": True,
                    "result": cached,
                }
            )
            self._notify()
            return cached

        def on_child_start(metadata: dict[str, Any]) -> None:
            _apply_child_metadata(record, metadata)
            self._notify()

        def on_child_update(metadata: dict[str, Any]) -> None:
            with self._lock:
                _apply_child_metadata(record, metadata)
                activity = metadata.get("activity") if isinstance(metadata, dict) else None
                if activity:
                    self._journal(
                        {
                            "type": "activity",
                            "agentId": str(agent_id),
                            "activity": str(activity),
                        }
                    )
                approval = metadata.get("approval") if isinstance(metadata, dict) else None
                if isinstance(approval, dict):
                    self._journal(
                        {
                            "type": "approval",
                            "agentId": str(agent_id),
                            **approval,
                        }
                    )
                self._notify()

        request = ChildAgentRequest(
            id=agent_id,
            prompt=prompt,
            label=label,
            phase=phase_name,
            toolsets=list(resolved.toolsets),
            model=resolved.model,
            schema=schema,
            agent_type=resolved.agent_type_name,
            isolation=resolved.isolation,
            cwd=self.frame.cwd,
            structured_tool=bool(schema),
            on_start=on_child_start,
            on_update=on_child_update,
            resolved=resolved,
        )
        if schema:
            record.structured = {
                "status": "pending",
                "mode": "tool",
                "attempts": 0,
            }

        max_attempts = 1
        record.status = "running"
        record.started_at = monotonic()
        self._journal(
            {
                "type": "started",
                "key": journal_key,
                "agentId": str(agent_id),
            }
        )
        self._notify()

        accumulated_tokens = 0
        for attempt in range(max_attempts):
            try:
                with self.context.agent_slot():
                    raw_result = self._run_child(request, record)
                metadata = raw_result.metadata if isinstance(raw_result, ChildAgentResult) else {}
                result = raw_result.content if isinstance(raw_result, ChildAgentResult) else raw_result
                _apply_child_metadata(record, metadata)
                # Count every attempt's tokens toward the budget; record.tokens
                # reports the run total across attempts.
                self.context.record_tokens(record.tokens)
                accumulated_tokens += record.tokens
                if schema:
                    if not isinstance(metadata, dict) or not metadata.get("structured_captured"):
                        raise WorkflowRuntimeError(
                            "child agent did not submit valid structured output"
                        )
                    result = metadata.get("structured_result")
                    record.structured.update(
                        {
                            "status": "valid",
                            "mode": "tool",
                            "attempts": int(metadata.get("structured_attempts") or 1),
                            "error": "",
                        }
                    )
                record.status = "done"
                record.attempts = attempt + 1
                record.tokens = accumulated_tokens
                record.result_preview = preview(result, 180)
                self.resume_cache.put(fingerprint, result)
                self._journal(
                    {
                        "type": "result",
                        "key": journal_key,
                        "agentId": str(agent_id),
                        "result": result,
                    }
                )
                return result
            except WorkflowHalt:
                # A run-level halt (stop / deadline / token/agent/loop limit) is
                # not a child failure — never retry or swallow it.
                raise
            except ChildAgentSkipped:
                record.attempts = attempt + 1
                record.status = "skipped"
                record.tokens = accumulated_tokens
                record.result_preview = ""
                self._journal(
                    {
                        "type": "result",
                        "key": journal_key,
                        "agentId": str(agent_id),
                        "skipped": True,
                        "result": None,
                    }
                )
                return None
            except Exception as exc:
                record.attempts = attempt + 1
                record.status = "error"
                record.tokens = max(record.tokens, accumulated_tokens)
                record.error = f"{type(exc).__name__}: {exc}"
                self._journal(
                    {
                        "type": "error",
                        "key": journal_key,
                        "agentId": str(agent_id),
                        "error": record.error,
                    }
                )
                if isinstance(exc, ChildAgentError):
                    raise
                raise ChildAgentError(str(exc)) from exc
            finally:
                record.ended_at = monotonic()
                self._notify()
        raise ChildAgentError("child agent failed without a result")

    def _run_child(self, request: ChildAgentRequest, record: AgentRecord) -> Any:
        return self.runner.run(request)

    async def parallel(self, thunks: list[Callable[[], Any]]) -> list[Any]:
        self._check_deadline()
        if not isinstance(thunks, list):
            raise WorkflowRuntimeError("parallel() expects a list of callables")
        _check_vm_array_length(thunks)
        if not all(callable(item) for item in thunks):
            raise WorkflowRuntimeError("parallel() entries must be callables, e.g. lambda: agent(...)")
        if not thunks:
            return []

        results = await asyncio.gather(
            *(self._run_parallel_thunk(index, thunk) for index, thunk in enumerate(thunks))
        )
        self._check_deadline()
        return results

    async def _run_parallel_thunk(self, index: int, thunk: Callable[[], Any]) -> Any:
        try:
            self._check_deadline()
            return await _maybe_await(thunk())
        except WorkflowHalt:
            raise
        except Exception as exc:
            message = f"parallel[{index}] failed: {type(exc).__name__}: {exc}"
            self.log(message)
            if not isinstance(exc, ChildAgentError):
                with self._lock:
                    self.frame.errors.append(message)
            return None

    async def pipeline(self, items: list[Any], *stages: Callable[[Any, Any, int], Any]) -> list[Any]:
        self._check_deadline()
        if not isinstance(items, list):
            raise WorkflowRuntimeError("pipeline() expects a list as the first argument")
        _check_vm_array_length(items)
        if not stages or not all(callable(stage) for stage in stages):
            raise WorkflowRuntimeError("pipeline() expects one or more callable stages")

        async def run_one(index: int, original: Any) -> Any:
            current = original
            try:
                for stage in stages:
                    self._check_deadline()
                    current = await _maybe_await(stage(current, original, index))
            except WorkflowHalt:
                raise
            except Exception as exc:
                message = f"pipeline[{index}] failed: {type(exc).__name__}: {exc}"
                self.log(message)
                if not isinstance(exc, ChildAgentError):
                    with self._lock:
                        self.frame.errors.append(message)
                return None
            return current

        return await asyncio.gather(*(run_one(i, item) for i, item in enumerate(items)))

    def phase(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowRuntimeError("phase() expects a non-empty string")
        clean = name.strip()
        with self._lock:
            self.frame.current_phase = clean
            self.frame.ensure_phase(clean)
            self._notify()

    def log(self, message: Any) -> None:
        if not isinstance(message, str):
            raise WorkflowRuntimeError("log() expects a string")
        with self._lock:
            self.frame.logs.append(preview(message, 500))
            self._notify()

    async def workflow(self, name_or_ref: Any, args: Any = None) -> Any:
        return await asyncio.to_thread(self._workflow_sync, name_or_ref, args)

    def _workflow_sync(self, name_or_ref: Any, args: Any = None) -> Any:
        """Run a child workflow from async Python scripts."""
        self._check_deadline()
        max_depth = getattr(self.config, "max_nesting_depth", 1)
        if self.depth >= max_depth:
            raise WorkflowRuntimeError(
                f"nested workflows are limited to {max_depth} "
                f"level{'s' if max_depth != 1 else ''} deep"
            )
        from .runtime import WorkflowOptions, run_workflow
        from ..storage.store import WorkflowStore, resolve_workflow_source

        params = _normalize_workflow_ref(name_or_ref)
        store = self.context.store or WorkflowStore()
        try:
            source = resolve_workflow_source(params, store=store, cwd=self.frame.cwd)
        except WorkflowParseError as exc:
            if "name" in params:
                name = str(params.get("name") or "")
                available = ", ".join(_available_workflow_names(store, self.frame.cwd)) or "none"
                raise WorkflowRuntimeError(
                    f"workflow({name!r}): no workflow with that name. Available: {available}"
                ) from exc
            raise WorkflowRuntimeError(str(exc)) from exc
        result = run_workflow(
            source.script,
            WorkflowOptions(
                args=args,
                cwd=self.frame.cwd,
                config=self.config,
                child_runner=self.runner,
                context=self.context,
                parent_frame=self.frame,
                depth=self.depth + 1,
                source_ref=source.source_ref,
                store=store,
            ),
        )
        return result.value

    def _check_deadline(self) -> None:
        self.context.check_runtime()

    def _notify(self) -> None:
        self.context.notify()

    def _journal(self, event: dict[str, Any]) -> None:
        self.context.journal(event)


_PUBLIC_AGENT_OPT_KEYS = frozenset(
    {
        "label",
        "phase",
        "schema",
        "model",
        "isolation",
        "agentType",
        "toolsets",
        "allowedTools",
        "allowed_tools",
        "disallowedTools",
        "disallowed_tools",
        "instructions",
        "systemPrompt",
        "system_prompt",
        "description",
    }
)

_INLINE_AGENT_OPT_KEYS = frozenset(
    {
        "toolsets",
        "allowedTools",
        "allowed_tools",
        "disallowedTools",
        "disallowed_tools",
        "instructions",
        "systemPrompt",
        "system_prompt",
        "description",
    }
)
_TOOL_SURFACE_OPT_KEYS = frozenset(
    {"toolsets", "allowedTools", "allowed_tools", "disallowedTools", "disallowed_tools"}
)
_MISSING_AGENT_TYPE_POLICIES = {"error", "fallback_warn"}


def _validate_agent_opts(opts: dict[str, Any]) -> None:
    unknown = sorted(str(key) for key in opts if str(key) not in _PUBLIC_AGENT_OPT_KEYS)
    if not unknown:
        return
    raise WorkflowRuntimeError(
        "unsupported agent() option(s): "
        + ", ".join(unknown)
        + ". Public workflow agent options are label, phase, schema, model, "
        "isolation, agentType, toolsets, allowedTools, disallowedTools, "
        "instructions, and systemPrompt. Provider/runtime, timeout, and retry "
        "policy belong in Hermes/plugin configuration, not workflow scripts."
    )


def _check_vm_array_length(items: list[Any]) -> None:
    if len(items) > MAX_VM_ARRAY_ITEMS:
        raise WorkflowRuntimeError(
            f"array length {len(items)} exceeds the maximum of {MAX_VM_ARRAY_ITEMS} "
            "supported across the workflow VM boundary"
        )


def _normalize_agent_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    return clean or None


def _normalize_agent_model(value: Any) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    if not clean or clean.lower() == "inherit":
        return None
    return clean


def _normalize_isolation(value: Any) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    if clean == "worktree":
        return clean
    raise WorkflowRuntimeError("isolation must be 'worktree'")


def _runtime_agent_specs(meta: dict[str, Any]) -> dict[str, Any]:
    raw = meta.get("agents") if isinstance(meta, dict) else None
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise WorkflowRuntimeError("meta.agents must be an object")
    from ..child.presets import build_runtime_agent_type

    specs: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise WorkflowRuntimeError("meta.agents keys must be strings")
        source = f"meta.agents.{key}"
        try:
            spec = build_runtime_agent_type(key, value, source=source)
        except ValueError as exc:
            raise WorkflowRuntimeError(str(exc)) from exc
        specs[key.strip()] = spec
        specs[spec.name] = spec
    return specs


def _resolve_agent_spec(
    opts: dict[str, Any],
    *,
    cwd: str,
    config: Any,
    structured_output: bool,
    phase_model: str | None = None,
    runtime_agents: dict[str, Any] | None = None,
) -> ResolvedAgentSpec:
    from ..child.presets import generic_agent_type, list_agent_types, resolve_agent_type
    from ..child.runner import (
        _prepare_mcp_tool_registry,
        _resolve_child_toolsets,
        build_child_system_prompt,
    )

    runtime_agents = runtime_agents or {}
    explicit_type = _normalize_agent_type(opts.get("agentType"))
    requested_type = explicit_type or "general-purpose"
    warnings: list[str] = []

    if explicit_type:
        agent_type_spec = runtime_agents.get(requested_type) or resolve_agent_type(requested_type, cwd=cwd)
        if agent_type_spec is None:
            policy = _missing_agent_type_policy(config)
            if policy == "fallback_warn":
                warning = (
                    f"agentType '{requested_type}' not found; falling back to "
                    "general-purpose"
                )
                warnings.append(warning)
                agent_type_spec = (
                    runtime_agents.get("general-purpose")
                    or resolve_agent_type("general-purpose", cwd=cwd)
                    or generic_agent_type()
                )
            else:
                available = ", ".join(_available_agent_names(cwd, runtime_agents)) or "none"
                raise WorkflowRuntimeError(
                    f"agent({{agentType}}): agent type '{requested_type}' not found. "
                    f"Available agents: {available}"
                )
    else:
        agent_type_spec = (
            runtime_agents.get("general-purpose")
            or resolve_agent_type("general-purpose", cwd=cwd)
            or generic_agent_type()
        )

    effective_spec = _compose_effective_agent_type(agent_type_spec, opts)
    explicit_isolation = _normalize_isolation(opts.get("isolation"))
    agent_type_isolation = _normalize_agent_type_isolation(
        getattr(effective_spec, "isolation", None)
    )
    model = _normalize_agent_model(
        opts.get("model")
        if opts.get("model")
        else phase_model
        if phase_model
        else getattr(effective_spec, "model", None)
    )
    _prepare_mcp_tool_registry(config)
    has_inline_or_meta_tool_surface = _has_inline_tool_surface(opts) or (
        not explicit_type
        and requested_type in runtime_agents
        and _spec_has_tool_surface(effective_spec)
    )
    toolsets = tuple(
        _resolve_child_toolsets(
            config,
            [],
            getattr(effective_spec, "toolsets", ()),
            agent_type_toolsets_explicit=bool(
                getattr(effective_spec, "toolsets_explicit", False)
                or getattr(effective_spec, "toolsets", ())
            ),
            include_discoverable=not explicit_type and not has_inline_or_meta_tool_surface,
        )
    )
    prompt = build_child_system_prompt(
        effective_spec,
        structured_output=structured_output,
    )
    return ResolvedAgentSpec(
        requested_agent_type=requested_type,
        agent_type_spec=effective_spec,
        model=model or None,
        isolation=explicit_isolation or agent_type_isolation,
        toolsets=toolsets,
        toolsets_explicit=bool(getattr(effective_spec, "toolsets_explicit", False)),
        allowed_tools=tuple(getattr(effective_spec, "allowed_tools", ()) or ()),
        allowed_tools_explicit=bool(getattr(effective_spec, "allowed_tools_explicit", False)),
        disallowed_tools=tuple(getattr(effective_spec, "disallowed_tools", ()) or ()),
        system_prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        workspace=str(Path(cwd).expanduser().resolve()),
        warnings=tuple(warnings),
    )


def _compose_effective_agent_type(base_spec: Any, opts: dict[str, Any]) -> Any:
    from ..child.presets import AgentTypeSpec

    inline_instructions = _inline_instructions(opts)
    inline_toolsets, inline_toolsets_explicit = _tuple_option(opts, "toolsets", "toolsets")
    inline_allowed, inline_allowed_explicit = _tuple_option(
        opts, "allowedTools", "allowed_tools", field_name="allowedTools"
    )
    inline_disallowed, inline_disallowed_explicit = _tuple_option(
        opts, "disallowedTools", "disallowed_tools", field_name="disallowedTools"
    )

    instructions = str(getattr(base_spec, "instructions", "") or "").strip()
    source = str(getattr(base_spec, "source", "") or "inline")
    if inline_instructions:
        if instructions:
            instructions = (
                instructions
                + "\n\nAdditional inline workflow instructions:\n\n"
                + inline_instructions
            )
        else:
            instructions = inline_instructions
        source = source + "+inline"

    base_toolsets = tuple(getattr(base_spec, "toolsets", ()) or ())
    base_toolsets_explicit = bool(getattr(base_spec, "toolsets_explicit", False) or base_toolsets)
    if inline_toolsets_explicit:
        toolsets = inline_toolsets
        toolsets_explicit = True
    else:
        toolsets = base_toolsets
        toolsets_explicit = base_toolsets_explicit

    base_allowed = tuple(getattr(base_spec, "allowed_tools", ()) or ())
    base_allowed_explicit = bool(getattr(base_spec, "allowed_tools_explicit", False) or base_allowed)
    if base_allowed_explicit and inline_allowed_explicit:
        inline_allowed_set = set(inline_allowed)
        allowed_tools = tuple(item for item in base_allowed if item in inline_allowed_set)
    elif inline_allowed_explicit:
        allowed_tools = inline_allowed
    elif base_allowed_explicit:
        allowed_tools = base_allowed
    else:
        allowed_tools = ()
    allowed_tools_explicit = base_allowed_explicit or inline_allowed_explicit

    disallowed_tools = _dedupe_tuple(
        tuple(getattr(base_spec, "disallowed_tools", ()) or ())
        + (inline_disallowed if inline_disallowed_explicit else ())
    )

    return AgentTypeSpec(
        name=str(getattr(base_spec, "name", None) or "general-purpose"),
        instructions=instructions,
        source=source,
        description=str(opts.get("description") or getattr(base_spec, "description", "") or ""),
        toolsets=toolsets,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=getattr(base_spec, "model", None),
        isolation=getattr(base_spec, "isolation", None),
        toolsets_explicit=toolsets_explicit,
        allowed_tools_explicit=allowed_tools_explicit,
    )


def _inline_instructions(opts: dict[str, Any]) -> str:
    value, explicit = _first_present(opts, "instructions", "systemPrompt", "system_prompt")
    if not explicit:
        return ""
    return str(value or "").strip()


def _tuple_option(
    opts: dict[str, Any],
    *keys: str,
    field_name: str | None = None,
) -> tuple[tuple[str, ...], bool]:
    value, explicit = _first_present(opts, *keys)
    if not explicit or value is None:
        return (), False
    label = field_name or keys[0]
    return _strict_string_tuple(value, label), True


def _first_present(data: dict[str, Any], *keys: str) -> tuple[Any, bool]:
    for key in keys:
        if key in data:
            return data.get(key), True
    return None, False


def _strict_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raise WorkflowRuntimeError(f"{field_name} must be a string or list of strings")
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise WorkflowRuntimeError(f"{field_name} must contain only strings")
        clean = item.strip()
        if not clean:
            if isinstance(value, str) and not value.strip():
                continue
            raise WorkflowRuntimeError(f"{field_name} must contain non-empty strings")
        cleaned.append(clean)
    return tuple(cleaned)


def _has_inline_tool_surface(opts: dict[str, Any]) -> bool:
    return any(key in opts and opts.get(key) is not None for key in _TOOL_SURFACE_OPT_KEYS)


def _spec_has_tool_surface(spec: Any) -> bool:
    return bool(
        getattr(spec, "toolsets_explicit", False)
        or getattr(spec, "allowed_tools_explicit", False)
        or getattr(spec, "disallowed_tools", ())
    )


def _dedupe_tuple(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        name = str(item).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return tuple(cleaned)


def _missing_agent_type_policy(config: Any) -> str:
    value = str(getattr(config, "missing_agent_type_policy", "error") or "error").strip()
    return value if value in _MISSING_AGENT_TYPE_POLICIES else "error"


def _available_agent_names(cwd: str, runtime_agents: dict[str, Any]) -> list[str]:
    from ..child.presets import list_agent_types

    names: list[str] = []
    seen: set[str] = set()
    for name in runtime_agents:
        clean = str(name).strip()
        if clean and clean not in seen:
            seen.add(clean)
            names.append(clean)
    for spec in list_agent_types(cwd=cwd):
        if spec.name not in seen:
            seen.add(spec.name)
            names.append(spec.name)
    return names


def _phase_model(frame: WorkflowFrame, phase_name: str | None) -> str | None:
    if not phase_name:
        return None
    for phase in frame.phases:
        if phase.title == phase_name:
            return _normalize_agent_model(phase.model)
    return None


def _normalize_workflow_ref(name_or_ref: Any) -> dict[str, str]:
    if isinstance(name_or_ref, str) and name_or_ref.strip():
        return {"name": name_or_ref.strip()}
    if isinstance(name_or_ref, dict) and set(name_or_ref) == {"scriptPath"}:
        script_path = name_or_ref.get("scriptPath")
        if isinstance(script_path, str) and script_path.strip():
            return {"scriptPath": script_path.strip()}
    raise WorkflowRuntimeError(
        "workflow() expects a non-empty workflow name or {'scriptPath': '<path>'}"
    )


def _available_workflow_names(store: Any, cwd: str) -> list[str]:
    from ..storage.store import _RESERVED_WORKFLOW_NAMES

    directories = [
        Path(cwd) / ".hermes" / "workflows",
        store.workflows_dir,
        Path(__file__).resolve().parent.parent / "workflows",
    ]
    names: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        try:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                stem = path.stem
                if not stem or stem.startswith("_") or stem in _RESERVED_WORKFLOW_NAMES:
                    continue
                if stem not in seen:
                    seen.add(stem)
                    names.append(stem)
        except OSError:
            continue
    return names


def _normalize_agent_type_isolation(value: Any) -> str | None:
    if value in (None, "", "shared", "none"):
        return None
    clean = str(value).strip()
    if clean == "worktree":
        return clean
    raise WorkflowRuntimeError(
        f"agentType isolation must be 'worktree' when set, got {clean!r}"
    )


def _apply_child_metadata(record: AgentRecord, metadata: dict[str, Any]) -> None:
    if not isinstance(metadata, dict):
        return
    record.runner = str(metadata.get("runner") or record.runner)
    record.workspace = _optional_str(metadata.get("workspace"))
    record.model = _optional_str(metadata.get("model"))
    record.task_id = _optional_str(metadata.get("task_id"))
    record.hermes_session_id = _optional_str(
        metadata.get("hermes_session_id") or metadata.get("session_id")
    )
    record.transcript_path = _optional_str(metadata.get("transcript_path"))
    record.agent_type = _optional_str(metadata.get("agent_type")) or record.agent_type
    record.isolation = _optional_str(metadata.get("isolation")) or record.isolation
    record.tokens = _as_int_metadata(metadata.get("tokens"))
    record.cache_read_tokens = _as_int_metadata(metadata.get("cache_read_tokens"))
    record.cache_write_tokens = _as_int_metadata(metadata.get("cache_write_tokens"))
    record.input_tokens = _as_int_metadata(metadata.get("input_tokens"))
    record.output_tokens = _as_int_metadata(metadata.get("output_tokens"))
    record.reasoning_tokens = _as_int_metadata(metadata.get("reasoning_tokens"))
    record.provider = _optional_str(metadata.get("provider")) or record.provider
    record.base_url = _optional_str(metadata.get("base_url")) or record.base_url
    record.reasoning_effort = (
        _optional_str(metadata.get("reasoning_effort")) or record.reasoning_effort
    )
    record.tool_calls = _as_int_metadata(metadata.get("tool_calls"))


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    return clean or None


def _as_int_metadata(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
