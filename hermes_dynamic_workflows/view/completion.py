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


def _bounded_card_text(value: Any, max_chars: int) -> str:
    text = _sanitize_text(" ".join(str(value or "").split()))
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _bounded_result_text(value: Any, max_chars: int) -> str:
    text = _sanitize_text(str(value or "").strip())
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


def _fit_utf16(text: str, max_units: int = 4096) -> str:
    if len(text.encode("utf-16-le")) // 2 <= max_units:
        return text
    suffix = "\n…"
    budget = max_units - (len(suffix.encode("utf-16-le")) // 2)
    used = 0
    chars: list[str] = []
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if used + units > budget:
            break
        chars.append(char)
        used += units
    return "".join(chars).rstrip() + suffix


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
            details_title, findings = "Details", details
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


def render_completion_card(
    record: dict[str, Any],
    *,
    preview_chars: int,
    show_cost: bool,
) -> str:
    card = _build_completion_card(record, preview_chars)
    lines = [f"{_completion_icon(card.status)} {card.title}"]
    if card.summary:
        lines.extend(["", card.summary])
    if card.findings:
        lines.extend(["", card.details_title or "Findings"])
        lines.extend(f"• {finding}" for finding in card.findings)
    if card.next_action:
        lines.extend(["", "Required action", card.next_action])
    if card.fallback:
        lines.extend(["", card.fallback])
    metrics = render_run_metrics(record, show_cost=show_cost)
    if metrics:
        lines.extend(["", metrics])
    base = _sanitize_text("\n".join(lines).rstrip())
    if show_cost:
        remaining = 4096 - (len(base.encode("utf-16-le")) // 2) - 2
        breakdown = render_cost_breakdown(record, char_budget=max(0, remaining))
        if breakdown:
            candidate = _sanitize_text(f"{base}\n\n{breakdown}")
            if len(candidate.encode("utf-16-le")) // 2 <= 4096:
                return candidate
    return _fit_utf16(base)
