# Human-Friendly Workflow Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render arbitrary workflow results as compact human-readable Telegram cards, remove terminal Rerun controls, and make final clarification responses self-contained.

**Architecture:** Extend the existing pure completion renderer rather than adding another presentation owner. Normalize arbitrary top-level results into ordered immutable rows, then render those rows under one UTF-16 budget; explicit `presentation` envelopes remain authoritative. Keep workflow control policy in the existing manager helper. Add final-response guidance to Hermes core's stable cached task-completion block so behavior changes universally without per-turn prompt mutation.

**Tech Stack:** Python 3, `unittest`/pytest, Hermes dynamic-workflows plugin, Hermes core prompt assembly, Telegram UTF-16 message limits.

## Global Constraints

- Generic behavior only; no skill-, review-, validation-, deployment-, or domain-specific inference.
- Explicit valid `presentation` envelopes remain authoritative.
- Never infer overall outcome from arbitrary prose.
- Preserve original subtask order.
- Show every subtask heading while the card fits; otherwise show `… N more results in stored report`.
- Keep final Telegram content within 4096 UTF-16 units.
- Preserve complete machine-readable workflow output.
- Render metrics once.
- Remove terminal Rerun only; preserve active Pause, Resume, Stop, and Restart controls plus valid Open log URL.
- Commentary is progress-only; final clarification responses contain the complete question and every referenced option.
- Preserve Hermes system-prompt byte stability during a conversation.
- Follow strict RED → GREEN → REFACTOR; no production edit before its failing test is observed.
- Do not touch the pre-existing untracked plugin `uv.lock`.

---

## File Map and Ownership

### Plugin repository: `/Users/atorres/Documents/GitHub/hermes-dynamic-workflows`

- `hermes_dynamic_workflows/view/completion.py`: sole owner of completion-result normalization, outcome-first card composition, and UTF-16 fitting.
- `hermes_dynamic_workflows/run/manager.py`: sole owner of workflow control-button availability and completion edit button clearing.
- `tests/test_run_manager.py`: completion-card behavior and final edit integration tests.
- `tests/test_gateway_callback.py`: control-button availability tests.

### Hermes core repository: `/Users/atorres/.hermes/hermes-agent`

- `agent/prompt_builder.py`: stable universal task-completion/final-response guidance.
- `tests/run_agent/test_run_agent.py`: prompt inclusion, opt-out, uniqueness, and cache-stable wording tests.
- `tests/gateway/test_run_progress_topics.py`: existing commentary/final delivery separation remains regression evidence; no production gateway change planned.

Architecture deletion test: a separate renderer module would only move cohesive private helpers out of `completion.py` while creating another policy owner. Keep normalization and rendering together. A runtime semantic final-response validator would guess natural-language references and duplicate model responsibility; use stable prompt guidance plus deterministic prompt/gateway tests instead.

---

### Task 1: Normalize arbitrary workflow results into ordered rows

**Files:**
- Modify: `hermes_dynamic_workflows/view/completion.py:11-25,229-394`
- Test: `tests/test_run_manager.py:2430-2600`

**Interfaces:**
- Consumes: `record["result"]: Any`, existing `_recognized_outcome(value) -> str | None`, `_bounded_card_text(value, max_chars) -> str`.
- Produces: `_ResultRow(label: str, status: str | None, heading: str, summary: str, findings: tuple[str, ...], next_action: str, missing: bool)` and `_result_rows(result: Any) -> tuple[_ResultRow, ...]`.
- Later tasks consume `_result_rows()` only through `render_completion_card()`; no public API changes.

- [ ] **Step 1: Write failing normalization tests**

Add focused tests that call the pure helper directly:

```python
def test_result_rows_preserve_mixed_nested_results_in_order(self):
    from hermes_dynamic_workflows.view.completion import _result_rows

    rows = _result_rows({
        "results": [
            "PASS — zero blockers\nValidated source.",
            None,
            {
                "label": "Security scan",
                "status": "failed",
                "summary": "Three blockers remain.",
                "findings": ["Secret exposed.", "Unsafe redirect."],
                "nextAction": "Rotate credential.",
            },
        ]
    })

    self.assertEqual([row.label for row in rows], ["Result 1", "Result 2", "Security scan"])
    self.assertEqual(rows[0].heading, "PASS — zero blockers")
    self.assertEqual(rows[0].summary, "Validated source.")
    self.assertTrue(rows[1].missing)
    self.assertEqual(rows[1].heading, "No result returned")
    self.assertEqual(rows[2].status, "failed")
    self.assertEqual(rows[2].findings, ("Secret exposed.", "Unsafe redirect."))
    self.assertEqual(rows[2].next_action, "Rotate credential.")


def test_result_rows_do_not_promote_verdict_from_plain_string(self):
    from hermes_dynamic_workflows.view.completion import _result_rows

    row = _result_rows("Example failure text: FAIL does not describe this run.")[0]

    self.assertIsNone(row.status)
```

Also cover a scalar, ordinary dictionary, nested `results`, null, explicit `title`/`name`/`label`, malformed findings, and unsupported scalar.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py::CompletionCardRenderTests -q -o 'addopts='
```

Expected: FAIL because `_result_rows` and `_ResultRow` do not exist.

- [ ] **Step 3: Implement minimal pure normalization**

Add:

```python
@dataclass(frozen=True)
class _ResultRow:
    label: str
    status: str | None
    heading: str
    summary: str = ""
    findings: tuple[str, ...] = ()
    next_action: str = ""
    missing: bool = False


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
            label = _bounded_card_text(value.get("label") or value.get("title") or value.get("name") or fallback_label, 96)
            status = None
            for key in ("status", "verdict", "outcome"):
                if value.get(key) is None:
                    continue
                recognized = _recognized_outcome(value[key])
                if recognized is not None:
                    status = recognized
                    break
            heading = _bounded_card_text(value.get("title") or value.get("summary") or value.get("message") or label, 120)
            summary = _bounded_card_text(value.get("summary") or value.get("message"), 480)
            findings_value = value.get("findings") if isinstance(value.get("findings"), list) else []
            rows.append(_ResultRow(
                label,
                status,
                heading,
                summary if summary != heading else "",
                _render_finding_rows(findings_value),
                _bounded_card_text(value.get("nextAction") or value.get("next_action"), 400),
            ))
            continue
        rows.append(_ResultRow(fallback_label, None, _bounded_card_text(value, 120)))
    return tuple(rows)
```

Keep presentation-envelope and review-specific existing branches intact. Do not infer a plain string's status.

- [ ] **Step 4: Run normalization tests and verify GREEN**

Run the Task 1 command. Expected: all `CompletionCardRenderTests` pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add hermes_dynamic_workflows/view/completion.py tests/test_run_manager.py
git commit -m "feat: normalize generic workflow results"
```

---

### Task 2: Render all fitting rows under one Telegram budget

**Files:**
- Modify: `hermes_dynamic_workflows/view/completion.py:397-436`
- Test: `tests/test_run_manager.py:2430-2900`

**Interfaces:**
- Consumes: `_result_rows(result) -> tuple[_ResultRow, ...]`, `_fit_utf16(text, max_units=4096) -> str`, `render_run_metrics()`.
- Produces: `_render_result_rows(rows: tuple[_ResultRow, ...], *, max_units: int) -> str` and updated `render_completion_card(...) -> str`.

- [ ] **Step 1: Write failing card tests**

Add tests asserting:

```python
def test_nested_results_render_human_rows_without_raw_json(self):
    record = self._blocked_review_record()
    record["result"] = {"results": ["PASS — clean\nVerified.", None, {"status": "failed", "summary": "Three blockers."}]}

    text = manager_module._progress_bubble_text(
        record,
        PluginConfig(notify_progress_cost=False),
        completed=True,
    )

    self.assertIn("3 subtasks · 2 results · 1 missing", text)
    self.assertIn("1. PASS — clean", text)
    self.assertIn("2. No result returned", text)
    self.assertIn("3. Three blockers.", text)
    self.assertNotIn('"results"', text)
    self.assertNotIn("null", text)


def test_oversized_result_set_keeps_one_card_and_reports_overflow(self):
    record = self._blocked_review_record()
    record["result"] = {"results": [f"Result {index} " + ("x" * 180) for index in range(100)]}

    text = manager_module._progress_bubble_text(record, PluginConfig(notify_progress_cost=False), completed=True)

    self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
    self.assertRegex(text, r"… \d+ more results in stored report")
    self.assertIn("1. Result 0", text)
```

Also assert all headings appear for a five-item input, original order, details truncate before headings, findings/required action render, and metrics appear exactly once.

- [ ] **Step 2: Run tests and verify RED**

Run Task 1 command. Expected: FAIL because nested `results` still uses raw fallback and no overflow marker exists.

- [ ] **Step 3: Implement row renderer and budget allocation**

Implement `_render_result_rows()` with these exact priorities:

1. Build title and aggregate `N subtasks · X results · Y missing`.
2. Reserve UTF-16 units for metrics and `… N more results in stored report`.
3. Add numbered row headings in original order.
4. Add summary/findings/required-action detail only when remaining budget permits.
5. When the next heading cannot fit, stop and append exact overflow count.
6. Pass final output through `_fit_utf16()` as defense in depth.

Use a small helper:

```python
def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
```

Route generic list and nested `results` values through this path in `_build_completion_card()`/`render_completion_card()`. Keep explicit valid `presentation`, review aggregation, transport errors, and intentional stop behavior unchanged. Render `render_run_metrics()` once after result rows. Keep `render_cost_breakdown()` only when it adds per-subtask priced-agent information; do not duplicate the same total metrics line.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py::CompletionCardRenderTests tests/test_display.py -q -o 'addopts='
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add hermes_dynamic_workflows/view/completion.py tests/test_run_manager.py
git commit -m "feat: render readable workflow completion cards"
```

---

### Task 3: Remove terminal Rerun and clear stale controls

**Files:**
- Modify: `hermes_dynamic_workflows/run/manager.py:1852-1909,2093-2103`
- Test: `tests/test_gateway_callback.py:143-187`
- Test: `tests/test_run_manager.py` completion-edit button tests near existing progress bubble tests.

**Interfaces:**
- Consumes: `_control_buttons_for(record, config) -> list | None`.
- Produces: terminal records return only a valid Open log row or `None`; completion edit passes `buttons=[]` when no terminal controls remain.

- [ ] **Step 1: Replace terminal-Rerun expectation with failing absence tests**

```python
def test_terminal_control_buttons_do_not_include_rerun(self):
    for status in ("completed", "failed", "error", "stopped", "interrupted"):
        buttons = _control_buttons_for(self._record(status=status), PluginConfig())
        flattened = buttons or []
        if flattened and isinstance(flattened[0], list):
            flattened = [button for row in flattened for button in row]
        self.assertFalse(any(button.get("callback_data", "").startswith("wf:rerun:") for button in flattened))


def test_terminal_control_buttons_keep_open_log_only(self):
    record = self._record(status="completed")
    record["logUrl"] = "https://example.com/log"

    self.assertEqual(
        _control_buttons_for(record, PluginConfig()),
        [[{"text": "📄 Open log", "url": "https://example.com/log"}]],
    )
```

Add an integration assertion that final edit receives `buttons=[]` when there is no log URL, clearing Pause/Stop/Restart left from the live bubble.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_gateway_callback.py tests/test_run_manager.py -q -o 'addopts='
```

Expected: FAIL because terminal records still add `wf:rerun:<runId>`.

- [ ] **Step 3: Remove terminal Rerun production branch**

Delete `_TERMINAL_RERUN_STATES` and this branch only:

```python
if status in _TERMINAL_RERUN_STATES and run_id and record.get("scriptPath"):
    controls.append({"text": "🔁 Rerun", "callback_data": f"wf:rerun:{run_id}"})
```

Keep callback handling/storage support for explicit rerun commands unchanged. Update the completion-edit comment so it describes clearing stale active controls rather than terminal Rerun.

- [ ] **Step 4: Run control tests and verify GREEN**

Run Task 3 command. Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add hermes_dynamic_workflows/run/manager.py tests/test_gateway_callback.py tests/test_run_manager.py
git commit -m "fix: remove terminal workflow rerun control"
```

---

### Task 4: Make final responses self-contained in stable Hermes guidance

**Repository:** `/Users/atorres/.hermes/hermes-agent`

**Files:**
- Modify: `agent/prompt_builder.py:321-334`
- Test: `tests/run_agent/test_run_agent.py:1541-1620`
- Verify unchanged gateway behavior: `tests/gateway/test_run_progress_topics.py:832-1090`

**Interfaces:**
- Consumes: `TASK_COMPLETION_GUIDANCE: str`, `agent.task_completion_guidance: bool`.
- Produces: stable cached instruction included exactly once when enabled and absent when disabled.

- [ ] **Step 1: Write failing stable-guidance tests**

Add to `TestTaskCompletionGuidance`:

```python
def test_guidance_requires_self_contained_final_responses(self):
    agent = self._make_agent()
    prompt = agent._build_system_prompt()

    assert "Intermediate commentary is progress-only" in prompt
    assert "complete question and every referenced option" in prompt
    assert prompt.count("Intermediate commentary is progress-only") == 1


def test_self_contained_guidance_respects_task_completion_opt_out(self):
    agent = self._make_agent(task_completion_guidance=False)

    assert "Intermediate commentary is progress-only" not in agent._build_system_prompt()
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
scripts/run_tests.sh tests/run_agent/test_run_agent.py -k TaskCompletionGuidance
```

Expected: FAIL because wording is absent.

- [ ] **Step 3: Add minimal stable guidance**

Append to `TASK_COMPLETION_GUIDANCE` without creating a second block:

```python
"\nIntermediate commentary is progress-only and may be edited or omitted by a "
"messaging gateway. Keep every final response self-contained. A final "
"clarification must include the complete question and every referenced option; "
"never require the user to recover information from commentary."
```

Do not add turn-specific content, a hook, a runtime parser, or gateway-specific prompt mutation.

- [ ] **Step 4: Verify focused prompt and gateway suites**

Run:

```bash
scripts/run_tests.sh \
  tests/run_agent/test_run_agent.py \
  tests/agent/test_system_prompt.py \
  tests/agent/test_prompt_caching.py \
  tests/gateway/test_run_progress_topics.py \
  tests/gateway/test_duplicate_reply_suppression.py
```

Expected: PASS with zero failures. Confirm repeated `_build_system_prompt()` calls return byte-identical content for the same agent session.

- [ ] **Step 5: Commit Task 4**

```bash
git add agent/prompt_builder.py tests/run_agent/test_run_agent.py
git commit -m "fix: keep final responses self-contained"
```

---

### Task 5: Integrated verification and live Telegram canaries

**Files:**
- No planned production changes.
- Update tests only if a genuine uncovered regression appears; any production fix starts a new RED cycle.

**Interfaces:**
- Validates plugin completion card, Telegram button clearing, stable prompt guidance, and gateway delivery together.

- [ ] **Step 1: Run complete plugin suite in a clean shell**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q -o 'addopts='
```

Expected: PASS. If either documented CPU-starvation timing test fails, rerun that exact test against unchanged baseline before classifying it; do not hide a new failure.

- [ ] **Step 2: Run core focused suite**

```bash
cd /Users/atorres/.hermes/hermes-agent
scripts/run_tests.sh \
  tests/run_agent/test_run_agent.py \
  tests/agent/test_system_prompt.py \
  tests/agent/test_prompt_caching.py \
  tests/gateway/test_run_progress_topics.py \
  tests/gateway/test_duplicate_reply_suppression.py
```

Expected: PASS.

- [ ] **Step 3: Inspect exact diffs and scope**

In each repository run:

```bash
git status --short
git diff --check HEAD~3..HEAD
git diff --stat HEAD~3..HEAD
git diff --numstat HEAD~3..HEAD
```

For core use the actual Task 4 commit range rather than `HEAD~3`. Confirm plugin `uv.lock` remains untracked and untouched.

- [ ] **Step 4: Restart gateway from outside the serving gateway process**

Use the supported Hermes gateway restart path. Verify exactly one `hermes_cli.main gateway run` process serves afterward and its start time is after both implementation commits.

- [ ] **Step 5: Run mixed-result workflow canary**

Run a small workflow whose final result is:

```python
return {
    "results": [
        "PASS — first task complete\nEvidence retained.",
        None,
        {"label": "Third task", "status": "failed", "summary": "One blocker remains."},
    ]
}
```

Verify Telegram shows three ordered rows, `No result returned`, one metrics line, no raw JSON dump, no Rerun button, and no stale active controls.

- [ ] **Step 6: Run overflow canary**

Render or run enough long results to exceed 4096 units. Verify one Telegram card, an explicit `… N more results in stored report` marker, and persisted full output.

- [ ] **Step 7: Run clarification canary**

Cause the agent to ask a multi-option clarification after an interim progress message. Verify the final Telegram message itself contains the complete question and every option. Do not accept the prompt test alone as visual delivery proof.

- [ ] **Step 8: Parent final review and publication**

Parent reviews both final diffs and test evidence. Commit/push each repository through its native workflow. For Hermes core, follow the fork update model. For the plugin, push the current verified working branch only after checking `git branch -vv` and remote heads. Report clickable commit URLs.
