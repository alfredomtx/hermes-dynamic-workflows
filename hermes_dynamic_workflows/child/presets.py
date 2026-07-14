"""Agent type resolution for workflow child agents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage.store import default_store_root


REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class AgentTypeSpec:
    name: str
    instructions: str
    source: str
    description: str = ""
    toolsets: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model: str | None = None
    isolation: str | None = None
    toolsets_explicit: bool = False
    allowed_tools_explicit: bool = False
    max_turns: int | None = None
    reasoning_effort: str | None = None


def build_runtime_agent_type(
    name: str,
    data: Any,
    *,
    source: str,
    reasoning_key: str = "reasoningEffort",
) -> AgentTypeSpec:
    """Build an in-memory agent type from meta["agents"] or structured files."""
    clean_name = _validate_runtime_agent_name(str(name or ""), source=source)
    if not isinstance(data, dict):
        raise ValueError(f"{source} must be an object")
    max_turns = _max_turns_from(data, source=source)
    reasoning_effort = _reasoning_effort_from(data, source=source, key=reasoning_key)
    spec_name = _validate_runtime_agent_name(
        str(data.get("name") or clean_name), source=source
    )
    instructions_value = (
        data.get("instructions")
        or data.get("systemPrompt")
        or data.get("system_prompt")
        or data.get("prompt")
        or data.get("content")
        or ""
    )
    instructions = str(instructions_value).strip()
    if not instructions:
        raise ValueError(f"{source} is missing instructions")
    toolsets, toolsets_explicit = _as_tuple_strict(
        _first_present(data, "toolsets", "tools"), "toolsets"
    )
    allowed_tools, allowed_tools_explicit = _as_tuple_strict(
        _first_present(data, "allowedTools", "allowed_tools"), "allowedTools"
    )
    disallowed_tools, _ = _as_tuple_strict(
        _first_present(data, "disallowedTools", "disallowed_tools"),
        "disallowedTools",
    )
    return AgentTypeSpec(
        name=spec_name,
        instructions=instructions,
        source=source,
        description=_description_from(data),
        toolsets=toolsets,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        model=_as_optional_str(data.get("model")),
        isolation=_as_runtime_isolation(data.get("isolation"), source=source),
        toolsets_explicit=toolsets_explicit,
        allowed_tools_explicit=allowed_tools_explicit,
        max_turns=max_turns,
        reasoning_effort=reasoning_effort,
    )


def generic_agent_type() -> AgentTypeSpec:
    return AgentTypeSpec(
        name="general-purpose",
        instructions=(
            "You are an agent. Use the available tools to complete the task fully. "
            "Return concise task results to the calling workflow script."
        ),
        source="builtin:generic-fallback",
        description="Generic workflow child agent fallback.",
        toolsets=("*",),
        model="inherit",
        toolsets_explicit=True,
    )


def resolve_agent_type(name: str | None, *, cwd: str | None = None) -> AgentTypeSpec | None:
    clean = str(name or "").strip()
    if not clean:
        return None

    path = _find_agent_type_file(clean, cwd=cwd)
    if path is not None:
        return _load_agent_type_file(clean, path)

    for base in _agent_type_bases(cwd):
        if not base.is_dir():
            continue
        for candidate in sorted(base.rglob("*")):
            if not candidate.is_file() or candidate.suffix.lower() not in {
                ".md",
                ".yaml",
                ".yml",
                ".json",
            }:
                continue
            try:
                rel = candidate.resolve().relative_to(base.resolve())
                spec = _load_agent_type_file(str(rel.with_suffix("")), candidate)
            except Exception:
                if _declared_agent_name(candidate) == clean:
                    raise
                continue
            if spec.name == clean:
                return spec
    return None


def list_agent_types(*, cwd: str | None = None) -> list[AgentTypeSpec]:
    """Return active workflow agent types in resolution precedence order."""
    active: dict[str, AgentTypeSpec] = {}
    for base in _agent_type_bases(cwd):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".yaml", ".yml", ".json"}:
                continue
            try:
                rel = path.resolve().relative_to(base.resolve())
            except ValueError:
                continue
            try:
                spec = _load_agent_type_file(str(rel.with_suffix("")), path)
            except Exception:
                continue
            active.setdefault(spec.name, spec)
    return list(active.values())


def _find_agent_type_file(name: str, *, cwd: str | None) -> Path | None:
    rel = _safe_agent_type_relative_path(name)
    suffix = rel.suffix.lower()
    rels = [rel] if suffix else [rel.with_suffix(ext) for ext in (".md", ".yaml", ".yml", ".json")]
    for base in _agent_type_bases(cwd):
        for candidate_rel in rels:
            candidate = (base / candidate_rel).resolve()
            try:
                candidate.relative_to(base.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
    return None


def _agent_type_bases(cwd: str | None) -> list[Path]:
    bases: list[Path] = []
    if cwd:
        bases.append(Path(cwd).expanduser() / ".hermes" / "dynamic-workflows" / "agents")
    bases.append(default_store_root() / "agents")
    plugin_root = Path(__file__).resolve().parent.parent
    bases.append(plugin_root / "agents")
    return bases


def _safe_agent_type_relative_path(name: str) -> Path:
    raw = str(name or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("agentType must not be empty")
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError(f"invalid agentType: {name!r}")
    if any(part.startswith(".") for part in parts):
        raise ValueError(f"invalid agentType: {name!r}")
    return Path(*parts)


def _load_agent_type_file(name: str, path: Path) -> AgentTypeSpec:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_structured_agent_type(name, path, _read_json(path))
    if suffix in {".yaml", ".yml"}:
        return _load_structured_agent_type(name, path, _read_yaml(path))
    return _load_markdown_agent_type(name, path)


def _load_markdown_agent_type(name: str, path: Path) -> AgentTypeSpec:
    text = path.read_text(encoding="utf-8")
    try:
        frontmatter, body = _parse_frontmatter(text)
    except Exception as exc:
        raise ValueError(f"invalid agentType file {path}: {exc}") from exc
    body = body.strip() or text.strip()
    return AgentTypeSpec(
        name=str(frontmatter.get("name") or Path(name).stem),
        instructions=body,
        source=str(path),
        description=_description_from(frontmatter),
        toolsets=_as_tuple(_first_present(frontmatter, "toolsets", "tools")),
        allowed_tools=_as_tuple(_first_present(frontmatter, "allowed_tools", "allowedTools")),
        disallowed_tools=_as_tuple(_first_present(frontmatter, "disallowed_tools", "disallowedTools")),
        model=_as_optional_str(frontmatter.get("model")),
        isolation=_as_optional_str(frontmatter.get("isolation")),
        toolsets_explicit=("toolsets" in frontmatter or "tools" in frontmatter),
        allowed_tools_explicit=("allowed_tools" in frontmatter or "allowedTools" in frontmatter),
        max_turns=_max_turns_from(frontmatter, source=str(path)),
        reasoning_effort=_reasoning_effort_from(
            frontmatter,
            source=str(path),
            key="reasoning_effort",
        ),
    )


def _load_structured_agent_type(name: str, path: Path, data: Any) -> AgentTypeSpec:
    if not isinstance(data, dict):
        raise ValueError(f"agentType file must contain an object: {path}")
    data = dict(data)
    data.setdefault("name", Path(name).stem)
    try:
        return build_runtime_agent_type(
            str(data.get("name") or name),
            data,
            source=str(path),
            reasoning_key="reasoning_effort",
        )
    except ValueError as exc:
        raise ValueError(f"invalid agentType file {path}: {exc}") from exc


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text
    end = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if end is None:
        raise ValueError("unterminated Markdown frontmatter")
    raw = "".join(lines[1:end]).strip()
    body = "".join(lines[end + 1 :]).lstrip("\r\n")
    data = _read_yaml_text(raw)
    return (data if isinstance(data, dict) else {}, body)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> Any:
    return _read_yaml_text(path.read_text(encoding="utf-8"))


def _read_yaml_text(text: str) -> Any:
    try:
        import yaml
    except Exception as exc:
        return _read_simple_yaml_text(text)
    return yaml.safe_load(text) or {}


def _read_simple_yaml_text(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{"):
            raise ValueError("flow mappings require PyYAML")
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if value.lstrip().startswith("{"):
            raise ValueError("flow mappings require PyYAML")
        key = key.strip()
        turn_limit_key = key.strip("{}").strip().strip("'\"")
        if turn_limit_key in ("maxTurns", "max_turns"):
            key = turn_limit_key
        value = value.strip()
        if value.startswith("[") != value.endswith("]"):
            raise ValueError("unterminated flow sequence")
        if not key:
            continue
        if key == "maxTurns" and re.fullmatch(r"[+-]?\d+", value):
            data[key] = int(value)
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
        else:
            data[key] = value.strip("'\"")
    return data


def _max_turns_from(data: dict[str, Any], *, source: str) -> int | None:
    if "max_turns" in data:
        raise ValueError(f"{source} max_turns is not supported; use maxTurns")
    if "maxTurns" not in data:
        return None
    value = data["maxTurns"]
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError(f"{source} maxTurns must be an integer from 1 to 1000")
    return value


def _reasoning_effort_from(
    data: dict[str, Any],
    *,
    source: str,
    key: str,
) -> str | None:
    alias = "reasoning_effort" if key == "reasoningEffort" else "reasoningEffort"
    if alias in data:
        raise ValueError(f"{source} {alias} is not supported; use {key}")
    if key not in data:
        return None
    value = data[key]
    if type(value) is not str or value not in REASONING_EFFORTS:
        allowed = ", ".join(REASONING_EFFORTS)
        raise ValueError(f"{source} {key} must be one of: {allowed}")
    return value


def _declared_agent_name(path: Path) -> str | None:
    try:
        if path.suffix.lower() == ".json":
            data = _read_json(path)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            data = _read_yaml(path)
        else:
            data, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("name")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def _validate_runtime_agent_name(name: str, *, source: str) -> str:
    clean = str(name or "").strip()
    try:
        rel = _safe_agent_type_relative_path(clean)
    except Exception as exc:
        raise ValueError(f"invalid runtime agent name: {clean!r}") from exc
    if len(rel.parts) != 1 or rel.name != clean:
        raise ValueError(f"invalid runtime agent name: {clean!r}")
    return clean


def _as_tuple_strict(value: Any, field_name: str) -> tuple[tuple[str, ...], bool]:
    if value is None:
        return (), False
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings")
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        clean = item.strip()
        if not clean:
            if isinstance(value, str) and not value.strip():
                continue
            raise ValueError(f"{field_name} must contain non-empty strings")
        cleaned.append(clean)
    return tuple(cleaned), True


def _as_runtime_isolation(value: Any, *, source: str) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    if clean in {"shared", "none"}:
        return None
    if clean == "worktree":
        return clean
    raise ValueError(f"{source} isolation must be 'worktree', 'shared', or 'none'")


def _description_from(data: dict[str, Any]) -> str:
    description = str(data.get("description") or data.get("whenToUse") or "").strip()
    return description.replace("\\n", "\n").replace('\\"', '"')


def _as_optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    clean = str(value).strip()
    return clean or None
