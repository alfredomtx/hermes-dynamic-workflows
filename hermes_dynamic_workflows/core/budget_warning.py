"""Conservative static warnings for undersized workflow child budgets."""

from __future__ import annotations

import ast
from typing import Any


_BUDGET_LIMITS = (
    ("maxTurns", 20),
    ("maxToolCalls", 20),
    ("maxToolOutputChars", 200_000),
)
_NARROW_TOOLSETS = frozenset({"file", "web"})
_TOOLSET_KEYS = ("toolsets",)
_ALLOWED_TOOL_KEYS = ("allowedTools", "allowed_tools")


def find_budget_warnings(script: str) -> tuple[str, ...]:
    """Return deterministic, advisory warnings for suspicious child budgets.

    This deliberately inspects only literal AST values. Invalid syntax, dynamic
    metadata, dynamic options, and dynamic budget values produce no warning;
    launch validation remains responsible for enforcing the actual contract.
    """
    tree = _parse_script(script)
    if tree is None or _literal_phase_count(tree) < 2:
        return ()

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_agent_call(node)
    ]
    calls.sort(key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))

    warnings: list[str] = []
    for index, call in enumerate(calls):
        options = _literal_options(call)
        if options is None or not _has_broad_tool_surface(options):
            continue
        low_dimensions = [
            f"{key}={value}"
            for key, limit in _BUDGET_LIMITS
            for value in (options.get(key),)
            if type(value) is int and value <= limit
        ]
        if not low_dimensions:
            continue

        label = options.get("label")
        if isinstance(label, str) and label.strip():
            identity = f"{label.strip()!r} (index {index})"
        else:
            identity = f"index {index}"
        dimensions = ", ".join(low_dimensions)
        warnings.append(
            f"Advisory child-budget warning for agent {identity}: low literal budget(s) "
            f"{dimensions}. These are hard ceilings; launch continues. For broad "
            "multi-phase work, split phases through pipeline() or increase only the "
            "limiting dimension."
        )
    return tuple(warnings)


def _parse_script(script: str) -> ast.Module | None:
    if not isinstance(script, str):
        return None
    try:
        return ast.parse(script)
    except (SyntaxError, ValueError, TypeError, MemoryError):
        return None


def _literal_phase_count(tree: ast.Module) -> int:
    if not tree.body:
        return 0
    first = tree.body[0]
    if not isinstance(first, ast.Assign) or len(first.targets) != 1:
        return 0
    target = first.targets[0]
    if not isinstance(target, ast.Name) or target.id != "meta":
        return 0
    try:
        meta = ast.literal_eval(first.value)
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return 0
    if not isinstance(meta, dict):
        return 0
    phases = meta.get("phases")
    return len(phases) if isinstance(phases, list) else 0


def _is_agent_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "agent"
    )


def _literal_options(call: ast.Call) -> dict[str, Any] | None:
    if len(call.args) < 2 or not isinstance(call.args[1], ast.Dict):
        return None
    try:
        options = ast.literal_eval(call.args[1])
    except (ValueError, TypeError, SyntaxError, MemoryError):
        return None
    return options if isinstance(options, dict) else None


def _has_broad_tool_surface(options: dict[str, Any]) -> bool:
    """Recognize default or plainly broad surfaces without guessing intent."""
    allowed = _first_present(options, _ALLOWED_TOOL_KEYS)
    toolsets = _first_present(options, _TOOLSET_KEYS)

    # An explicit empty allowlist/toolset has no normal tools. It is also the
    # boring exemption for evidence-only synthesizers.
    if allowed is not _MISSING and allowed == []:
        return False
    if toolsets is not _MISSING and toolsets == []:
        return False

    # A non-empty explicit allowlist is bounded by definition. A wildcard is
    # the sole allowlist form broad enough to warn about here.
    if allowed is not _MISSING and allowed is not None:
        if not isinstance(allowed, list):
            return False
        return "*" in allowed

    # Omitted/None toolsets inherit the default workflow surface.
    if toolsets is _MISSING or toolsets is None:
        return True
    if not isinstance(toolsets, list):
        return False
    names = [item for item in toolsets if isinstance(item, str) and item.strip()]
    if len(names) != len(toolsets) or not names:
        return False
    if "*" in names:
        return True
    if len(names) == 1 and names[0] in _NARROW_TOOLSETS:
        return False
    return len(names) > 1


_MISSING = object()


def _first_present(options: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in options:
            return options[key]
    return _MISSING
