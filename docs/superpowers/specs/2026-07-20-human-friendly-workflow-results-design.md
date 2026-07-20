# Human-Friendly Workflow Results

## Goal

Make Telegram workflow completion messages readable for any workflow task without relying on domain-specific assumptions. Preserve every top-level subtask result, keep complete machine-readable output available outside the card, and remove the terminal Rerun button.

Also prevent clarification responses from referring to choices that appeared only in transient progress commentary.

## Scope

### Included

- Generic completion rendering for scalar, list, dictionary, nested `results`, and null values.
- Explicit presentation-envelope rendering.
- A visible row for every top-level subtask result while the Telegram card fits.
- An explicit overflow count linked conceptually to the persisted report when headings alone exceed the card limit.
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

Normal generic completion shape:

```text
⚠️ Workflow completed with missing output

3 subtasks · 2 results · 1 missing

1. ✅ PASS · zero blockers
   Validated live frontend behavioral skill.

2. ⚠️ No result returned

3. ❌ FAIL · 3 blockers
   Canonical source differs.
   Frontend references remain unresolved.

7m 31s · 3 agents · ~$1.10 · 2.18M tokens
```

Rules:

- Show every top-level subtask while the card fits.
- Preserve original result order.
- Keep headings scan-friendly and summaries bounded.
- Show explicit findings and required action when present.
- Render metrics once at the bottom.
- Do not emit raw JSON punctuation as the normal user-facing representation.
- Do not repeat a separate cost section when the metrics line already carries cost unless per-subtask cost data is intentionally rendered and fits the character budget.

## Length Budget

Telegram counts UTF-16 code units and limits text messages to 4096 units. Completion rendering must fit that limit without dropping an entire subtask.

Budget policy:

1. Reserve space for title, aggregate counts, as many ordered subtask headings as fit, an overflow marker, and metrics.
2. Allocate remaining space across visible subtask summaries.
3. Truncate verbose details before headings.
4. If one subtask exceeds its allocation, append a clear truncation marker.
5. If all headings fit, retain every heading and replace omitted detail with a per-row truncation marker.
6. If headings alone cannot fit, retain the largest ordered prefix that fits and append `… N more results in stored report`.
7. Apply a final UTF-16 fit guard as defense in depth.

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
- every subtask retained in original order when headings fit;
- explicit overflow count when headings alone exceed the card limit;
- explicit labels and ordinal fallback labels;
- valid presentation envelope remains authoritative;
- malformed presentation falls back safely;
- quoted or negated verdict words do not alter task outcome;
- raw JSON syntax absent from normal cards;
- metrics rendered exactly once;
- long inputs remain within 4096 UTF-16 units;
- truncation removes details before subtask headings.

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
- Every top-level subtask is visible when headings fit; oversized result sets show an explicit overflow count and remain complete in the stored report.
- Missing values are shown honestly without invented failure semantics.
- Explicit presentation envelopes remain authoritative.
- Full machine-readable output remains persisted.
- Telegram card contains one metrics line and no default raw JSON dump.
- Terminal workflow cards contain no Rerun button.
- Active workflow controls continue working.
- Final clarification messages never depend on transient commentary.
- Focused tests and live Telegram canaries pass.
