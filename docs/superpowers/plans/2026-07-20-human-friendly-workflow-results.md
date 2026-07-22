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
- Show every top-level subtask in the compact index while the index rows fit; otherwise show `… N more results in stored report`.
- Keep one bounded summary line for each visible returned result while detail budget permits; give exception sections priority for additional findings and actions.
- Keep final Telegram content within 4096 UTF-16 units.
- Preserve complete machine-readable workflow output.
- Render metrics once.
- Compose Markdown from trusted static delimiters only; escape model- and workflow-provided content before inserting it, and never sanitize the composed Markdown as plain text.
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
- `tests/test_display.py`: completion-card length and display regression coverage.
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


def test_result_rows_normalize_ordinary_dictionary_fields(self):
    from hermes_dynamic_workflows.view.completion import _result_rows

    row = _result_rows({
        "title": "Security scan",
        "status": "warning",
        "summary": "One check needs attention.",
        "findings": ["Synthetic finding."],
        "nextAction": "Review the retained evidence.",
        "opaque": {"must_remain_persisted": True},
    })[0]

    self.assertEqual(row.label, "Security scan")
    self.assertEqual(row.status, "warning")
    self.assertEqual(row.summary, "One check needs attention.")
    self.assertEqual(row.findings, ("Synthetic finding.",))
    self.assertEqual(row.next_action, "Review the retained evidence.")
```

Also cover a scalar, ordinary dictionary, nested `results`, null, explicit `title`/`name`/`label`, malformed findings, and unsupported scalar. Assert that every returned row summary is a single bounded line after embedded whitespace is normalized; missing rows remain summary-free.

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
            summary = _bounded_card_text("\n".join(lines[1:]), 480) if len(lines) > 1 else ""
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
    self.assertIn("PASS — clean", text)
    self.assertIn("No result returned", text)
    self.assertIn("Three blockers.", text)
    self.assertNotIn('"results"', text)
    self.assertNotIn("null", text)


def test_oversized_result_set_keeps_one_card_and_reports_overflow(self):
    record = self._blocked_review_record()
    record["result"] = {"results": [f"Result {index} " + ("x" * 180) for index in range(100)]}

    text = manager_module._progress_bubble_text(record, PluginConfig(notify_progress_cost=False), completed=True)

    self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
    self.assertRegex(text, r"… \d+ more results in stored report")
    self.assertIn("Result 0", text)
```

Also assert all five item titles appear in original order in the structural output, details truncate before structural rows, findings/required action render, and metrics appear exactly once. These are semantic and budget assertions; Task 6 replaces the baseline plain-text shape with the approved hybrid Markdown hierarchy and updates exact formatting assertions.

- [ ] **Step 2: Run tests and verify RED**

Run Task 1 command. Expected: FAIL because nested `results` still uses raw fallback and no overflow marker exists.

- [ ] **Step 3: Implement row renderer and budget allocation**

Implement `_render_result_rows()` with these exact priorities:

1. Build title and aggregate `N subtasks · X results · Y missing`.
2. Reserve UTF-16 units for metrics and `… N more results in stored report`.
3. Preserve each row's original ordinal, marker, and bounded title as structural inputs for the later compact-index renderer; do not put summaries, findings, paths, or commands into index columns.
4. Add bounded detail only when remaining budget permits, keeping structural rows, overflow text, and metrics ahead of detail.
5. When the next structural row cannot fit, stop and append the exact overflow count.
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

> **Rich-card sequencing:** Tasks 6-7 are the approved follow-up for the hybrid Markdown card. Execute Task 5 Steps 1-4 and Step 7 as baseline gates after Tasks 1-4; defer rich-card Steps 5-6 and publication Step 8 until Tasks 6-7. Do not treat the baseline plain-text examples from earlier tasks as final visual acceptance; the post-Task-6 formatter probe and live canaries in Task 7 are authoritative for the rich hierarchy, and Task 7 Step 5 supersedes Task 5 Step 8 publication timing.

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

Use the supported Hermes gateway restart path from a controlling shell that is not the serving process:

```bash
hermes gateway restart
hermes gateway status
pgrep -af 'hermes_cli.main gateway run'
```

Expected: `hermes gateway status` reports the gateway healthy, `pgrep` reports exactly one serving `hermes_cli.main gateway run` process, and its start time is after the implementation commits under test. Task 7 repeats this check after the Task 6 commit.

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

Verify Telegram shows the approved hybrid card: a bold warning title, italic aggregate metadata, one fenced compact index with three ordered rows, titled detail sections, exactly one italic summary for the successful first result, exception findings/action hierarchy, one italic metrics line, no raw JSON dump, no Rerun button, and no stale active controls.

- [ ] **Step 6: Run overflow canary**

Render or run enough long results to exceed the compact-index budget. Verify one Telegram card with an aligned index prefix, an italic `… N more results in stored report` marker before italic metrics, exception detail priority, raw and post-formatter lengths within 4096 UTF-16 units, and persisted full output containing every returned result.

- [ ] **Step 7: Run clarification canary**

Cause the agent to ask a multi-option clarification after an interim progress message. Verify the final Telegram message itself contains the complete question and every option. Do not accept the prompt test alone as visual delivery proof.

- [ ] **Step 8: Parent final review and publication**

Parent reviews both final diffs and test evidence. Commit/push each repository through its native workflow. For Hermes core, follow the fork update model. For the plugin, push the current verified working branch only after checking `git branch -vv` and remote heads. Report clickable commit URLs.

---

### Task 6: Rich Markdown result-card hierarchy

**Files:**
- Modify: `hermes_dynamic_workflows/view/completion.py:50-66,258-344,612-653`
- Test: `tests/test_run_manager.py::CompletionCardRenderTests`

**Interfaces:**
- Consumes: `_ResultRow`, `_result_row_marker(row: _ResultRow) -> str`, `_utf16_units()`, `_fit_utf16()`, `render_run_metrics()`.
- Produces: `_escape_markdown_content(text: str) -> str`, `_inline_code_content(text: str) -> str`, `_result_index(rows: tuple[_ResultRow, ...], *, max_units: int) -> tuple[str, int]`, and updated `_render_result_rows()` / `render_completion_card()` output.
- Trusted renderer Markdown is composed only from static delimiters. Every model-provided title, summary, finding, action, metric fragment, and workflow name passes through `_escape_markdown_content()` or `_inline_code_content()` before entering trusted formatting. `_sanitize_text()` remains only for plain/raw fallback content; never apply it to a composed card because it inserts zero-width characters into trusted fences and emphasis. The real Telegram adapter remains the final MarkdownV2 conversion boundary.

- [ ] **Step 1: Write failing hierarchy and safety tests**

Add focused tests:

```python
def test_mixed_results_render_hybrid_markdown_hierarchy(self):
    record = self._blocked_review_record()
    record["result"] = {
        "results": [
            "PASS — first task complete\nEvidence retained.",
            None,
            {
                "label": "Third task",
                "status": "failed",
                "summary": "One blocker remains.",
                "findings": ["Synthetic canary blocker."],
                "nextAction": "Confirm this card is readable.",
            },
        ]
    }

    text = manager_module._progress_bubble_text(
        record, PluginConfig(notify_progress_cost=False), completed=True,
    )

    self.assertTrue(text.startswith("**⚠️ Final review task 7 needs attention**"), text)
    self.assertIn("*3 subtasks · 2 results · 1 missing*", text)
    self.assertIn("```\n01  ✅  PASS — first task complete", text)
    self.assertIn("02  ⚠️  No result returned", text)
    self.assertIn("03  ❌  One blocker remains.", text)
    self.assertIn("**✅ 1 · PASS — first task complete**", text)
    self.assertIn("*Evidence retained.*", text)
    self.assertIn("**Findings**\n• Synthetic canary blocker.", text)
    self.assertIn("**Required action**\n`Confirm this card is readable.`", text)
    self.assertRegex(text, r"\*11m 31s · 2 agents · 5\.04M tokens\*$")
    self.assertEqual(text.count("*Evidence retained.*"), 1)
    self.assertEqual(text.count("```"), 2)

def test_plain_heading_verdict_is_row_marker_only(self):
    record = self._blocked_review_record()
    record["result"] = {
        "results": [
            "PASS — explicit row label",
            "Example failure text: FAIL does not describe this run.",
            "Quoted `BLOCK` is diagnostic text, not a verdict.",
        ]
    }

    rows = manager_module._result_rows(record["result"])
    text = manager_module._progress_bubble_text(
        record, PluginConfig(notify_progress_cost=False), completed=True,
    )

    self.assertIsNone(rows[0].status)
    self.assertIn("01  ✅  PASS — explicit row label", text)
    self.assertIn("02  •  Example failure text: FAIL", text)
    self.assertIn("03  •  Quoted", text)


def test_index_ordinal_width_matches_largest_ordinal(self):
    from hermes_dynamic_workflows.view.completion import _ResultRow, _result_index

    for count, first_ordinal in ((9, "01"), (10, "01"), (99, "01"), (100, "001")):
        width = max(2, len(str(count)))
        rows = tuple(
            _ResultRow(f"Task {index}", None, f"Task {index}")
            for index in range(1, count + 1)
        )
        index_text, visible_count = _result_index(rows, max_units=4096)

        self.assertEqual(visible_count, count)
        self.assertIn(f"{first_ordinal}  •  Task 1", index_text)
        self.assertIn(
            f"{str(count).zfill(width)}  •  Task {count}",
            index_text,
        )

def test_model_markdown_cannot_break_card_structure(self):
    record = self._blocked_review_record()
    record["result"] = {
        "results": [{
            "status": "failed",
            "title": "**fake title** ```",
            "summary": "_fake summary_ [link](https://example.com) ~~fake strike~~ ||fake spoiler||",
            "findings": ["`fake code` and **fake bold**"],
            "nextAction": "run `unsafe` \\ now",
        }]
    }

    text = manager_module._progress_bubble_text(
        record, PluginConfig(notify_progress_cost=False), completed=True,
    )

    self.assertEqual(text.count("```"), 2)
    self.assertIn(r"\*\*fake title\*\*", text)
    self.assertIn(r"\_fake summary\_", text)
    self.assertIn(r"\[link\]", text)
    self.assertIn(r"\~\~fake strike\~\~", text)
    self.assertIn(r"\|\|fake spoiler\|\|", text)
    self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)


def test_valid_presentation_remains_authoritative_and_report_stays_hidden(self):
    record = self._blocked_review_record()
    record["result"] = {
        "presentation": {
            "status": "blocked",
            "title": "Final review blocked",
            "summary": "No implementation defects were found.",
            "findings": ["Synthetic test records remain in live storage."],
            "nextAction": "Remove stale artifacts, then rerun the review.",
        },
        "report": {"secret_internal_key": "must stay out of the card"},
    }

    text = manager_module._progress_bubble_text(
        record, PluginConfig(notify_progress_cost=False), completed=True,
    )

    self.assertTrue(text.startswith("**⛔ Final review blocked**"), text)
    self.assertIn("*No implementation defects were found.*", text)
    self.assertIn("**Findings**", text)
    self.assertIn("`Remove stale artifacts, then rerun the review.`", text)
    self.assertNotIn("secret_internal_key", text)
    self.assertEqual(text.count("5.04M tokens"), 1)


def test_malformed_presentation_uses_bounded_raw_fallback(self):
    record = self._blocked_review_record()
    record["result"] = {
        "presentation": {"status": "blocked", "findings": False},
        "report": {"opaque": "retained"},
    }

    text = manager_module._progress_bubble_text(
        record, PluginConfig(notify_progress_cost=False), completed=True,
    )

    self.assertIn("Result:", text)
    self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
```

Also add tests for every visible successful row's exactly one italic summary while budget permits; exception details before successful details under pressure while index order and original ordinals stay stable; ordinal width at 9, 10, 99, and 100 rows; missing rows without invented summaries; italic overflow before italic metrics; and unchanged explicit presentation/review/raw fallback behavior. The pressure case must reserve one summary line per visible returned row before optional findings/actions when those summaries fit, then spend remaining detail budget on exception findings/actions before successful extras.

- [ ] **Step 2: Run tests and verify RED**

```bash
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py::CompletionCardRenderTests -q -o 'addopts='
```

Expected: FAIL because title/metadata/detail formatting and compact index fences do not exist.

- [ ] **Step 3: Add untrusted-content escaping helpers**

Implement:

```python
_MARKDOWN_CONTENT_RE = re.compile(r"([\\`*_\[\]~|])")


def _escape_markdown_content(text: str) -> str:
    sanitized = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))
    return _MARKDOWN_CONTENT_RE.sub(r"\\\1", sanitized)


def _inline_code_content(text: str) -> str:
    sanitized = "".join("�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in str(text))
    return sanitized.replace("\\", "\\\\").replace("`", "\\`")
```

Escape the source-level Markdown controls that could create bold, italic, link, strike, spoiler, or code syntax (`\\`, backtick, `*`, `_`, `[`, `]`, `~`, and `|`). Keep `_sanitize_text()` for plain/raw fallback protection only. Do not run trusted renderer fences or emphasis through `_sanitize_text()`, because it intentionally breaks triple backticks; audit and remove any existing whole-card `_sanitize_text(prefix|suffix|base|candidate)` calls after trusted composition.

- [ ] **Step 4: Render trusted compact index**

Build escaped index rows with `str(index).zfill(width)` where `width = max(2, len(str(max(1, len(rows)))))`, a fixed status column, and bounded title only. Update `_result_row_marker()` so a structured `row.status` controls its marker; for a plain-string heading, recognize only an explicit leading display prefix such as `PASS —`, `OK:`, `WARN —`, or `FAIL —` for the row marker while leaving `row.status` and overall transport/task outcome unchanged. Quoted, negated, or diagnostic `FAIL`/`BLOCK` prose stays neutral (`•`). Use a neutral marker such as `•` for unknown status. Keep the largest ordered prefix whose trusted fences and optional italic overflow marker fit `max_units`. Return `(rendered_text, visible_count)` with shape:

````python
f"```\n{index_rows}\n```\n\n*… {hidden} more results in stored report*"
````

Never place summaries, findings, paths, commands, or action text in index columns. Do not use pipe or HTML tables. When a required action or intentionally rendered identifier/path/command is emitted in a detail section, use `_inline_code_content()` only for a single-line value that cannot close a code span; otherwise use escaped plain content.

- [ ] **Step 5: Render prioritized rich detail sections**

Update `_render_result_rows()`:

1. Emit italic aggregate metadata.
2. Emit compact index.
3. Build two stable detail groups: exception rows (`missing` or status in `{blocked, failed, warning}`), then all remaining rows. Missing rows get a heading only; returned rows get at most one summary line at this stage.
4. Emit trusted bold heading with original ordinal and marker.
5. Emit one italic escaped summary per visible returned row before optional extra detail when that summary fits; never emit the same summary both in the index and in the detail section.
6. Emit trusted bold `Findings` and escaped bullets, spending remaining detail budget on exception rows before successful/unknown rows.
7. Emit trusted bold `Required action` and inline-code content only when the action is single-line and contains no backtick; otherwise emit escaped plain content without creating an unsafe code span.
8. Stop details before reserved metrics/overflow budget is crossed.

Do not invent a missing-row summary.

- [ ] **Step 6: Format trusted card shell and metrics**

Use single-asterisk source spans for italics (`*text*`), because the live Telegram adapter converts that standard Markdown form to MarkdownV2 italics; source underscores are escaped literally by `TelegramAdapter.format_message()`.

Use:

```python
prefix = f"**{_completion_icon(card.status)} {_escape_markdown_content(card.title)}**"
metrics_text = f"*{_escape_markdown_content(metrics)}*" if metrics else ""
```

Existing explicit presentation/review branches also receive bold title/labels, italic summaries/metrics, escaped findings, and inline-code required action when safe. Preserve plain/raw fallback delivery if semantic normalization fails, preserve the one metrics line, and do not duplicate total cost when `render_cost_breakdown()` has no per-subtask priced-agent data.

- [ ] **Step 7: Run focused and full suites**

```bash
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py::CompletionCardRenderTests tests/test_display.py -q -o 'addopts='

env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q -o 'addopts='
```

Expected: PASS. Run `git diff --check`; confirm only `completion.py` plus the focused tests changed and that both the raw card and the adapter-formatted card stay within 4096 UTF-16 units for the hostile fixture.

- [ ] **Step 8: Commit Task 6**

```bash
git add hermes_dynamic_workflows/view/completion.py tests/test_run_manager.py tests/test_display.py
git commit -m "feat: format workflow result cards"
```

---

### Task 7: Rich-card Telegram verification and publication

**Files:**
- No planned production changes.
- Any discovered defect begins with a focused failing test before production edits.

**Probe source:** `/Users/atorres/.hermes/hermes-agent/plugins/platforms/telegram/adapter.py`, `TelegramAdapter.format_message(content: str) -> str`.

**Interfaces:**
- Validates source Markdown structure, the real `TelegramAdapter.format_message(content: str) -> str` conversion, edited completion delivery, button clearing, and mobile-readable layout.

- [ ] **Step 1: Probe Markdown conversion with hostile content**

From the plugin checkout, feed the exact Task 6 mixed and hostile-content outputs through the real adapter formatter (not a hand-written Markdown parser):

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \\
  PYTHONPATH="/Users/atorres/.hermes/hermes-agent:/Users/atorres/Documents/GitHub/hermes-dynamic-workflows" \\
  /Users/atorres/.hermes/hermes-agent/venv/bin/python - <<'PY'
from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.run import manager as manager_module
from plugins.platforms.telegram.adapter import TelegramAdapter
from tests.test_run_manager import CompletionCardRenderTests

fixture = CompletionCardRenderTests()

def render(result):
    record = fixture._blocked_review_record()
    record["result"] = result
    return manager_module._progress_bubble_text(
        record,
        PluginConfig(notify_progress_cost=False),
        completed=True,
    )

cards = [
    render({
        "results": [
            "PASS — first task complete\\nEvidence retained.",
            None,
            {
                "label": "Third task",
                "status": "failed",
                "summary": "One blocker remains.",
                "findings": ["Synthetic canary blocker."],
                "nextAction": "Confirm this card is readable.",
            },
        ]
    }),
    render({
        "results": [{
            "status": "failed",
            "title": "**fake title** ```",
            "summary": "_fake summary_ [link](https://example.com) ~~fake strike~~ ||fake spoiler||",
            "findings": ["`fake code` and **fake bold**"],
            "nextAction": "run `unsafe` \\\\ now",
        }]
    }),
]

adapter = TelegramAdapter(PlatformConfig(enabled=True, token="formatter-probe"))
for index, card in enumerate(cards):
    formatted = adapter.format_message(card)
    assert isinstance(formatted, str)
    assert card.count("```") == 2
    assert formatted.count("```") == 2
    assert utf16_len(card) <= 4096
    assert utf16_len(formatted) <= 4096
    if index == 0:
        assert "*⚠️ Final review task 7 needs attention*" in formatted
        assert "_3 subtasks" in formatted
        assert "_Evidence retained" in formatted
    else:
        assert "fake title" in formatted and "fake summary" in formatted
        assert r"\*\*fake" in formatted or r"\_fake" in formatted
print("TelegramAdapter.format_message probe: PASS; 2 cards formatted, fences preserved, UTF-16 <= 4096")
PY
```

The probe must construct `cards` from the actual Task 6 renderer output before the loop; do not substitute a guessed string. Assert conversion succeeds without plain-text fallback, exactly one fenced block survives, bold/italic entities remain balanced, and escaped model content stays literal. If the formatter or live edit falls back to plain text, stop and add a focused failing regression test before any production edit.

- [ ] **Step 2: Restart gateway**

Restart from a separate controlling shell with `hermes gateway restart` followed by `hermes gateway status`. Verify exactly one post-change `hermes_cli.main gateway run` process with a start time after the Task 6 commit, and confirm the serving checkout contains that commit with `git -C /Users/atorres/Documents/GitHub/hermes-dynamic-workflows show --no-patch --format='%H %s' HEAD`.

- [ ] **Step 3: Run live mixed-result canary**

Verify the edited completion delivery, not only a direct helper call: bold warning title, italic aggregate metadata, aligned fenced compact index, bold result headings, exactly one italic success summary, exception findings/action hierarchy, italic metrics exactly once, no raw Markdown punctuation or raw JSON, no Rerun button, and `buttons=[]` when no Open log URL exists.

- [ ] **Step 4: Run live overflow canary**

Return 100 long results. Verify the width-matched index prefix plus italic overflow marker remain readable, exception detail receives priority, the formatted delivery remains one card within Telegram's 4096 UTF-16 limit, and the persisted workflow record still contains all 100 results.

- [ ] **Step 5: Parent final review and guarded publication**

Parent reviews final diff and test/live evidence. Publish plugin master through guarded git operations, verify remote SHA, and report the clickable commit URL. Clean implementation worktree/branch only after remote and live read-back succeed.
