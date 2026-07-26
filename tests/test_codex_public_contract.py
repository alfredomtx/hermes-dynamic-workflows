from __future__ import annotations

from pathlib import Path

import pytest

from hermes_dynamic_workflows.adapters.workflow import _DESCRIPTION


ROOT = Path(__file__).parents[1]
MAINTAINED_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "README.ja-JP.md",
    ROOT / "TECHNICAL.md",
)


def _contract_text(document: Path) -> str:
    assert document.is_file(), f"maintained document is missing: {document}"
    return document.read_text(encoding="utf-8")


def _assert_codex_contract(text: str, source: str) -> None:
    lowered = text.lower()
    assert "codex(opts)" in text, source
    for mode in ("code", "discover", "debug", "verify"):
        assert mode in lowered, source
    assert "allowfiles" in lowered, source
    assert "code" in lowered and "allowfiles" in lowered, source
    if source in {"workflow tool description", "README.md", "TECHNICAL.md"}:
        assert "single bounded" in lowered or "one bounded" in lowered, source
    assert "agent()" in text, source
    assert (
        "agent() -> codex" in lowered
        or "agent() to codex" in lowered
        or "nested" in lowered and "codex" in lowered
    ), source
    for forbidden in ("commit", "push", "rebase", "review", "test"):
        assert forbidden in lowered, source
    assert "codex({" in lowered
    for option in ("mode", "workdir", "contract"):
        assert f'"{option}"' in lowered, source


def test_workflow_tool_description_exposes_the_complete_codex_contract() -> None:
    _assert_codex_contract(_DESCRIPTION, "workflow tool description")


@pytest.mark.parametrize("document", MAINTAINED_DOCUMENTS, ids=lambda path: path.name)
def test_project_documentation_exposes_the_same_codex_contract(document: Path) -> None:
    _assert_codex_contract(_contract_text(document), document.name)
