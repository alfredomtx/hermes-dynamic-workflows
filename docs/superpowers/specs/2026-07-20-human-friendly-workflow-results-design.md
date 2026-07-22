# Human-Friendly Workflow Results

## Goal

Make Telegram workflow completion messages readable for any workflow task without relying on domain-specific assumptions. Preserve every top-level subtask result, keep complete machine-readable output available outside the card, and remove the terminal Rerun button.

Also prevent clarification responses from referring to choices that appeared only in transient progress commentary.

## Scope

### Included

- Generic completion rendering for scalar, list, dictionary, nested `results`, and null values.
- Explicit presentation-envelope rendering.
- A visible row for every top-level subtask result while the Telegram card fits.
- An explicit overflow count linked conceptually to the persisted report when compact index rows exceed the card limit.
- Bounded summaries that respect Telegram's UTF-16 4096-character limit.
- One metrics line for duration, agent count, estimated cost, and tokens.
- Removal of terminal workflow Rerun buttons.
- Regression coverage for self-contained final clarification responses.

### Excluded

- Skill-specific, validation-specific, review-specific, or deployment-specific inference.
- Parsing arbitrary prose to determine workflow success or failure.
- Removing active Pause, Resume, Stop, or Restart controls.
- Deleting stored workflow output or changing workflow execution semantics.
- Adding an expandable Telegram details interface.

## Result Presentation Contract

Workflow authors may return an explicit presentation envelope:

```json
{
  "presentation": {
    "status": "warning",
    "title": "Validation needs attention",
    "summary": "Two checks passed; one failed.",
    "findings": ["Canonical source differs."],
    "nextAction": "Repair the source copy."
  },
  "report": {
    "results": []
  }
}
```

When valid, `presentation` is authoritative for the primary completion card. `report` remains machine-readable data and is not dumped into the normal Telegram message.

Malformed presentation data falls back to cautious generic rendering. It must not crash completion delivery or silently omit the workflow result.

## Generic Fallback

When no valid presentation envelope exists, render the result structurally rather than dumping serialized JSON.

### Result types

- **String:** use the first meaningful line as the row heading and a bounded remainder as its summary.
- **Dictionary:** prefer explicit generic fields such as `title`, `status`, `summary`, `message`, `findings`, `nextAction`, and `next_action`. Unknown fields remain in stored output and are not dumped by default.
- **List:** render every top-level item in original order.
- **Nested `results` list:** treat each item as a top-level subtask result and render every item in original order.
- **Null:** render `No result returned`. Do not label it failed, skipped, or cancelled without explicit engine data.
- **Other scalar values:** render their bounded textual representation.

### Labels

Use a supplied item label, title, or name when available. Otherwise assign stable ordinal labels such as `Result 1`, `Result 2`, and `Result 3`.

### Outcome handling

Keep transport status separate from task outcome:

1. **Transport status:** completed, failed, stopped, interrupted, or another engine-owned state.
2. **Task outcome:** passed, warning, blocked, failed, or unknown when supplied through structured result fields.

The renderer may display verdict words already present in plain-string headings, such as `PASS — zero blockers`, but must not promote the overall task outcome by parsing arbitrary prose. A quoted `FAIL`, a negated blocker statement, or diagnostic sample must not change workflow status.

## Telegram Card Layout

Use a hybrid summary-index plus detail layout. Telegram has no reliable native Markdown-table rendering, so the compact index uses a monospace block for stable narrow columns. Rich result sections remain outside the code block so bold, italic, inline-code, and bullet formatting still render.

Normal generic completion shape:

````text
**⚠️ Workflow needs attention**
_3 subtasks · 2 results · 1 missing_

```
01  ✅  First task
02  ⚠️  No result returned
03  ❌  Third task
```

**✅ 1 · First task**
_Evidence retained._

**⚠️ 2 · No result returned**

**❌ 3 · Third task**
_One blocker remains._

**Findings**
• Synthetic canary blocker.

**Required action**
`Confirm this card is readable.`

_7m 31s · 3 agents · ~$1.10 · 2.18M tokens_
````

Rules:

- Render the outcome title and section labels in bold.
- Render aggregate metadata, result summaries, overflow text, and metrics in italics.
- Render required actions, identifiers, paths, and commands as inline code when they fit safely.
- Render a compact monospace index before details; use zero-padded ordinals while the result count is below 100, then width-match the largest ordinal.
- Keep the index narrow: ordinal, status marker, and bounded title only. Do not put summaries, findings, paths, or commands into table columns.
- Every returned result gets a titled detail section and one bounded summary line while the card budget permits.
- Missing results get an index row and detail heading but no invented summary.
- Warning, failed, blocked, and missing details receive budget before successful details. Within each priority class, preserve original result order and retain original ordinal labels.
- Show explicit findings as bullets and required action as its own labeled section.
- Render metrics once at the bottom.
- Do not use pipe tables, HTML tables, or wide columns; Telegram mobile rendering is not stable enough.
- Do not emit raw JSON punctuation as the normal user-facing representation.
- Do not repeat a separate cost section when the metrics line already carries cost unless per-subtask cost data is intentionally rendered and fits the character budget.

## Length Budget

Telegram counts UTF-16 code units and limits text messages to 4096 units. Completion rendering must fit that limit without dropping an entire subtask.

Budget policy:

1. Reserve space for bold title, italic aggregate metadata, compact index, overflow marker, and italic metrics.
2. Keep as many ordered index rows as fit; if all index rows cannot fit, append `_… N more results in stored report_`.
3. Allocate remaining detail budget to warning, failed, blocked, and missing sections first, then successful and unknown sections.
4. Within each priority class, preserve original result order and display the original ordinal in every detail heading.
5. Give each visible returned result one bounded summary line before allocating extra findings or action text.
6. Truncate detail text before index rows, overflow marker, or metrics.
7. Escape or sanitize model-provided Markdown control characters so result content cannot break the renderer's own formatting.
8. Apply a final UTF-16 fit guard as defense in depth.

Full output remains available in the persisted workflow artifact.

## Telegram Controls

- Remove `Rerun` from terminal workflow completion messages.
- Preserve active controls when meaningful:
  - Pause while queued or running.
  - Resume while paused.
  - Stop while queued, running, or paused.
  - Restart while active when currently supported.
- Preserve an Open log URL button when a valid log URL exists.
- Completion edits must clear stale active controls rather than leaving unusable buttons attached.

This change removes only the terminal Rerun affordance. It does not remove engine support for explicit restart or resume commands.

## Self-Contained Final Responses

Required user-facing questions, choices, decisions, and context belong in the final response. Intermediate commentary is progress-only and may be edited, collapsed, or omitted by a messaging gateway.

Response rules:

- Never place required A/B/C choices only in commentary.
- Never send a final response that says `pick A/B/C`, `as above`, or equivalent unless the referenced content appears in the same final response.
- Clarification responses must include the complete question and all options in their final message.
- Intermediate messages may report status but must not carry information required to answer the final message.

Use prompt-level instruction plus deterministic regression fixtures. Do not add a runtime semantic parser for backward references; natural-language reference detection would be brittle and outside the workflow renderer's responsibility.

## Components

### Completion view

Extend the completion view with pure helpers that:

- normalize an arbitrary workflow result into ordered presentation rows;
- preserve explicit presentation-envelope behavior;
- summarize strings and structured values cautiously;
- calculate aggregate returned/missing counts;
- allocate the Telegram character budget across all rows;
- render one metrics line.

Keep normalization separate from string rendering so tests can verify semantic decisions without asserting entire formatted messages.

### Workflow manager

Continue using the existing completion-card entry point. The manager supplies the terminal record, config, and control buttons but does not infer domain meaning.

### Gateway response instruction

Add the self-contained-final requirement at the narrow prompt or response-assembly surface that controls gateway-facing final replies. Keep implementation platform-neutral when possible because transient commentary can affect other gateways too.

## Error Handling

- Invalid presentation envelope: cautious generic fallback.
- Missing result: explicit `No result returned` row.
- Unknown status: neutral marker and unchanged transport status.
- Unsupported value: bounded textual fallback.
- Rendering exception: preserve existing raw fallback only as last-resort delivery protection, bounded to Telegram limits.
- Button-capability mismatch: omit unsupported buttons without breaking completion delivery.

## Verification

### Completion rendering

Test:

- plain string result;
- dictionary result;
- list result;
- nested `results` list;
- mixed strings, dictionaries, and nulls;
- every subtask retained in original order in the compact index when it fits;
- explicit italic overflow count when index rows exceed the card limit;
- compact index uses a fenced monospace block with aligned ordinal and status columns;
- outcome title, result headings, findings label, and required-action label render bold;
- aggregate metadata, summaries, overflow text, and metrics render italic;
- required action renders inline code when safe;
- every returned result receives one summary line while detail budget permits;
- warning, failure, blocked, and missing details receive budget before successful details;
- model-provided Markdown punctuation cannot break card structure;
- explicit labels and ordinal fallback labels;
- valid presentation envelope remains authoritative;
- malformed presentation falls back safely;
- quoted or negated verdict words do not alter task outcome;
- raw JSON syntax absent from normal cards;
- metrics rendered exactly once;
- long inputs remain within 4096 UTF-16 units;
- truncation removes detail sections before compact index rows, overflow marker, or metrics.

### Controls

Test:

- terminal states have no Rerun button;
- running states retain Pause, Stop, and Restart;
- paused state retains Resume, Stop, and Restart;
- terminal edit clears stale active controls;
- valid Open log URL remains available.

### Response delivery discipline

Test representative clarification output so the final message itself contains:

- the question;
- every referenced choice;
- enough context to answer without intermediate commentary.

### Live verification

After implementation and focused tests:

1. Restart the gateway because plugin code is loaded by the gateway process.
2. Run a generic workflow canary that returns mixed strings, a dictionary, and null.
3. Confirm Telegram shows every subtask, one metrics line, no raw JSON dump, and no Rerun button.
4. Confirm one post-change gateway process is serving.
5. Send a clarification canary with choices and confirm the final Telegram message is self-contained.

## Acceptance Criteria

- Arbitrary workflow result structures produce a readable completion card.
- Every top-level subtask is visible when compact index rows fit; oversized result sets show an explicit overflow count and remain complete in the stored report.
- Missing values are shown honestly without invented failure semantics.
- Explicit presentation envelopes remain authoritative.
- Full machine-readable output remains persisted.
- Telegram card uses the approved hybrid hierarchy: bold title/labels, italic metadata/summaries/metrics, compact monospace result index, and rich detail sections.
- Every returned result gets one bounded summary line while detail budget permits; exception details receive priority.
- Telegram card contains one metrics line and no default raw JSON dump.
- Terminal workflow cards contain no Rerun button.
- Active workflow controls continue working.
- Final clarification messages never depend on transient commentary.
- Focused tests and live Telegram canaries pass.
