# Native Codex Workflow Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native async `codex()` workflow primitive that invokes the existing deterministic Codex launcher as a cached, cancellable workflow worker stage.

**Architecture:** `WorkflowAPI` validates and tracks each Codex stage; a focused runner module owns subprocess lifecycle and receipt parsing; `codex-coder.py` remains sole owner of repository safety, sandbox, allowlist, and postflight gates. Existing `AgentRecord`, topology, resume cache, concurrency semaphore, journal, and progress renderer carry the stage without a second execution graph.

**Tech Stack:** Python 3.11, asyncio, subprocess, unittest/pytest, Dynamic Workflows sandbox/runtime, existing `~/.hermes/scripts/codex-coder.py` protocol.

## Global Constraints

- Reuse `codex-coder.py`; do not implement a second Codex runtime.
- Invoke subprocesses with argv and `shell=False`; never expose arbitrary executable or environment options.
- Support only `code`, `discover`, `debug`, and `verify` modes.
- Direct parent Codex remains preferred for one bounded stage without orchestration semantics.
- No commits, pushes, rebases, tests, reviews, publication, or arbitrary shell stages inside `codex()`.
- Validate workflow-facing options before worker reservation and process launch.
- Include canonical workdir and starting Git `HEAD` in resume identity.
- Stop/deadline cancellation must terminate the launcher process group.
- Preserve unrelated baseline failures: three pricing-render expectations and one missing-session-id expectation.

---

### Task 1: Codex Runner Contract and Process Lifecycle

**Files:**
- Create: `hermes_dynamic_workflows/engine/codex.py`
- Create: `tests/test_codex_runner.py`

**Interfaces:**
- Consumes: existing `codex-coder.py` CLI and JSON receipt schema.
- Produces: `CodexStageRequest`, `CodexStageRunner`, `SubprocessCodexStageRunner.run(request, stop_event, deadline) -> dict[str, Any]`, and `codex_stage_fingerprint_inputs(request, start_head) -> dict[str, Any]`.

- [ ] **Step 1: Write failing runner tests**

Cover exact argv construction, external contract/output paths, receipt parsing, unsuccessful receipt rejection, malformed/missing receipt rejection, non-zero exit handling, process-group cancellation, and timeout. Use a temporary fake launcher and temporary Git repository; never invoke a real provider in unit tests.

Expected API:

```python
request = CodexStageRequest(
    mode="code",
    workdir=repo,
    contract="PARENT-LOCKED IMPLEMENTATION CONTRACT\n...",
    allow_files=("src/example.py",),
    timeout=30.0,
    model="gpt-5.6-luna",
    reasoning="high",
)
receipt = SubprocessCodexStageRunner(launcher=fake_launcher).run(
    request,
    stop_event=threading.Event(),
    deadline=time.monotonic() + 60,
)
assert receipt["success"] is True
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest tests/test_codex_runner.py -q
```

Expected: collection/import failure because `hermes_dynamic_workflows.engine.codex` does not exist.

- [ ] **Step 3: Implement minimal runner**

Implement immutable request validation data, launcher resolution through `get_hermes_home() / "scripts/codex-coder.py"`, temporary external artifacts, argv construction, process-group launch, cooperative stop/deadline checks, termination escalation, JSON receipt parsing, and stable `WorkflowRuntimeError` messages.

Runner command shape:

```python
argv = [
    sys.executable,
    str(launcher),
    "--mode", request.mode,
    "--workdir", request.workdir,
    "--contract-file", str(contract_path),
    "--model", request.model,
    "--reasoning", request.reasoning,
    "--timeout", str(request.timeout),
    "--json-output", str(output_path),
]
for path in request.allow_files:
    argv.extend(["--allow-file", path])
```

Use `start_new_session=True` on POSIX. On stop/deadline, terminate the process group and raise the existing `WorkflowStopped` or `WorkflowDeadlineExceeded` signal rather than wrapping it as a normal runtime error.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all `tests/test_codex_runner.py` tests pass.

- [ ] **Step 5: Commit runner slice**

```bash
git add hermes_dynamic_workflows/engine/codex.py tests/test_codex_runner.py
git commit -m "feat: add Codex workflow stage runner"
```

### Task 2: Workflow API, Resume, Progress, and Topology

**Files:**
- Modify: `hermes_dynamic_workflows/engine/api.py`
- Modify: `hermes_dynamic_workflows/engine/context.py`
- Modify: `hermes_dynamic_workflows/engine/runtime.py`
- Modify: `hermes_dynamic_workflows/core/types.py`
- Create: `tests/test_codex_runtime.py`

**Interfaces:**
- Consumes: Task 1 `CodexStageRequest` and runner.
- Produces: `await codex(opts: dict[str, Any]) -> dict[str, Any]` in workflow globals and injectable `WorkflowOptions.codex_runner`.

- [ ] **Step 1: Write failing workflow API tests**

Cover:

```python
script = '''
meta = {"name": "codex-stage", "description": "exercise native Codex"}
return await codex({
    "mode": "discover",
    "workdir": args["repo"],
    "contract": args["contract"],
    "label": "map repository",
    "phase": "Discover",
})
'''
```

Assertions:

- `codex` exists in workflow globals.
- missing/unknown mode, relative workdir, empty contract, invalid allowlist, mode/allowlist mismatch, and non-positive timeout launch zero runners and reserve zero workers;
- successful receipt is returned and recorded with `runner="codex-cli"`, `agent_type="codex-cli"`, provider/model/reasoning, usage, artifact handle, changed paths preview, and done status;
- unsuccessful execution records error and journals `started` then `error`;
- cache hit skips runner invocation;
- same inputs at changed starting `HEAD` miss cache;
- `parallel()` and `pipeline()` include Codex worker IDs in topology and obey shared concurrency;
- stop/deadline signals propagate as workflow halts.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest tests/test_codex_runtime.py -q
```

Expected: failures because `codex` is absent from globals and `WorkflowOptions` has no Codex runner.

- [ ] **Step 3: Implement minimal workflow integration**

Add `codex` to `WorkflowAPI.globals()`. Validate every public option before `context.reserve_agent()`. Resolve starting `HEAD` read-only, construct fingerprint via existing `agent_fingerprint()` with primitive marker and request inputs, then use existing `ResumeCache` and journal `v2:` convention.

Represent the stage with existing `AgentRecord`:

```python
record = AgentRecord(
    id=worker_id,
    label=label,
    phase=phase_name,
    prompt=contract,
    prompt_preview=preview(contract, 160),
    runner="codex-cli",
    agent_type="codex-cli",
    isolation="shared",
    provider="openai-codex",
    model="gpt-5.6-luna",
    reasoning_effort="high",
)
```

Use `context.agent_slot()` around the runner. Map launcher usage fields into existing token fields, call `context.record_tokens()`, preserve receipt directory in `transcript_path`, and update topology membership through the same context-local helpers as `agent()`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run Task 2 Step 2 command. Expected: all `tests/test_codex_runtime.py` tests pass.

- [ ] **Step 5: Run affected runtime regressions**

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest tests/test_runtime.py tests/test_sandbox.py tests/test_token_budget.py tests/test_display.py -q
```

Expected: new runtime tests pass; preserve the three documented pricing-render baseline failures without adding failures.

- [ ] **Step 6: Commit integration slice**

```bash
git add hermes_dynamic_workflows/engine/api.py \
  hermes_dynamic_workflows/engine/context.py \
  hermes_dynamic_workflows/engine/runtime.py \
  hermes_dynamic_workflows/core/types.py \
  tests/test_codex_runtime.py
git commit -m "feat: expose Codex stages to workflows"
```

### Task 3: Public Contract and Documentation

**Files:**
- Modify: `hermes_dynamic_workflows/adapters/workflow.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `README.ja-JP.md`
- Modify: `TECHNICAL.md`
- Create: `tests/test_codex_public_contract.py`

**Interfaces:**
- Consumes: Task 2 public `codex()` contract.
- Produces: model-facing workflow-tool instructions and user documentation that match runtime behavior.

- [ ] **Step 1: Write failing public-contract tests**

Assert `_DESCRIPTION` and each maintained document contain:

- `codex(opts)` signature;
- all four modes;
- `code` allowlist requirement;
- direct-parent route for single bounded stages;
- no `agent() -> Codex` nesting;
- no commit/push/rebase/review/test behavior;
- concise example matching actual option names.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest tests/test_codex_public_contract.py -q
```

Expected: failures because no public documentation mentions native `codex()`.

- [ ] **Step 3: Update tool description and documentation**

Add one concise hook entry and one example. Remove or replace text claiming workflows cannot contain a native Codex stage. Keep existing `agent()` guidance intact.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run Task 3 Step 2 command. Expected: all public-contract tests pass.

- [ ] **Step 5: Commit documentation slice**

```bash
git add hermes_dynamic_workflows/adapters/workflow.py README.md README.zh-CN.md README.ja-JP.md TECHNICAL.md tests/test_codex_public_contract.py
git commit -m "docs: document native Codex workflow stages"
```

### Task 4: Full Verification and Real Read-Only Smoke

**Files:**
- Modify only if a verified defect is found in Task 1–3 files.
- Test: all plugin tests plus real disposable-repository smoke.

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: deterministic verification evidence and one real launcher receipt without repository mutation.

- [ ] **Step 1: Run syntax and diff gates**

```bash
python -m py_compile \
  hermes_dynamic_workflows/engine/codex.py \
  hermes_dynamic_workflows/engine/api.py \
  hermes_dynamic_workflows/engine/context.py \
  hermes_dynamic_workflows/engine/runtime.py \
  hermes_dynamic_workflows/core/types.py \
  hermes_dynamic_workflows/adapters/workflow.py
git diff --check master...HEAD
git status --short
```

Expected: syntax passes, no whitespace errors, only planned files changed.

- [ ] **Step 2: Run focused native-Codex suite**

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest tests/test_codex_runner.py tests/test_codex_runtime.py tests/test_codex_public_contract.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full plugin suite**

```bash
PYTHONPATH="$PWD:/Users/atorres/.hermes/hermes-agent" uv run --project /Users/atorres/.hermes/hermes-agent --extra dev \
  python -m pytest -q
```

Expected: no regressions beyond the documented baseline of three pricing-render failures and one missing-session-id failure. Compare exact failing node IDs.

- [ ] **Step 4: Run real read-only workflow smoke**

Create a disposable clean Git repository with one committed file. Execute a workflow containing:

```python
return await codex({
    "mode": "discover",
    "workdir": args["repo"],
    "contract": "DISCOVERY CONTRACT\n# Goal\n...\n# Scope\n...\n# Output\n...\n# Stop triggers\n...",
    "timeout": 180,
})
```

Require:

- workflow completion;
- receipt `success is True`;
- `start_head == end_head`;
- `changed_paths == []`;
- non-empty `final_message` and resolved `usage`;
- disposable repository remains clean.

- [ ] **Step 5: Final frozen-diff review**

Inspect complete `master...HEAD` diff, changed-file list, receipt artifacts, and verification output. Reject scope drift, duplicated launcher logic, undocumented behavior, or tests that assert mocks instead of public behavior.

- [ ] **Step 6: Publish only after parent acceptance**

Merge the feature branch into local `master`, push normally to `alfredomtx/master`, verify remote SHA, restart gateway, and run one live Dynamic Workflow smoke. Update the local Dynamic Workflows skill to replace stale “no native Codex primitive” guidance, publish profile backup through the required backup helper, and verify both repositories by remote readback.
