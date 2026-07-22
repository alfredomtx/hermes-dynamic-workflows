from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .render import render_cost_breakdown, render_run_metrics


@dataclass(frozen=True)
class _CompletionCard:
    status: str
    title: str
    summary: str = ""
    details_title: str = ""
    findings: tuple[str, ...] = ()
    next_action: str = ""
    fallback: str = ""
    result_rows: tuple[_ResultRow, ...] | None = None


@dataclass(frozen=True)
class _ResultRow:
    label: str
    status: str | None
    heading: str
    summary: str = ""
    findings: tuple[str, ...] = ()
    next_action: str = ""
    missing: bool = False


def content_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def is_intentional_stop_record(record: dict[str, Any]) -> bool:
    """True for a user-requested workflow stop, not an execution failure."""
    if str(record.get("status") or "").lower() != "stopped":
        return False
    error = str(record.get("error") or "").strip()
    if not error:
        return True
    return error.splitlines()[0].startswith("WorkflowStopped:")


def _sanitize_text(text: str) -> str:
    text = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in text)
    return re.sub(r"`{3,}", lambda match: "\u200b".join(match.group()), text)


_MARKDOWN_CONTENT_RE = re.compile(r"([\\`*_\[\]~|])")


def _escape_markdown_content(text: str) -> str:
    sanitized = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))
    return _MARKDOWN_CONTENT_RE.sub(r"\\\1", sanitized)


_FALLBACK_MARKDOWN_CONTENT_RE = re.compile(r"([\\`*~|])")
_FALLBACK_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(")


def _escape_fallback_content(text: str) -> str:
    """Escape raw fallback syntax while keeping ordinary JSON punctuation visible."""
    sanitized = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))
    escaped = _FALLBACK_MARKDOWN_CONTENT_RE.sub(r"\\\1", sanitized)
    escaped = _FALLBACK_LINK_RE.sub(
        lambda match: f"\\[{match.group(1)}\\](",
        escaped,
    )
    return re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", escaped)


def _inline_code_content(text: str) -> str:
    sanitized = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))
    return sanitized.replace("\\", "\\\\").replace("`", "\\`")


def _bounded_card_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _bounded_result_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    remaining = len(text) - max_chars
    return text[:max_chars].rstrip() + f"\n… ({remaining} chars omitted)"


def _recognized_outcome(value: Any) -> str | None:
    outcome = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if outcome in {"block", "blocked", "blocker", "blocking"}:
        return "blocked"
    if outcome in {"fail", "failed", "failure", "error"}:
        return "failed"
    if outcome in {"warn", "warning", "partial", "interrupted", "needs_attention"}:
        return "warning"
    if outcome in {"pass", "passed", "ok"}:
        return "passed"
    if outcome in {"success", "succeeded", "completed", "done"}:
        return "completed"
    if outcome in {"cancel", "canceled", "cancelled", "stop", "stopped"}:
        return "stopped"
    return None


def _transport_outcome(record: dict[str, Any]) -> str:
    value = record.get("status")
    if value is None or not str(value).strip():
        return "completed"
    return _recognized_outcome(value) or "warning"


def _cautious_outcome(status: str | None) -> str:
    if status in {"blocked", "failed", "warning", "stopped"}:
        return status
    return "warning"


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit_utf16(text: str, max_units: int = 4096) -> str:
    if max_units <= 0:
        return ""
    if _utf16_units(text) <= max_units:
        return text
    if max_units == 1:
        return "…"
    suffix = "\n…"
    budget = max_units - _utf16_units(suffix)
    used = 0
    chars: list[str] = []
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        chars.append(char)
        used += units
    return "".join(chars).rstrip() + suffix


def _truncate_utf16_text(text: str, max_units: int) -> str:
    if max_units <= 0:
        return ""
    if _utf16_units(text) <= max_units:
        return text
    if max_units == 1:
        return "…"
    budget = max_units - 1
    used = 0
    chars: list[str] = []
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        chars.append(char)
        used += units
    return "".join(chars).rstrip() + "…"


def _workflow_card_name(record: dict[str, Any]) -> str:
    workflow = record.get("workflow") or {}
    meta = workflow.get("meta") or {}
    raw = meta.get("name") or record.get("source", {}).get("ref") or "workflow"
    words = " ".join(str(raw).replace("_", " ").replace("-", " ").split())
    return _bounded_card_text(words.capitalize() or "Workflow", 72)


def _finding_source_text(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("title")
            or value.get("summary")
            or value.get("details")
            or value.get("message")
            or ""
        )
    return " ".join(str(value or "").split())


def _finding_text(value: Any) -> str:
    text = _finding_source_text(value)
    if text.startswith("["):
        closing = text.find("]", 1, 80)
        if closing >= 0:
            text = text[closing + 1 :].strip()
    return _bounded_card_text(text, 240)


def _is_explicit_blocker(value: Any) -> bool:
    text = _finding_source_text(value).lower()
    if "non-blocking" in text or "nonblocking" in text or re.search(
        r"\b(?:no|not|never|isn't|isnt|doesn't|doesnt|does not|do not|won't|wont|cannot|can't|cant)\b.{0,32}\bblock",
        text,
    ):
        return False
    if isinstance(value, dict):
        markers = (value.get("severity"), value.get("status"), value.get("verdict"))
        return any(_recognized_outcome(marker) == "blocked" for marker in markers if marker is not None)
    return any(
        marker in text
        for marker in (
            "[important, blocking]",
            "[blocker]",
            "remains a release blocker",
            "is a release blocker",
        )
    )


def _render_finding_rows(values: list[Any]) -> tuple[str, ...]:
    rendered: list[str] = []
    for value in values:
        text = _finding_text(value)
        if text and text not in rendered:
            rendered.append(text)
    return tuple(rendered[:4])


def _result_items(result: Any) -> list[Any]:
    if isinstance(result, dict) and isinstance(result.get("results"), list):
        return list(result["results"])
    if isinstance(result, list):
        return list(result)
    return [result]


def _result_rows(result: Any) -> tuple[_ResultRow, ...]:
    rows: list[_ResultRow] = []
    for index, value in enumerate(_result_items(result), start=1):
        fallback_label = f"Result {index}"
        if value is None:
            rows.append(_ResultRow(fallback_label, None, "No result returned", missing=True))
            continue
        if isinstance(value, str):
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            heading = _bounded_card_text(lines[0] if lines else "No result returned", 120)
            summary = _bounded_result_text("\n".join(lines[1:]), 480) if len(lines) > 1 else ""
            rows.append(_ResultRow(fallback_label, None, heading, summary))
            continue
        if isinstance(value, dict):
            label = _bounded_card_text(
                value.get("label") or value.get("title") or value.get("name") or fallback_label,
                96,
            )
            status = None
            for key in ("status", "verdict", "outcome"):
                if value.get(key) is None:
                    continue
                recognized = _recognized_outcome(value[key])
                if recognized is not None:
                    status = recognized
                    break
            heading = _bounded_card_text(
                value.get("title") or value.get("summary") or value.get("message") or label,
                120,
            )
            summary = _bounded_card_text(value.get("summary") or value.get("message"), 480)
            findings_value = value.get("findings") if isinstance(value.get("findings"), list) else []
            rows.append(
                _ResultRow(
                    label,
                    status,
                    heading,
                    summary if summary != heading else "",
                    _render_finding_rows(findings_value),
                    _bounded_card_text(value.get("nextAction") or value.get("next_action"), 400),
                )
            )
            continue
        rows.append(_ResultRow(fallback_label, None, _bounded_card_text(value, 120)))
    return tuple(rows)


def _result_row_marker(row: _ResultRow) -> str:
    if row.missing or row.status == "warning":
        return "⚠️"
    if row.status in {"blocked", "failed"}:
        return "❌"
    if row.status in {"passed", "completed"}:
        return "✅"
    if row.status == "stopped":
        return "⏹"
    heading = str(row.heading).lstrip()
    if re.match(r"^(?:PASS|OK)\s*(?:—|:)\s*", heading, re.IGNORECASE):
        return "✅"
    if re.match(r"^(?:WARN|WARNING)\s*(?:—|:)\s*", heading, re.IGNORECASE):
        return "⚠️"
    if re.match(r"^(?:FAIL|BLOCK|BLOCKED)\s*(?:—|:)\s*", heading, re.IGNORECASE):
        return "❌"
    return "•"


def _result_index(rows: tuple[_ResultRow, ...], *, max_units: int) -> tuple[str, int]:
    if max_units <= 0 or not rows:
        return "", 0
    width = max(2, len(str(max(1, len(rows)))))
    index_lines = [
        f"{str(index).zfill(width)}  {_result_row_marker(row)}  "
        f"{_escape_markdown_content(row.heading)}"
        for index, row in enumerate(rows, start=1)
    ]

    def candidate(visible_count: int) -> str:
        fence = "```\n" + "\n".join(index_lines[:visible_count]) + "\n```"
        hidden = len(rows) - visible_count
        if hidden:
            return f"{fence}\n\n*… {hidden} more results in stored report*"
        return fence

    for visible_count in range(len(rows), -1, -1):
        rendered = candidate(visible_count)
        if _utf16_units(rendered) <= max_units:
            return rendered, visible_count
    return "", 0


def _join_result_blocks(blocks: list[list[str]]) -> str:
    return "\n\n".join("\n".join(block) for block in blocks if block)


def _result_heading_line(ordinal: int, row: _ResultRow) -> str:
    return (
        f"**{_result_row_marker(row)} {ordinal} · "
        f"{_escape_markdown_content(row.heading)}**"
    )


def _result_summary_line(row: _ResultRow) -> str | None:
    if row.missing or not row.summary:
        return None
    return f"*{_escape_markdown_content(row.summary)}*"


def _result_action_line(action: str) -> str:
    if "\n" not in action and "`" not in action:
        return f"`{_inline_code_content(action)}`"
    return _escape_markdown_content(action)


def _result_extra_sections(row: _ResultRow) -> list[list[str]]:
    sections: list[list[str]] = []
    if row.findings:
        sections.append([
            "**Findings**",
            *(f"• {_escape_markdown_content(finding)}" for finding in row.findings),
        ])
    if row.next_action:
        sections.append(["**Required action**", _result_action_line(row.next_action)])
    return sections


def _result_rows_need_attention(rows: tuple[_ResultRow, ...]) -> bool:
    return any(
        row.missing or row.status in {"blocked", "failed", "warning"}
        for row in rows
    )


def _result_card_status(
    transport: str,
    rows: tuple[_ResultRow, ...],
    status: str | None = None,
) -> str:
    card_status = status or transport
    if (
        transport == "completed"
        and card_status in {"completed", "passed"}
        and _result_rows_need_attention(rows)
    ):
        return "warning"
    return card_status


def _render_result_rows(rows: tuple[_ResultRow, ...], *, max_units: int) -> str:
    total = len(rows)
    missing = sum(row.missing for row in rows)
    result_count = total - missing
    aggregate = f"{total} subtasks · {result_count} results · {missing} missing"
    aggregate_block = [f"*{_escape_markdown_content(aggregate)}*"]
    blocks: list[list[str]] = [aggregate_block]
    used_without_details = _utf16_units(_join_result_blocks(blocks))

    index_budget = max(0, max_units - used_without_details - 2)
    index_text, visible_count = _result_index(rows, max_units=index_budget)
    if index_text:
        blocks.append([index_text])

    visible_rows = list(enumerate(rows[:visible_count], start=1))
    exceptions = [
        item for item in visible_rows
        if item[1].missing or item[1].status in {"blocked", "failed", "warning"}
    ]
    ordinary = [item for item in visible_rows if item not in exceptions]
    ordered_rows = exceptions + ordinary

    mandatory: dict[int, list[str]] = {}
    for ordinal, row in ordered_rows:
        row_block = [_result_heading_line(ordinal, row)]
        summary = _result_summary_line(row)
        if summary:
            row_block.append(summary)
        candidate = blocks + [*mandatory.values(), row_block]
        if _utf16_units(_join_result_blocks(candidate)) <= max_units:
            mandatory[ordinal] = row_block

    extras: dict[int, list[str]] = {ordinal: [] for ordinal in mandatory}

    def rendered_detail_blocks() -> list[list[str]]:
        return [
            [*mandatory[ordinal], *extras[ordinal]]
            for ordinal, _row in ordered_rows
            if ordinal in mandatory
        ]

    for ordinal, row in ordered_rows:
        if ordinal not in mandatory:
            continue
        for section in _result_extra_sections(row):
            section_lines: list[str] = []
            for line in section:
                trial_extras = {key: list(value) for key, value in extras.items()}
                trial_extras[ordinal].extend(section_lines)
                trial_extras[ordinal].append(line)
                trial_blocks = [
                    [*mandatory[key], *trial_extras[key]]
                    for key, _row in ordered_rows
                    if key in mandatory
                ]
                if _utf16_units(_join_result_blocks(blocks + trial_blocks)) > max_units:
                    break
                section_lines.append(line)
            if len(section_lines) > 1:
                extras[ordinal].extend(section_lines)

    result = _join_result_blocks(blocks + rendered_detail_blocks())
    return _fit_utf16(result, max_units)


def _classify_findings(values: list[Any]) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    blockers = [value for value in values if _is_explicit_blocker(value)]
    return len(blockers), _render_finding_rows(blockers), _render_finding_rows(values)


def _outcome_values(mapping: dict[str, Any]) -> list[Any]:
    return [mapping[key] for key in ("status", "verdict", "outcome") if mapping.get(key) is not None]


def _resolve_outcomes(
    result: dict[str, Any],
    presentation: dict[str, Any] | None,
    reviews: list[dict[str, Any]],
) -> tuple[str | None, bool]:
    values = _outcome_values(result)
    if presentation is not None:
        values.extend(_outcome_values(presentation))
    for review in reviews:
        values.extend(_outcome_values(review))
    outcomes: list[str] = []
    has_unknown = False
    for value in values:
        outcome = _recognized_outcome(value)
        if outcome is None:
            has_unknown = True
        else:
            outcomes.append(outcome)
    for status in ("blocked", "failed", "warning", "passed", "completed", "stopped"):
        if status in outcomes:
            return status, has_unknown
    return None, has_unknown


def _validated_reviews(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    if "reviews" not in result or result.get("reviews") is None:
        return []
    value = result.get("reviews")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _review_findings(reviews: list[dict[str, Any]]) -> list[Any] | None:
    values: list[Any] = []
    for review in reviews:
        findings = review.get("findings")
        if findings is None:
            findings = []
        if not isinstance(findings, list):
            return None
        values.extend(findings)
    return values


def _completion_title(status: str, *, is_review: bool, workflow_name: str) -> str:
    noun = "Review" if is_review else workflow_name
    suffix = {
        "blocked": "blocked",
        "failed": "failed",
        "warning": "needs attention",
        "passed": "passed",
        "stopped": "stopped",
    }.get(status, "completed")
    return f"{noun} {suffix}"


def _completion_result_block(record: dict[str, Any], preview_chars: int) -> str:
    result = record.get("result")
    if result is None:
        return ""
    text = _sanitize_text(content_from_value(result).strip())
    if not text:
        return ""
    if preview_chars > 0 and len(text) > preview_chars:
        remaining = len(text) - preview_chars
        text = text[:preview_chars] + f"\n... (truncated {remaining} chars)"
    return f"Result:\n{text}"


def _raw_completion_card(
    record: dict[str, Any],
    workflow_name: str,
    status: str,
    preview_chars: int,
) -> _CompletionCard:
    return _CompletionCard(
        status=status,
        title=_completion_title(status, is_review=False, workflow_name=workflow_name),
        fallback=_completion_result_block(record, min(max(preview_chars, 240), 1800)),
    )


def _build_completion_card(record: dict[str, Any], preview_chars: int) -> _CompletionCard:
    workflow_name = _workflow_card_name(record)
    transport = _transport_outcome(record)
    result = record.get("result")
    if is_intentional_stop_record(record):
        return _CompletionCard(status="stopped", title="Workflow stopped", summary="Stopped intentionally.")
    if record.get("error"):
        return _CompletionCard(
            status="failed",
            title="Workflow failed",
            summary=_bounded_card_text(record.get("error"), min(max(preview_chars, 240), 900)),
        )
    if transport in {"failed", "stopped", "warning"}:
        summary = ""
        if isinstance(result, str):
            summary = _bounded_result_text(result, min(max(preview_chars, 240), 1200))
        elif isinstance(result, dict) and isinstance(result.get("presentation"), dict):
            summary = _bounded_card_text(result["presentation"].get("summary"), 400)
        card = _raw_completion_card(record, workflow_name, transport, preview_chars)
        return _CompletionCard(
            status=transport,
            title=card.title,
            summary=summary,
            fallback="" if summary else card.fallback,
        )
    if isinstance(result, str):
        return _CompletionCard(
            status=transport,
            title=_completion_title(transport, is_review=False, workflow_name=workflow_name),
            summary=_bounded_result_text(result, min(max(preview_chars, 240), 1200)),
        )
    if isinstance(result, list):
        rows = _result_rows(result)
        final_status = _result_card_status(transport, rows)
        return _CompletionCard(
            status=final_status,
            title=_completion_title(final_status, is_review=False, workflow_name=workflow_name),
            result_rows=rows,
        )
    if not isinstance(result, dict):
        return _raw_completion_card(record, workflow_name, transport, preview_chars)

    explicit_value = result.get("presentation")
    if explicit_value is not None and not isinstance(explicit_value, dict):
        return _raw_completion_card(record, workflow_name, "warning", preview_chars)
    explicit = explicit_value if isinstance(explicit_value, dict) else None
    reviews = _validated_reviews(result)
    if reviews is None:
        status, _ = _resolve_outcomes(result, explicit, [])
        return _raw_completion_card(
            record,
            workflow_name,
            _cautious_outcome(status),
            preview_chars,
        )
    status, has_unknown = _resolve_outcomes(result, explicit, reviews)
    if has_unknown:
        return _raw_completion_card(record, workflow_name, _cautious_outcome(status), preview_chars)

    if explicit is not None:
        findings_value = explicit.get("findings")
        if findings_value is None:
            findings_value = []
        if not isinstance(findings_value, list):
            return _raw_completion_card(record, workflow_name, _cautious_outcome(status), preview_chars)
        final_status = status or transport
        blocker_count, blockers, details = _classify_findings(findings_value)
        if final_status == "blocked" and blocker_count:
            details_title, findings = "Blocking findings", blockers
        elif findings_value:
            details_title, findings = "Findings", details
        else:
            details_title, findings = "", ()
        title = _bounded_card_text(explicit.get("title"), 96) or _completion_title(
            final_status,
            is_review="review" in workflow_name.lower(),
            workflow_name=workflow_name,
        )
        return _CompletionCard(
            status=final_status,
            title=title,
            summary=_bounded_card_text(explicit.get("summary"), 400),
            details_title=details_title,
            findings=findings,
            next_action=_bounded_card_text(
                explicit.get("nextAction") or explicit.get("next_action"),
                400,
            ),
        )

    summary = _bounded_card_text(result.get("summary") or result.get("message"), 400)
    if reviews:
        review_findings = _review_findings(reviews)
        if review_findings is None or status is None:
            return _raw_completion_card(record, workflow_name, _cautious_outcome(status), preview_chars)
        if status == "blocked":
            blocker_count, blockers, details = _classify_findings(review_findings)
            reviewer_noun = "reviewer" if len(reviews) == 1 else "reviewers"
            if blocker_count:
                finding_noun = "finding" if blocker_count == 1 else "findings"
                prefix = (
                    f"{blocker_count} blocking {finding_noun} "
                    f"from {len(reviews)} {reviewer_noun}."
                )
                details_title, findings = "Blocking findings", blockers
            else:
                prefix = f"{len(reviews)} {reviewer_noun} returned BLOCK."
                details_title, findings = ("Review details", details) if details else ("", ())
            summary = f"{prefix} {summary}".strip()
        else:
            if not summary:
                reviewer_noun = "reviewer" if len(reviews) == 1 else "reviewers"
                verb = "passed" if status in {"passed", "completed"} else "needs attention"
                summary = f"{len(reviews)} {reviewer_noun}: {verb}."
            details_title = "Findings" if review_findings else ""
            findings = _render_finding_rows(review_findings)
        return _CompletionCard(
            status=status,
            title=_completion_title(status, is_review=True, workflow_name=workflow_name),
            summary=summary,
            details_title=details_title,
            findings=findings,
        )

    if isinstance(result.get("results"), list):
        rows = _result_rows(result)
        final_status = _result_card_status(transport, rows, status)
        return _CompletionCard(
            status=final_status,
            title=_completion_title(final_status, is_review=False, workflow_name=workflow_name),
            result_rows=rows,
        )

    top_findings = result.get("findings")
    if top_findings is None:
        top_findings = []
    if not isinstance(top_findings, list):
        return _raw_completion_card(record, workflow_name, _cautious_outcome(status), preview_chars)
    if status is not None or summary or top_findings:
        final_status = status or transport
        blocker_count, blockers, details = _classify_findings(top_findings)
        if final_status == "blocked" and blocker_count:
            finding_noun = "finding" if blocker_count == 1 else "findings"
            summary = f"{blocker_count} blocking {finding_noun}. {summary}".strip()
            details_title, findings = "Blocking findings", blockers
        elif top_findings:
            details_title, findings = "Details", details
        else:
            details_title, findings = "", ()
        return _CompletionCard(
            status=final_status,
            title=_completion_title(final_status, is_review=False, workflow_name=workflow_name),
            summary=summary,
            details_title=details_title,
            findings=findings,
        )
    return _raw_completion_card(record, workflow_name, transport, preview_chars)


def _completion_icon(status: str) -> str:
    return {
        "blocked": "⛔",
        "failed": "❌",
        "warning": "⚠️",
        "stopped": "⏹",
        "passed": "✅",
        "completed": "✅",
    }.get(status, "✅")


_TELEGRAM_MARKDOWN_SPECIALS = frozenset("_*[]()~`>#+-=|{}.!\\\\")
_CARD_MAX_UNITS = 4096


def _card_formatter_units(text: str) -> int:
    """Conservatively budget the installed Telegram MarkdownV2 conversion.

    The workflow plugin emits standard Markdown, while the Telegram adapter
    escapes MarkdownV2 controls (including the backslashes this renderer uses
    to protect model content).  Counting one possible adapter escape for every
    MarkdownV2 control keeps the source and the final formatted message within
    Telegram's UTF-16 limit without importing a gateway adapter into the
    plugin.
    """
    return _utf16_units(text) + sum(
        1 for char in text if char in _TELEGRAM_MARKDOWN_SPECIALS
    )


def _card_blocks_text(blocks: list[str]) -> str:
    return "\n\n".join(block for block in blocks if block).rstrip()


def _card_blocks_fit(blocks: list[str]) -> bool:
    text = _card_blocks_text(blocks)
    return (
        _utf16_units(text) <= _CARD_MAX_UNITS
        and _card_formatter_units(text) <= _CARD_MAX_UNITS
    )


def _fit_italic_card_block(
    value: Any,
    fixed_blocks: list[str],
    *,
    max_chars: int,
    preserve_result_overflow: bool = False,
) -> str:
    normalized = (
        " ".join(_bounded_result_text(value, max_chars).split())
        if preserve_result_overflow
        else _bounded_card_text(value, max_chars)
    )
    if not normalized:
        return ""

    def candidate(limit: int) -> str:
        content = normalized if len(normalized) <= limit else _truncate_utf16_text(normalized, limit)
        return f"*{_escape_markdown_content(content)}*"

    if _card_blocks_fit([*fixed_blocks, candidate(len(normalized))]):
        return candidate(len(normalized))

    low, high = 1, len(normalized)
    best = ""
    while low <= high:
        limit = (low + high) // 2
        rendered = candidate(limit)
        if _card_blocks_fit([*fixed_blocks, rendered]):
            best = rendered
            low = limit + 1
        else:
            high = limit - 1
    return best


def _fit_escaped_card_block(
    value: Any,
    fixed_blocks: list[str],
    *,
    max_chars: int,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""

    def candidate(limit: int) -> str:
        content = normalized if len(normalized) <= limit else _truncate_utf16_text(normalized, limit)
        return _escape_fallback_content(content)

    limit = min(max_chars, len(normalized))
    if limit <= 0:
        return ""
    rendered = candidate(limit)
    if _card_blocks_fit([*fixed_blocks, rendered]):
        return rendered

    low, high = 1, limit
    best = ""
    while low <= high:
        current = (low + high) // 2
        rendered = candidate(current)
        if _card_blocks_fit([*fixed_blocks, rendered]):
            best = rendered
            low = current + 1
        else:
            high = current - 1
    return best


def _render_non_result_card(card: _CompletionCard, metrics: str) -> str:
    title = _bounded_card_text(card.title, 96)
    title_block = f"**{_completion_icon(card.status)} {_escape_markdown_content(title)}**"

    metric_block = _fit_italic_card_block(metrics, [title_block], max_chars=1200) if metrics else ""
    suffix = [metric_block] if metric_block else []
    selected = [title_block]

    if card.summary:
        summary = _fit_italic_card_block(
            card.summary,
            [*selected, *suffix],
            max_chars=1200,
            preserve_result_overflow=True,
        )
        if summary:
            selected.append(summary)

    findings = [
        _bounded_card_text(value, 240)
        for value in tuple(card.findings or ())[:4]
    ]
    findings = [value for value in findings if value]
    if findings:
        details_title = _bounded_card_text(card.details_title or "Findings", 96)
        for count in range(len(findings), 0, -1):
            findings_block = "\n".join([
                f"**{_escape_markdown_content(details_title)}**",
                *(f"• {_escape_markdown_content(value)}" for value in findings[:count]),
            ])
            if _card_blocks_fit([*selected, findings_block, *suffix]):
                selected.append(findings_block)
                break

    if card.next_action:
        action = _bounded_card_text(card.next_action, 400)
        if action:
            action_block = "\n".join([
                "**Required action**",
                _result_action_line(action),
            ])
            if _card_blocks_fit([*selected, action_block, *suffix]):
                selected.append(action_block)
            else:
                for limit in range(len(action) - 1, 0, -1):
                    shortened = _truncate_utf16_text(action, limit)
                    action_block = "\n".join([
                        "**Required action**",
                        _result_action_line(shortened),
                    ])
                    if _card_blocks_fit([*selected, action_block, *suffix]):
                        selected.append(action_block)
                        break

    if card.fallback:
        fallback = _fit_escaped_card_block(
            card.fallback,
            [*selected, *suffix],
            max_chars=1800,
        )
        if fallback:
            selected.append(fallback)

    base_blocks = [*selected, *suffix]
    if not _card_blocks_fit(base_blocks):
        # The title and the generated metric block are complete trusted blocks;
        # never repair an over-budget card by slicing through their delimiters.
        base_blocks = [title_block, *suffix]
    return _card_blocks_text(base_blocks)


def render_completion_card(
    record: dict[str, Any],
    *,
    preview_chars: int,
    show_cost: bool,
) -> str:
    card = _build_completion_card(record, preview_chars)
    lines = [
        f"**{_completion_icon(card.status)} "
        f"{_escape_markdown_content(card.title)}**"
    ]
    if card.summary:
        lines.extend(["", f"*{_escape_markdown_content(card.summary)}*"])
    if card.findings:
        lines.extend(["", f"**{_escape_markdown_content(card.details_title or 'Findings')}**"])
        lines.extend(f"• {_escape_markdown_content(finding)}" for finding in card.findings)
    if card.next_action:
        lines.extend([
            "",
            "**Required action**",
            _result_action_line(card.next_action),
        ])
    metrics = render_run_metrics(record, show_cost=show_cost)
    if card.result_rows is not None:
        if card.fallback:
            lines.extend(["", _escape_fallback_content(card.fallback)])
        metrics_text = f"*{_escape_markdown_content(metrics)}*" if metrics else ""
        prefix = "\n".join(lines).rstrip()
        fixed_units = _utf16_units(prefix) + 2
        if metrics_text:
            fixed_units += 2 + _utf16_units(metrics_text)
        result_budget = max(0, 4096 - fixed_units)
        result_text = _render_result_rows(card.result_rows, max_units=result_budget)
        base = prefix
        if result_text:
            base = f"{base}\n\n{result_text}"
        if metrics_text:
            base = f"{base}\n\n{metrics_text}"
        base = base.rstrip()
        if show_cost:
            remaining = max(0, 4096 - _utf16_units(base) - 2)
            breakdown = render_cost_breakdown(record, char_budget=remaining)
            if breakdown:
                escaped_breakdown = _escape_markdown_content(breakdown)
                candidate = f"{base}\n\n{escaped_breakdown}"
                if _utf16_units(candidate) <= 4096:
                    return candidate
        return _fit_utf16(base)

    base = _render_non_result_card(card, metrics)
    if show_cost:
        remaining = max(0, _CARD_MAX_UNITS - _card_formatter_units(base) - 2)
        breakdown = render_cost_breakdown(record, char_budget=remaining)
        if breakdown:
            escaped_breakdown = _escape_markdown_content(breakdown)
            candidate = f"{base}\n\n{escaped_breakdown}"
            if _card_blocks_fit([base, escaped_breakdown]):
                return candidate
    return base
