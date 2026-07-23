# Human-Friendly Workflow Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render arbitrary workflow results as compact human-readable Telegram cards, deliver the rich result as a separate message from the terminal execution snapshot, remove terminal Rerun and all Telegram Restart controls, and make final clarification responses self-contained.

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
- Preserve the existing standalone/default completion-card metrics footer and optional cost-breakdown behavior; for a wrapped result message, render duration/cost/token metrics exactly once in the stable header and suppress the card's internal metrics and cost breakdown.
- Compose Markdown from trusted static delimiters only; escape model- and workflow-provided content before inserting it, and never sanitize the composed Markdown as plain text.
- Remove Telegram Restart and terminal Rerun buttons from every workflow message; preserve active Pause, Resume, Stop, and valid Open log URL.
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
- Produces: `_render_result_rows(rows: tuple[_ResultRow, ...], *, max_units: int) -> str` and `render_completion_card(record: dict[str, Any], *, preview_chars: int, show_cost: bool, max_units: int = 4096, include_metrics: bool = True) -> str`.

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

Also assert all five item titles appear in original order in the structural output, details truncate before structural rows, findings/required action render, and the standalone/default metrics footer appears exactly once. These are semantic and budget assertions; Task 6 replaces the baseline plain-text shape with the approved hybrid Markdown hierarchy and updates exact formatting assertions.

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

Route generic list and nested `results` values through this path in `_build_completion_card()`/`render_completion_card()`. Keep explicit valid `presentation`, review aggregation, transport errors, and intentional stop behavior unchanged. With the default `include_metrics=True`, render `render_run_metrics()` once after result rows and preserve `render_cost_breakdown()` only when it adds per-subtask priced-agent information; do not duplicate the same total metrics line.

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
- Produces: terminal records return only a valid Open log row or `None`; completion edit passes `buttons=[]` when no terminal controls remain. Task 10 removes the active-state Telegram Restart button while preserving restart callbacks/backend support.

- [ ] **Step 1: Preserve active controls and replace terminal-Rerun expectation with failing absence tests**

```python
def test_running_control_buttons_include_pause_stop_restart(self):
    buttons = _control_buttons_for(self._record(status="running"), PluginConfig())
    callbacks = [button["callback_data"] for button in buttons]
    self.assertIn("wf:pause:wf_abc123", callbacks)
    self.assertIn("wf:stop:wg123", callbacks)
    self.assertIn("wf:restart:wf_abc123", callbacks)


def test_paused_control_buttons_include_resume_stop_restart(self):
    buttons = _control_buttons_for(self._record(status="paused"), PluginConfig())
    callbacks = [button["callback_data"] for button in buttons]
    self.assertIn("wf:resume:wf_abc123", callbacks)
    self.assertIn("wf:stop:wg123", callbacks)
    self.assertIn("wf:restart:wf_abc123", callbacks)


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
        [{"text": "📄 Open log", "url": "https://example.com/log"}],
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

Expected: FAIL because terminal records still add `wf:rerun:<runId>` before Task 3’s production edit; the active Restart absence remains a follow-up assertion for Task 10.

- [ ] **Step 3: Remove terminal Rerun production branch**

Delete `_TERMINAL_RERUN_STATES` and this branch only:

```python
if status in _TERMINAL_RERUN_STATES and run_id and record.get("scriptPath"):
    controls.append({"text": "🔁 Rerun", "callback_data": f"wf:rerun:{run_id}"})
```

Keep callback handling/storage support for explicit rerun commands unchanged. Update the completion-edit comment so it describes clearing stale active controls rather than terminal Rerun.

- [ ] **Step 4: Run control tests and verify GREEN**

Run Task 3 command. Expected: PASS for terminal Rerun absence and active Pause/Resume/Stop coverage; Task 10 will remove the active Telegram Restart branch.

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

Verify Telegram shows the approved two-message delivery: the original execution message is a terminal task-tree snapshot, while the separate result begins with the stable workflow header and uses that header as the sole source of duration/cost/token metrics. Its card body has a bold warning title, italic aggregate metadata, one fenced compact index with three ordered rows, titled detail sections, exactly one italic summary for the successful first result, exception findings/action hierarchy, no internal metrics footer or cost breakdown, no raw JSON dump, no Telegram Restart/Rerun button, and no stale active controls.

- [ ] **Step 6: Run overflow canary**

Render or run enough long results to exceed the compact-index budget. Verify the separate result card has an aligned index prefix and an italic `… N more results in stored report` marker before its detail content, while the stable header remains the sole duration/cost/token metrics line outside the card. Verify exception detail priority, raw and post-formatter lengths within 4096 UTF-16 units, the terminal execution snapshot remains a distinct task-tree message, and persisted full output contains every returned result.

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
- Consumes: `_ResultRow`, `_result_row_marker(row: _ResultRow) -> str`, `_utf16_units()`, `_fit_utf16()`, and the default `render_run_metrics()` path.
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

Existing explicit presentation/review branches also receive bold title/labels, italic summaries/metrics, escaped findings, and inline-code required action when safe. Preserve plain/raw fallback delivery if semantic normalization fails. The default `include_metrics=True` path keeps the existing metrics footer and optional cost-breakdown behavior; the wrapped-result path added in Task 9 passes `include_metrics=False` and emits neither metrics nor cost breakdown from the card.

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

- Verify the two-message edited completion delivery, not only a direct helper call: the terminal execution edit has only the stable task-tree snapshot and optional Open log, the separate result starts with the stable workflow header, each duration/cost/token segment occurs exactly once in that header, the card has the bold warning title, italic aggregate metadata, aligned fenced compact index, bold result headings, exactly one italic success summary, exception findings/action hierarchy, no internal metrics footer or cost breakdown, no raw Markdown punctuation or raw JSON, no Telegram Restart/Rerun button, and the result send has no buttons.

- [ ] **Step 4: Run live overflow canary**

Return 100 long results. Verify the width-matched index prefix plus italic overflow marker remain readable, exception detail receives priority, the formatted delivery remains one card within Telegram's 4096 UTF-16 limit, and the persisted workflow record still contains all 100 results.

- [ ] **Step 5: Parent final review and guarded publication**

Parent reviews final diff and test/live evidence. Publish plugin master through guarded git operations, verify remote SHA, and report the clickable commit URL. Clean implementation worktree/branch only after remote and live read-back succeed.

---

## Follow-up scope: separate execution and result messages

Tasks 8–10 are the approved follow-up to the lifecycle contract in the design spec. They supersede any earlier wording that treats the final edit of the progress bubble as the result-card delivery: the original message is the execution reference and ends as a terminal task-tree snapshot; the result card is always delivered by a distinct send. In particular, update the old Task 5/Task 7 live assertions from “one edited completion card” to “one terminal execution edit followed by one separate result send.” Task 6’s renderer tests continue to exercise `_progress_bubble_text(..., completed=True)` as the rich-card renderer; Task 9 makes the manager’s final edit use a separate terminal-snapshot helper, so the existing 4096-unit rich-card renderer is not replaced.

These follow-up tasks are plugin-only. Do not modify Hermes core, gateway core, callback storage, TUI restart commands, dependency declarations, `uv.lock`, or `.superpowers/`; use no new dependency. The only persistent delivery markers are `resultMessageId` when the adapter returns a confirmed message id and `resultMessageDelivered` when an adapter confirms success without exposing an id. Both are written to the run record only after confirmed send success. A transient in-flight flag is held only in `ManagedRun` and is never used as a durable success marker.

### Task 8: Stable workflow header and terminal task-tree snapshot

**Files:**
- Modify: `hermes_dynamic_workflows/view/render.py:104-118,282-385,469-485,806-832`
- Modify: `hermes_dynamic_workflows/run/manager.py:70-106,2048-2176`
- Test: `tests/test_display.py` stable-header and task-marker tests
- Test: `tests/test_run_manager.py` terminal progress-render tests

**Interfaces:**
- Produces: `render_workflow_header(run: dict[str, Any], *, show_cost: bool = True) -> str`, whose first character is always `🔄` and whose complete shape is `🔄 <workflow-name> · <duration> · <cost> · <tokens>` with absent metrics omitted.
- Produces: `render_terminal_task_snapshot(run: dict[str, Any], *, show_cost: bool = True) -> str`, which delegates to the detailed progress renderer and never calls `render_completion_card`.
- Preserves: `_progress_bubble_text(record, config, completed=True)` as the rich-card renderer used by the existing completion-card tests. Only `_edit_progress_bubble(..., completed=True)` switches to `render_terminal_task_snapshot` in Task 9.

- [ ] **Step 1: Write failing stable-header and glyph tests**

Add the following focused tests. The fixture deliberately supplies fixed snapshot duration, totals, costable usage, and a failed agent so the header and tree are deterministic:

```python
def test_stable_workflow_header_is_shared_shape_for_active_and_terminal_progress(self):
    from hermes_dynamic_workflows.view.render import render_run_progress, render_workflow_header

    run = {
        "status": "running",
        "workflow": {
            "meta": {"name": "consolidate-delegation-policy"},
            "duration_seconds": 597,
            "totals": {"agents": 2, "done": 1, "running": 1, "errors": 0, "tokens": 1_860_000},
            "agents": [
                {"id": 1, "label": "policy", "status": "done", "model": "gpt-5.6-luna", "tokens": 930_000},
                {"id": 2, "label": "delegation", "status": "running", "model": "gpt-5.6-luna", "tokens": 930_000},
            ],
        },
    }

    from unittest.mock import patch

    header = render_workflow_header(run, show_cost=False)
    progress_header = render_run_progress(run, show_cost=False).splitlines()[0]
    with patch("hermes_dynamic_workflows.view.render._format_cost", return_value="~$1.05"):
        priced_header = render_workflow_header(run, show_cost=True)

    assert header == "🔄 consolidate-delegation-policy · 9m 57s · ~1.86M tok"
    assert progress_header == header
    assert priced_header == "🔄 consolidate-delegation-policy · 9m 57s · ~$1.05 · ~1.86M tok"
    assert not progress_header.startswith("✅")
    assert not progress_header.startswith("❌")


def test_terminal_task_snapshot_uses_tree_glyphs_and_has_no_result_card(self):
    from hermes_dynamic_workflows.view.render import render_terminal_task_snapshot

    run = {
        "status": "failed",
        "workflow": {
            "meta": {"name": "glyph-canary"},
            "duration_seconds": 2,
            "totals": {"agents": 2, "done": 1, "running": 0, "errors": 1, "tokens": 0},
            "agents": [
                {"id": 1, "label": "completed task", "status": "done"},
                {"id": 2, "label": "failed task", "status": "failed"},
            ],
        },
    }

    text = render_terminal_task_snapshot(run, show_cost=False)

    assert text.startswith("🔄 glyph-canary · 2s")
    assert "✓ completed task" in text
    assert "✗ failed task" in text
    assert "✅" not in text
    assert "❌" not in text
    assert "**" not in text


def test_failed_topology_row_uses_tree_failure_glyph(self):
    from hermes_dynamic_workflows.view.render import render_terminal_task_snapshot

    run = {
        "status": "failed",
        "workflow": {
            "meta": {"name": "topology-glyph-canary"},
            "duration_seconds": 1,
            "totals": {"agents": 0, "done": 0, "running": 0, "errors": 1, "tokens": 0},
            "agents": [],
            "topologies": [{"kind": "pipeline", "status": "failed", "items": 1, "stages": 1, "agent_ids": []}],
        },
    }

    assert "✗ pipeline" in render_terminal_task_snapshot(run, show_cost=False)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run from the plugin checkout with the Hermes virtualenv and no ambient pytest addopts:

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_display.py -k 'stable_workflow_header or terminal_task_snapshot or failed_topology_row' -q -o 'addopts='
```

Expected: FAIL because `render_workflow_header` and `render_terminal_task_snapshot` do not yet exist, and the detailed terminal header still uses `status_emoji`.

- [ ] **Step 3: Implement the shared header and tree-only terminal renderer**

Add the pure header helper beside `render_run_progress` and make detailed progress use it. Do not include agent count in this identity header; the stable contract carries workflow name, duration, estimated cost, and token total only. Keep the existing overview (`detailed=False`) status presentation unchanged.

```python
def render_workflow_header(run: dict[str, Any], *, show_cost: bool = True) -> str:
    snapshot = run.get("workflow") or {}
    meta = snapshot.get("meta") or {}
    name = _bounded_text(meta.get("name") or run.get("source", {}).get("ref") or "workflow", _PROGRESS_NAME_MAX_CHARS)
    parts: list[str] = []
    duration = _duration(run, snapshot)
    if duration:
        parts.append(_format_duration(duration))
    if show_cost:
        cost = _format_cost(_total_cost(_all_agents(snapshot)))
        if cost:
            parts.append(cost)
    tokens = _totals(snapshot).get("tokens")
    if tokens:
        parts.append(f"~{_format_tokens(tokens)} tok")
    return f"🔄 {name}" + (f" · {' · '.join(parts)}" if parts else "")


def render_terminal_task_snapshot(run: dict[str, Any], *, show_cost: bool = True) -> str:
    return render_run_progress(run, show_cost=show_cost)
```

Inside `_render_run_block`, select `render_workflow_header(run, show_cost=show_cost)` only for `detailed=True`; leave the compact `/workflows` overview’s `status_emoji` line as-is. Keep `render_run_progress`’s detailed roster and phase checklist paths, because they already use `_detail_marker` and `_agent_marker` with `✓` and `✗`. Change `_topology_marker` so `failed`, `error`, and `blocked` return `✗`, `done` and `completed` return `✓`, and active states return `▶`; do not add `✅` or `❌` to any task-tree path. Add the stable header to the manager’s terminal-snapshot call site in Task 9, not to the rich completion card.

- [ ] **Step 4: Run the focused tests and verify GREEN**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_display.py -k 'stable_workflow_header or terminal_task_snapshot or failed_topology_row' \
  tests/test_run_manager.py -k 'progress or completion' -q -o 'addopts='
```

Expected: PASS. Confirm the old completion-card tests still pass because `_progress_bubble_text(..., completed=True)` has not been changed in this task.

- [ ] **Step 5: Commit Task 8**

```bash
git add hermes_dynamic_workflows/view/render.py hermes_dynamic_workflows/run/manager.py \
  tests/test_display.py tests/test_run_manager.py
git commit -m "feat: add stable workflow task headers"
```

### Task 9: Split completion delivery and make result sends idempotent

**Files:**
- Modify: `hermes_dynamic_workflows/run/manager.py:70-106,235-286,842-877,1525-1628,1719-1771,1925-2143,2263-2269`
- Modify: `hermes_dynamic_workflows/view/completion.py:712-940` to add `max_units: int = 4096` and `include_metrics: bool = True` to `render_completion_card`; default rendering and its rich hierarchy remain unchanged.
- Test: `tests/test_run_manager.py` existing gateway progress tests around lines 1189-1819, plus new send/idempotency tests

**Interfaces:**
- Adds `ManagedRun.result_send_in_flight: bool` and `ManagedRun.store: WorkflowStore | None`; production construction supplies `self.store`, while hand-built unit fixtures supply a temporary store when persistence is under test.
- Adds run-record fields initialized at launch: `resultMessageId: None`, `resultMessageDelivered: False`, and `resultMessageError: None`.
- Changes `_send_gateway_text(...)` to return `_GatewaySendAttempt(confirmed, message_id, future)` and to accept an optional `buttons` argument. Default result sends do not pass `buttons`; seed/edit paths pass buttons only after the existing capability probe.
- Adds `_deliver_result_message(managed, config, session_context, *, block: bool) -> bool`. It reserves the send under `managed.lock`, releases the lock before scheduling or waiting on the adapter, writes `resultMessageId` or `resultMessageDelivered` only after confirmed success, and clears the reservation on failure so a later completion callback can retry.
- Changes `_edit_progress_bubble(..., completed=True)` to render only `render_terminal_task_snapshot`; it never receives or emits `render_completion_card` output. A nonblocking final edit callback invokes `_deliver_result_message(..., block=False)` only after the edit future resolves, while the worker-thread path uses `block=True` outside any managed lock.
- Replaces `_send_gateway_completion_notification(...)` with a thin compatibility wrapper around `_deliver_result_message(..., block=True)` or removes its only call site; no completion path may call `_send_gateway_text` directly for the result card. `_notify_completion` and the slow-seed callback both go through the idempotent helper.
- Changes `_render_gateway_completion_message` to import and use `render_workflow_header` plus the existing `_card_formatter_units` budget helper, returning `render_workflow_header(record) + "\\n\\n" + render_completion_card(..., max_units=remaining, include_metrics=False)`; the card receives only the remaining budget, keeps all rich content/budget behavior, and emits neither `render_run_metrics` nor `render_cost_breakdown`, so the combined source and adapter-formatted message remains within 4096 UTF-16 units.

- [ ] **Step 1: Write failing delivery, persistence, and failure-path tests**

Replace the old one-message expectations in `test_live_progress_bubble_seeds_then_finalizes_one_message`, `test_completion_edit_flood_failure_falls_back_to_fresh_send`, `test_completion_edit_flood_recovers_even_when_notify_on_complete_false`, `test_completion_edit_success_with_notify_on_complete_false_no_send`, and `test_live_progress_bubble_slow_seed_finalizes_via_callback_no_duplicate` with the separate-message contract. Add these focused assertions to the gateway test fixture:

```python
def test_terminal_edit_precedes_distinct_result_send_and_persists_message_id(self):
    events = []

    class Adapter:
        async def send(self, chat_id, content, metadata=None, buttons=None):
            events.append(("send", content, buttons))
            return SimpleNamespace(success=True, message_id=f"result-{len(events)}")

        async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None, buttons=None):
            events.append(("edit", content, buttons, finalize))
            return SimpleNamespace(success=True, message_id=message_id)

    # Use the existing gateway-run harness with notify_progress_cost=True and wait for the terminal record.
    final, store = self._run_gateway_fixture(Adapter(), notify_progress=True, notify_progress_cost=True)

    self.assertEqual(final["status"], "completed")
    self.assertEqual([event[0] for event in events], ["send", "edit", "send"])
    seed, terminal_edit, result = events
    self.assertTrue(terminal_edit[3])
    self.assertEqual(terminal_edit[2], [])
    self.assertTrue(terminal_edit[1].startswith("🔄 bubble · "))
    self.assertNotIn("**", terminal_edit[1])
    self.assertNotIn("Result:", terminal_edit[1])
    self.assertTrue(result[1].startswith("🔄 bubble · "))
    result_lines = result[1].splitlines()
    terminal_lines = terminal_edit[1].splitlines()
    self.assertEqual(result_lines[0], terminal_lines[0])
    self.assertEqual(result_lines[1], "")
    self.assertIn("**", result[1])
    self.assertIsNone(result[2])
    header = result_lines[0]
    card_body = "\n".join(result_lines[2:])
    for segment in ("9m 57s", "~$1.05", "~1.86M tok"):
        self.assertEqual(header.count(segment), 1)
        self.assertEqual(result[1].count(segment), 1)
        self.assertNotIn(segment, card_body)
    persisted = store.load_run(final["runId"])
    self.assertEqual(persisted["resultMessageId"], "result-3")
    self.assertTrue(persisted["resultMessageDelivered"] is False)


def test_duplicate_completion_callbacks_send_one_result(self):
    adapter = self._successful_send_only_adapter(message_id="stable-result")
    managed, config, context, store = self._managed_gateway_result_fixture(adapter)

    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))
    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))

    self.assertEqual(adapter.sent_count, 1)
    self.assertEqual(store.load_run(managed.run_id)["resultMessageId"], "stable-result")


def test_success_without_message_id_uses_durable_delivered_marker(self):
    adapter = self._successful_send_only_adapter(message_id=None)
    managed, config, context, store = self._managed_gateway_result_fixture(adapter)

    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))
    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))

    persisted = store.load_run(managed.run_id)
    self.assertIsNone(persisted["resultMessageId"])
    self.assertTrue(persisted["resultMessageDelivered"])
    self.assertEqual(adapter.sent_count, 1)


def test_result_send_failure_keeps_terminal_record_retryable(self):
    adapter = self._failure_then_success_send_adapter()
    managed, config, context, store = self._managed_gateway_result_fixture(adapter, status="failed")

    self.assertFalse(manager_module._deliver_result_message(managed, config, context, block=True))
    failed = store.load_run(managed.run_id)
    self.assertEqual(failed["status"], "failed")
    self.assertIsNone(failed["resultMessageId"])
    self.assertFalse(failed["resultMessageDelivered"])
    self.assertIsNotNone(failed["resultMessageError"])

    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))
    self.assertEqual(adapter.sent_count, 2)
    self.assertEqual(store.load_run(managed.run_id)["resultMessageId"], "retry-result")


def test_terminal_edit_failure_still_sends_result_without_overwriting_execution_body(self):
    adapter = self._terminal_edit_failure_adapter()
    final, store = self._run_gateway_fixture(adapter, notify_progress=True)

    self.assertEqual(final["status"], "completed")
    terminal_edits = [event for event in adapter.events if event[0] == "edit" and event[3]]
    result_sends = [event for event in adapter.events if event[0] == "send" and "**" in event[1]]
    self.assertEqual(len(terminal_edits), 1)
    self.assertEqual(len(result_sends), 1)
    self.assertNotIn("**", terminal_edits[0][1])
    self.assertIn("**", result_sends[0][1])
    self.assertTrue(store.load_run(final["runId"])["resultMessageId"])


def test_non_editable_seed_falls_back_to_launch_marker_and_one_separate_result(self):
    adapter = self._send_only_adapter_without_message_id()
    final, store = self._run_gateway_fixture(adapter, notify_progress=True)

    self.assertEqual(final["status"], "completed")
    self.assertEqual(len(adapter.sent), 2)
    self.assertIn("Workflow started", adapter.sent[0][1])
    self.assertTrue(adapter.sent[1][1].startswith("🔄 "))
    self.assertIn("\n\n", adapter.sent[1][1])
    self.assertIsNone(adapter.sent[1][2])
    self.assertTrue(store.load_run(final["runId"])["resultMessageDelivered"])


def test_result_send_exception_is_retryable(self):
    adapter = self._exception_then_success_send_adapter()
    managed, config, context, store = self._managed_gateway_result_fixture(adapter)

    self.assertFalse(manager_module._deliver_result_message(managed, config, context, block=True))
    self.assertIsNone(store.load_run(managed.run_id)["resultMessageId"])
    self.assertTrue(manager_module._deliver_result_message(managed, config, context, block=True))
    self.assertEqual(store.load_run(managed.run_id)["resultMessageId"], "exception-retry-result")
```

Define the adapter fixtures as test-only helpers in `tests/test_run_manager.py`: `_successful_send_only_adapter(message_id)` records sends and returns `SimpleNamespace(success=True, message_id=message_id)`; `_failure_then_success_send_adapter()` returns `SimpleNamespace(success=False, error="flood_control:30")` once and then `SimpleNamespace(success=True, message_id="retry-result")`; `_exception_then_success_send_adapter()` raises `RuntimeError("gateway unavailable")` once and then returns `SimpleNamespace(success=True, message_id="exception-retry-result")`; `_send_only_adapter_without_message_id()` exposes `async def send(...)`, no `edit_message`, and returns `SimpleNamespace(success=True)`; `_terminal_edit_failure_adapter()` returns `SimpleNamespace(success=False, error="flood_control:30")` only for `finalize=True` and a successful result for the result send. `_managed_gateway_result_fixture` must create a temporary `WorkflowStore`, save a terminal record with the three result-delivery fields, construct `ManagedRun(..., store=store)`, install the existing fake gateway runner/synchronous `safe_schedule_threadsafe` from the surrounding tests, and return `(managed, PluginConfig(...), session_context, store)`. `_run_gateway_fixture(adapter, notify_progress, notify_progress_cost)` must reuse the existing `WorkflowRunManager.start_from_params` harness in the neighboring gateway tests, pass the supplied adapter, progress flags, and cost-display setting, wait for the run, and return `(final_record, store)`.

Add these renderer-level assertions to `CompletionCardRenderTests` so the wrapper contract is independent of gateway delivery:

```python
def test_wrapped_renderer_suppresses_metrics_and_cost_breakdown(self):
    from unittest.mock import patch
    from hermes_dynamic_workflows.view import completion as completion_module

    record = self._blocked_review_record()
    with patch.object(completion_module, "render_run_metrics") as render_metrics, \
            patch.object(completion_module, "render_cost_breakdown") as render_breakdown:
        text = completion_module.render_completion_card(
            record,
            preview_chars=1200,
            show_cost=True,
            max_units=4096,
            include_metrics=False,
        )

    render_metrics.assert_not_called()
    render_breakdown.assert_not_called()
    self.assertNotIn("5.04M tokens", text)


def test_default_renderer_retains_metrics_footer(self):
    from hermes_dynamic_workflows.view import completion as completion_module

    text = completion_module.render_completion_card(
        self._blocked_review_record(),
        preview_chars=1200,
        show_cost=False,
    )

    self.assertIn("5.04M tokens", text)
```

- [ ] **Step 2: Run the delivery tests and verify RED**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py -k 'terminal_edit_precedes or duplicate_completion or without_message_id or result_send_failure or terminal_edit_failure or non_editable_seed or live_progress_bubble' \
  -q -o 'addopts='
```

Expected: FAIL because the current code edits the rich card into the execution message, suppresses the separate send after a successful edit, does not persist a result message id, and still emits the old slow-seed/flood expectations.

- [ ] **Step 3: Add persistent result-delivery state and a nonblocking send outcome**

Initialize the three record keys beside `result`, and construct `ManagedRun(store=self.store)`. Add the transient reservation field. Use the following shape; all network operations occur after the reservation lock is released:

```python
@dataclass(frozen=True)
class _GatewaySendAttempt:
    confirmed: bool
    message_id: str | None = None
    future: Any = None


def _gateway_result_ok(result: Any) -> bool:
    success = getattr(result, "success", None)
    return True if success is None else bool(success)


def _gateway_attempt_from_future(future: Any) -> _GatewaySendAttempt:
    try:
        result = future.result()
    except Exception:
        return _GatewaySendAttempt(confirmed=False)
    return _GatewaySendAttempt(
        confirmed=_gateway_result_ok(result),
        message_id=str(getattr(result, "message_id", "") or "") or None,
    )


def _deliver_result_message(
    managed: "ManagedRun",
    config: PluginConfig,
    session_context: dict[str, str] | None,
    *,
    block: bool,
) -> bool:
    with managed.lock:
        record = managed.record
        if record.get("resultMessageId") or record.get("resultMessageDelivered"):
            return True
        if managed.result_send_in_flight:
            return False
        managed.result_send_in_flight = True

    try:
        attempt = _send_gateway_text(
            record,
            session_context,
            _render_gateway_completion_message(record, config),
            block=block,
        )
        if not block and attempt.future is not None:
            attempt.future.add_done_callback(
                lambda future: _finish_result_delivery(
                    managed, _gateway_attempt_from_future(future)
                )
            )
            return False
        return _finish_result_delivery(managed, attempt)
    except Exception as exc:
        return _finish_result_delivery(managed, _GatewaySendAttempt(confirmed=False), exc)


def _finish_result_delivery(
    managed: "ManagedRun",
    attempt: _GatewaySendAttempt,
    error: BaseException | None = None,
) -> bool:
    with managed.lock:
        record = managed.record
        if attempt.confirmed:
            if attempt.message_id:
                record["resultMessageId"] = attempt.message_id
            else:
                record["resultMessageDelivered"] = True
            record["resultMessageError"] = None
        else:
            record["resultMessageError"] = str(error or "gateway result send was not confirmed")
        managed.result_send_in_flight = False
        if managed.store is not None:
            managed.store.save_run(record)
        return attempt.confirmed
```

Update `_send_gateway_text` so it returns `_GatewaySendAttempt`: resolve the target, create `send_kwargs = {"metadata": metadata}`, add `buttons` only when the caller supplied it and `_accepts_buttons(adapter.send)` is true, schedule `adapter.send(chat_id, text, **send_kwargs)`, and return `_GatewaySendAttempt(future=future)` for `block=False`. For `block=True`, call `future.result(timeout=15)` outside every managed lock and return `_gateway_attempt_from_future(future)`. A missing target, missing future, raised send, or explicit `success=False` returns `confirmed=False`; a successful result without `message_id` remains confirmed and is persisted through `resultMessageDelivered=True`.

- [ ] **Step 4: Split terminal edit text from result-card text and preserve the card budget**

Change `_edit_progress_bubble` to select `render_terminal_task_snapshot(managed.record, show_cost=config.notify_progress_cost)` when `completed=True`; keep `_progress_bubble_text(..., completed=True)` for rich result-card tests. Do not set `resultMessageId` from an edit. Pass `buttons=[]` on the terminal edit even when `_control_buttons_for` returns `None`, so stale Pause/Resume/Stop/Restart keyboards are cleared; when a valid log URL exists, Task 10 will make the terminal edit retain only that Open log row.

Make the existing helper use this exact signature:

```text
def render_completion_card(
    record: dict[str, Any],
    *,
    preview_chars: int,
    show_cost: bool,
    max_units: int = 4096,
    include_metrics: bool = True,
) -> str:
```

Thread `max_units` through its existing `_card_text_fits`, `_card_blocks_fit`, result-budget, metrics, and cost-breakdown checks. When `include_metrics=False`, do not call or append `render_run_metrics` or `render_cost_breakdown`; preserve all rich content and budget behavior. The default `include_metrics=True` and `max_units=4096` retain the existing footer/cost-breakdown behavior and all Task 6 `CompletionCardRenderTests` assertions.

Render the separate message as:

```python
def _render_gateway_completion_message(record: dict[str, Any], config: PluginConfig) -> str:
    header = render_workflow_header(record, show_cost=config.notify_progress_cost)
    header_units = _card_formatter_units(f"{header}\n\n")
    remaining = max(0, 4096 - header_units)
    card = render_completion_card(
        record,
        preview_chars=config.notify_result_preview_chars,
        show_cost=config.notify_progress_cost,
        max_units=remaining,
        include_metrics=False,
    )
    return f"{header}\n\n{card}" if card else header
```

The result send calls `_send_gateway_text` without a `buttons` argument. Thus the original execution edit owns the optional Open log button and the result message has no keyboard. The stable header helper is the only source of the first line and of duration/cost/token metrics in the result message; the card body has no internal metrics footer or cost breakdown.

- [ ] **Step 5: Rework completion ordering, slow-seed callbacks, and retry behavior**

Keep the existing bounded seed wait and wake-event logic, but replace the old “bubble finalized means no fresh send” branch with this exact ordering:

```python
if active:
    # Worker thread: blocking is safe here, and the lock is not held.
    _edit_progress_bubble(managed, config, completed=True, force=True, block=True)
    _deliver_result_message(managed, config, session_context, block=True)
elif requested:
    # The seed callback owns both operations after the seed future resolves.
    bubble_pending = True
else:
    # No active/editable seed: launch-marker fallback remains independent.
    _deliver_result_message(managed, config, session_context, block=True)
```

The result delivery call is not gated by `notify_on_complete`; the gateway completion artifact must still exist when a progress bubble or launch-marker fallback is configured. Keep `notify_on_complete` as the gate for the task-notification injection and gateway wake event. If the terminal edit returns `success=False`, raises, has no editable target, or is skipped because no seed exists, still call `_deliver_result_message`; never replace the execution message with result-card text. A failed result send records `resultMessageError`, leaves both durable success markers unset, and returns without changing terminal `status`, so a later retry can call the same helper.

In `_on_seeded`, when the run is already terminal, schedule the final edit with `block=False` and attach a done callback. The callback must inspect the already-completed edit future without waiting, then invoke `_deliver_result_message(..., block=False)`. If the edit cannot be scheduled, invoke the same result helper immediately with `block=False`. The result send’s future callback calls `_finish_result_delivery`; it must not call `.result()` on a coroutine scheduled onto the current gateway loop. This preserves the existing gateway-loop deadlock protection for both slow-seed and flood paths.

- [ ] **Step 6: Run focused delivery tests and verify GREEN**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_run_manager.py -k 'terminal_edit_precedes or duplicate_completion or without_message_id or result_send_failure or terminal_edit_failure or non_editable_seed or live_progress_bubble or completion_edit_flood' \
  tests/test_run_manager.py::CompletionCardRenderTests -q -o 'addopts='
```

Expected: PASS. Confirm the same workflow header is byte-for-byte identical in the terminal edit and result send; each duration/cost/token segment occurs exactly once in the complete result message and only in its first header line; the result body retains the existing rich-card formatting without an internal metrics footer or cost breakdown; the default `CompletionCardRenderTests` still retain their footer; both raw and adapter-formatted result messages fit the combined 4096 UTF-16 budget; and the stored full result remains unchanged.

- [ ] **Step 7: Commit Task 9**

```bash
git add hermes_dynamic_workflows/run/manager.py hermes_dynamic_workflows/view/completion.py \
  tests/test_run_manager.py
git commit -m "feat: deliver workflow results separately"
```

### Task 10: Remove Telegram Restart controls and run complete verification

**Files:**
- Modify: `hermes_dynamic_workflows/run/manager.py:1876-1909` to remove the active-state Restart button only; keep restart/rerun callback and backend methods unchanged.
- Test: `tests/test_gateway_callback.py:105-201`
- Test: `tests/test_run_manager.py` result-button and terminal-edit assertions
- Test: `tests/test_display.py` final task-tree glyph assertions
- No Hermes core files; no dependency files; no `uv.lock` or `.superpowers/`.

**Interfaces:**
- `_control_buttons_for` keeps Pause for queued/running, Resume for paused, Stop for queued/running/paused, and Open log when a valid URL exists. It never emits `wf:restart` or `wf:rerun` in any state.
- `on_gateway_callback` continues accepting authorized `wf:restart:<runId>` and `wf:rerun:<runId>` callbacks so backend, CLI, TUI, and explicit command support remain intact.
- Result sends always omit `buttons`; terminal execution edits clear active controls and retain only Open log.

- [ ] **Step 1: Write failing Restart-absence and result-button tests**

Replace the active-state expectations in `tests/test_gateway_callback.py` with:

```python
def test_running_control_buttons_keep_pause_and_stop_but_no_restart(self):
    buttons = _control_buttons_for(self._record(status="running"), PluginConfig())
    callbacks = [button["callback_data"] for button in buttons]
    self.assertIn("wf:pause:wf_abc123", callbacks)
    self.assertIn("wf:stop:wg123", callbacks)
    self.assertFalse(any("restart" in callback or "rerun" in callback for callback in callbacks))


def test_paused_control_buttons_keep_resume_and_stop_but_no_restart(self):
    buttons = _control_buttons_for(self._record(status="paused"), PluginConfig())
    callbacks = [button["callback_data"] for button in buttons]
    self.assertIn("wf:resume:wf_abc123", callbacks)
    self.assertIn("wf:stop:wg123", callbacks)
    self.assertFalse(any("restart" in callback or "rerun" in callback for callback in callbacks))


def test_no_restart_or_rerun_button_in_any_telegram_state(self):
    statuses = ("queued", "running", "paused", "stopping", "completed", "failed", "error", "stopped", "interrupted")
    for status in statuses:
        buttons = _control_buttons_for(self._record(status=status), PluginConfig()) or []
        rows = buttons if buttons and isinstance(buttons[0], list) else [buttons]
        callbacks = [button.get("callback_data", "") for row in rows for button in row]
        self.assertFalse(any("restart" in callback or "rerun" in callback for callback in callbacks), status)


def test_terminal_open_log_is_the_only_execution_control(self):
    record = self._record(status="completed")
    record["logUrl"] = "https://example.com/log"
    self.assertEqual(
        _control_buttons_for(record, PluginConfig()),
        [{"text": "📄 Open log", "url": "https://example.com/log"}],
    )


def test_restart_callbacks_remain_supported_without_a_telegram_button(self):
    class FakeManager:
        def restart(self, run_id):
            return {"runId": "wf_new123"}

    with patch.object(gc_module, "get_run_manager", return_value=FakeManager()):
        directive = on_gateway_callback(data="wf:restart:wf_old123", authorized=True)
    self.assertIn("wf_new123", directive["answer"])
    self.assertTrue(directive["strip_buttons"])
```

- [ ] **Step 2: Run control tests and verify RED**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_gateway_callback.py -k 'control_buttons or restart_callbacks' -q -o 'addopts='
```

Expected: FAIL because `_control_buttons_for` still appends `wf:restart:<runId>` for active states.

- [ ] **Step 3: Remove only the Telegram Restart branch**

Delete the active-state branch:

```python
if status in _ACTIVE_CONTROL_STATES and run_id:
    controls.append({"text": "🔄 Restart", "callback_data": f"wf:restart:{run_id}"})
```

Do not delete `_ACTIVE_CONTROL_STATES`, callback parsing, `restart()`, `rerun`, persistence, or TUI controls. Keep the terminal control behavior from Task 9: a terminal execution edit passes the optional Open log row or `[]`, and the result send passes no buttons.

- [ ] **Step 4: Run focused and full suites and verify GREEN**

```bash
cd /Users/atorres/Documents/GitHub/hermes-dynamic-workflows
env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/test_gateway_callback.py tests/test_run_manager.py tests/test_display.py -q -o 'addopts='

env -i HOME="$HOME" PATH="/Users/atorres/.hermes/hermes-agent/venv/bin:/usr/bin:/bin" \
  /Users/atorres/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q -o 'addopts='
```

Expected: PASS. The full suite must retain callback/backend restart coverage while proving no Telegram state contains a Restart/Rerun button, result messages have no buttons, terminal execution messages have only optional Open log, and the current rich-card tests still pass.

- [ ] **Step 5: Run live Telegram canaries**

Restart the serving gateway from a separate controlling shell, then verify one healthy post-change process:

```bash
hermes gateway restart
hermes gateway status
pgrep -af 'hermes_cli.main gateway run'
```

Run a mixed-result workflow returning a string, `None`, and a failed dictionary. Read back both messages: the original execution message starts with the stable `🔄` header, ends as a task tree with `✓`/`✗`, has no rich result body, and retains only optional Open log; the separate result begins with the byte-identical header, has a blank line, contains the existing rich card with no internal metrics footer or cost breakdown, and has each duration/cost/token segment exactly once in the header and nowhere in the card body. Verify the result has no buttons. Run a 100-item overflow workflow and verify both messages remain available, each stays within the adapter’s 4096 UTF-16 limit, and the persisted artifact retains every item. Run the clarification canary from Task 5 and verify the final question includes every choice without relying on commentary.

- [ ] **Step 6: Commit Task 10**

```bash
git add hermes_dynamic_workflows/run/manager.py tests/test_gateway_callback.py \
  tests/test_run_manager.py tests/test_display.py
git commit -m "fix: remove Telegram workflow restart button"
```

## Follow-up plan self-review

- [ ] **Spec coverage:** Telegram Message Lifecycle is covered by Tasks 8–9; Task Markers by Task 8; Telegram Controls by Task 10; Delivery and controls by Tasks 9–10; standalone/default metrics-footer and cost-breakdown compatibility by Tasks 2, 6, and 9; wrapped-result header-only duration/cost/token metrics and the combined 4096 rich-card budget by Task 9; no-core/no-dependency scope by the follow-up constraints; adapter id/no-id/failure behavior by Task 9; and live canaries by Task 10 Step 5.
- [ ] **Placeholder scan:** scan this file for unfinished-task markers and vague implementation language, excluding the scan command itself. The check must return no matching lines:

```bash
python3 - <<'PY'
from pathlib import Path

text = Path("docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md").read_text(encoding="utf-8")
needles = ["T" + "BD", "TO" + "DO", "FIX" + "ME", "implement " + "later", "fill in " + "details", "add " + "appropriate"]
matches = [line for line in text.splitlines() if any(needle in line for needle in needles)]
if matches:
    raise SystemExit("placeholder/vague-plan matches:\n" + "\n".join(matches))
print("placeholder scan: PASS")
PY
```

- [ ] **Type and snippet consistency:** verify the names used across Tasks 8–10 are exactly `render_workflow_header`, `render_terminal_task_snapshot`, `_GatewaySendAttempt`, `_gateway_result_ok`, `_gateway_attempt_from_future`, `_deliver_result_message`, `_finish_result_delivery`, `resultMessageId`, `resultMessageDelivered`, and `resultMessageError`. Extract each newly added fenced `python` block after `## Follow-up scope` and compile it with `ast.parse`; fix any syntax error in the plan before implementation begins.
- [ ] **Lifecycle contradiction check:** confirm no follow-up test expects the execution edit to contain `render_completion_card`, `Result:`, or a rich-card heading; confirm every terminal path calls the idempotent result helper exactly once or leaves it retryable; confirm the old Task 5/Task 7 “one edited completion card” wording is explicitly superseded above.
- [ ] **Scope and diff check:** after writing this plan, run `git diff --check`, inspect the exact diff, and verify only this plan is staged. The pre-existing untracked `.superpowers/` and `uv.lock` must remain untouched.

```bash
git diff --check -- docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md
git diff --stat -- docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md
git status --short
```

The implementation worker must run the focused RED command before each production change, the corresponding GREEN command after it, and the full suite/live canaries before publication. This plan’s publication commit is separate from the future implementation commits: the current docs-only handoff is committed as `docs: plan separate workflow results`.

For this docs-only handoff, force-add exactly the ignored/selected plan path, inspect the staged file list, and commit only the plan:

```bash
git add -f docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md
test "$(git diff --cached --name-only)" = "docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md"
git diff --cached --check
git diff --cached --stat -- docs/superpowers/plans/2026-07-20-human-friendly-workflow-results.md
git commit -m "docs: plan separate workflow results"
```
