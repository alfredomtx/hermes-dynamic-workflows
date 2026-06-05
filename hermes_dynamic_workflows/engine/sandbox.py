"""AST validation for generated Python workflow scripts.

This is a guardrail, not a perfect Python sandbox. The runtime keeps the
available globals narrow and this validator rejects the most dangerous syntax.
The next hardening step is to execute scripts in a subprocess and expose
agent()/phase()/log() through RPC.
"""

from __future__ import annotations

import ast
from typing import Any

from .config import PluginConfig
from .errors import SandboxViolation, WorkflowParseError

# We gate CAPABILITY (what a script can touch), not CONTROL FLOW (how it loops
# or branches). while/try/raise are pure control flow — harmless on their own
# and required by the documented loop-until-budget / loop-until-dry / catch-
# gracefully patterns — so they are allowed. Imports, file/process/network
# access, dunder traversal and dynamic eval stay forbidden; that is the real
# integrity+escape boundary (all world-access must go through child agents and
# Hermes' approval engine, never the orchestration script itself).
FORBIDDEN_NODES = (
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Nonlocal,
    ast.With,
)

FORBIDDEN_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "type",
    "vars",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "sys",
    "importlib",
}

MAX_AST_NODES = 2500
MAX_STRING_LITERAL_CHARS = 20000
MAX_ABS_INT_LITERAL = 10**9


def parse_script(script: str, config: PluginConfig) -> ast.Module:
    if not isinstance(script, str) or not script.strip():
        raise WorkflowParseError("workflow script must be a non-empty Python string")
    if len(script) > config.script_max_chars:
        raise WorkflowParseError(
            f"workflow script is too large ({len(script)} chars; max {config.script_max_chars})"
        )
    try:
        tree = ast.parse(script, filename="<workflow>", mode="exec")
    except SyntaxError as exc:
        raise WorkflowParseError(f"invalid Python workflow script: {exc.msg} at line {exc.lineno}") from exc
    validate_ast(tree)
    return instrument_loops(tree)


def validate_ast(tree: ast.AST) -> None:
    count = 0
    for node in ast.walk(tree):
        count += 1
        if count > MAX_AST_NODES:
            raise SandboxViolation(f"workflow script is too complex (>{MAX_AST_NODES} AST nodes)")

        if isinstance(node, FORBIDDEN_NODES):
            raise SandboxViolation(f"forbidden Python syntax: {type(node).__name__}")

        if isinstance(node, ast.Name):
            _validate_name(node.id)

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise SandboxViolation(f"forbidden attribute access: {node.attr}")

        if isinstance(node, ast.Constant):
            _validate_constant(node.value)

        if isinstance(node, ast.Call):
            _validate_call(node)

        if isinstance(node, ast.ExceptHandler):
            _validate_except_handler(node)


def extract_meta(tree: ast.Module) -> dict[str, Any]:
    """Extract a literal top-level ``meta = {...}`` assignment when present."""
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != "meta":
            continue
        try:
            value = ast.literal_eval(stmt.value)
        except (ValueError, TypeError, SyntaxError, MemoryError):
            raise WorkflowParseError("meta must be a literal dict")
        if not isinstance(value, dict):
            raise WorkflowParseError("meta must be a dict")
        return _normalize_meta(value)
    return {"name": "dynamic-workflow", "description": ""}


def _normalize_meta(value: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise WorkflowParseError("meta keys must be strings")
        if key.startswith("_") or key in {"__proto__", "constructor", "prototype"}:
            raise WorkflowParseError(f"forbidden meta key: {key}")
        if key in {"name", "description", "whenToUse"}:
            if item is not None and not isinstance(item, str):
                raise WorkflowParseError(f"meta.{key} must be a string")
            meta[key] = item or ""
        elif key == "phases":
            if not isinstance(item, list):
                raise WorkflowParseError("meta.phases must be a list")
            normalized = []
            for part in item:
                if isinstance(part, str):
                    normalized.append(part)
                    continue
                if isinstance(part, dict):
                    title = part.get("title")
                    if not isinstance(title, str) or not title.strip():
                        raise WorkflowParseError("meta.phases object entries require a title string")
                    entry = {"title": title.strip()}
                    for phase_key in ("detail", "model"):
                        value = part.get(phase_key)
                        if value is not None:
                            if not isinstance(value, str):
                                raise WorkflowParseError(f"meta.phases.{phase_key} must be a string")
                            entry[phase_key] = value
                    normalized.append(entry)
                    continue
                raise WorkflowParseError("meta.phases entries must be strings or objects")
            meta[key] = normalized
        else:
            meta[key] = item
    name = str(meta.get("name") or "").strip()
    if not name:
        raise WorkflowParseError("meta.name must be a non-empty string")
    meta["name"] = name
    meta.setdefault("description", "")
    return meta


def _validate_name(name: str) -> None:
    if name.startswith("__") or name in FORBIDDEN_NAMES:
        raise SandboxViolation(f"forbidden name: {name}")


def _validate_constant(value: Any) -> None:
    if isinstance(value, str) and len(value) > MAX_STRING_LITERAL_CHARS:
        raise SandboxViolation("string literal is too large")
    if isinstance(value, int) and abs(value) > MAX_ABS_INT_LITERAL:
        raise SandboxViolation("integer literal is too large")


def _validate_call(node: ast.Call) -> None:
    func = node.func
    if isinstance(func, ast.Name):
        _validate_name(func.id)
    elif isinstance(func, ast.Attribute):
        if func.attr.startswith("_"):
            raise SandboxViolation(f"forbidden method call: {func.attr}")
    else:
        raise SandboxViolation("dynamic call targets are not allowed")


def _validate_except_handler(node: ast.ExceptHandler) -> None:
    """Forbid wildcard catches that could swallow a ``WorkflowHalt``.

    A ``WorkflowHalt`` (user stop / deadline / hard limit) derives from
    ``BaseException``, so ``except Exception`` cannot catch it — but a bare
    ``except:`` or ``except BaseException`` would. Reject those so a run stays
    cancellable and bounded no matter what the script catches. Scripts may
    still ``except Exception`` (or a specific exposed type) to handle
    recoverable failures gracefully.
    """
    if node.type is None:
        raise SandboxViolation(
            "bare 'except:' is not allowed; catch Exception or a specific type"
        )
    handlers = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
    for handler_type in handlers:
        if isinstance(handler_type, ast.Name) and handler_type.id == "BaseException":
            raise SandboxViolation(
                "'except BaseException' is not allowed; catch Exception instead"
            )


# Name of the guard call the loop instrumenter injects; the runtime binds it in
# the script namespace. Dunder-prefixed so a script cannot define or shadow it
# (the validator forbids names starting with "__").
LOOP_GUARD_NAME = "__wf_tick__"


class _LoopGuard(ast.NodeTransformer):
    """Rewrite ``while TEST:`` into ``while __wf_tick__() and (TEST):``.

    ``__wf_tick__()`` checks stop/deadline and the loop-iteration cap (raising a
    WorkflowHalt if exceeded) and otherwise returns True, so the original TEST
    still controls the loop. This makes the cooperative deadline fire inside a
    pure-compute loop that never calls agent().
    """

    def visit_While(self, node: ast.While) -> ast.While:
        self.generic_visit(node)
        guard = ast.Call(
            func=ast.Name(id=LOOP_GUARD_NAME, ctx=ast.Load()), args=[], keywords=[]
        )
        node.test = ast.BoolOp(op=ast.And(), values=[guard, node.test])
        return node


def instrument_loops(tree: ast.Module) -> ast.Module:
    """Inject the per-iteration loop guard into every ``while`` loop.

    Runs after ``validate_ast`` (the injected nodes are trusted and not
    re-validated). ``for`` loops are bounded by their iterable and left alone.
    """
    _LoopGuard().visit(tree)
    ast.fix_missing_locations(tree)
    return tree
