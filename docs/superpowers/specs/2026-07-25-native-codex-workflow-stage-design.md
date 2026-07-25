# Native Codex Workflow Stage Design

## Goal

Add a small `codex()` workflow primitive that lets durable Dynamic Workflows invoke the existing deterministic Codex CLI launcher without wrapping Codex inside a Hermes child agent.

## Why

Direct Codex CLI is the preferred executor for bounded repository discovery, debugging evidence, implementation, and verification evidence. Dynamic Workflows currently exposes only `agent()`, so workflow-shaped tasks must choose between Codex efficiency and workflow durability. A native stage lets orchestration shape and executor shape remain independent.

## Public API

Workflow scripts receive one additional async global:

```python
result = await codex({
    "mode": "code",
    "workdir": "/absolute/repository",
    "contract": "PARENT-LOCKED IMPLEMENTATION CONTRACT\n...",
    "allowFiles": ["src/example.py", "tests/test_example.py"],
    "timeout": 900,
    "label": "implement example",
    "phase": "Implement",
})
```

Supported options:

- `mode`: required; one of `code`, `discover`, `debug`, or `verify`.
- `workdir`: required absolute Git workdir.
- `contract`: required non-empty contract text.
- `allowFiles`: required non-empty relative-path list for `code`; prohibited for read-only modes.
- `timeout`: optional positive number; defaults to 900 seconds and cannot exceed remaining workflow time.
- `label`: optional progress label; defaults to `codex:<mode>`.
- `phase`: optional progress phase; defaults to current phase.

Model and reasoning stay launcher-owned defaults (`gpt-5.6-luna`, `high`) in the first version. The workflow API does not add alternate provider configuration.

Return value is the validated launcher receipt as a JSON-compatible dictionary. A successful receipt includes `success`, `mode`, `start_head`, `end_head`, `changed_paths`, `usage`, `final_message`, `duration_seconds`, and artifact paths. A launcher failure raises `WorkflowRuntimeError` with its stable failure code/message while preserving the launcher receipt artifacts.

## Architecture

### Workflow API

`WorkflowAPI.codex()` validates the workflow-facing options before any process starts. It runs blocking work through `asyncio.to_thread()` and exposes progress through the existing workflow state.

### Codex runner

A focused runner module converts validated options into an argv call to `~/.hermes/scripts/codex-coder.py`. It writes the contract and requested JSON receipt into a temporary directory outside the target repository, invokes the launcher with `shell=False`, and parses the receipt file rather than trusting console prose.

The runner does not reimplement Git cleanliness checks, sandbox selection, mode contracts, allowlist enforcement, changed-path checks, `HEAD` checks, Codex JSONL validation, or receipt construction. Those remain exclusively owned by `codex-coder.py`.

`WorkflowOptions` accepts an injectable Codex runner for deterministic tests. Production resolves the default launcher through the active Hermes home rather than hardcoding Alfredo's home path.

### Progress and accounting

Each `codex()` call reserves one existing workflow worker record and one concurrency slot. The record uses:

- `runner="codex-cli"`
- `agent_type="codex-cli"`
- `provider="openai-codex"`
- launcher model and reasoning metadata
- launcher usage fields when present
- receipt directory as transcript/artifact handle

This keeps live progress and total worker accounting compatible without adding a second display tree. The stage participates in `parallel()` and `pipeline()` topology membership exactly like `agent()`.

### Resume identity

The resume fingerprint includes:

- primitive kind (`codex`)
- mode
- canonical workdir
- contract text
- ordered allowlist
- timeout
- starting Git `HEAD`

A cache hit returns the prior receipt without launching Codex. Including starting `HEAD` prevents replaying a result onto a different repository revision. The clean-worktree launcher gate remains authoritative for live execution.

### Cancellation and timeout

The runner starts the launcher in its own process group. While it runs, the runner checks the workflow stop event and deadline. Stop, workflow timeout, or stage timeout terminates the entire launcher process group, preserving any launcher artifacts already written. No Codex process may survive a stopped workflow.

The launcher already terminates its own Codex process group on its internal timeout. The workflow runner adds outer cancellation so the workflow stop control remains authoritative.

## Errors

Every workflow-facing validation error occurs before worker reservation or process launch:

- unknown/missing mode
- non-absolute workdir
- empty contract
- invalid allowlist shape
- missing allowlist in `code`
- allowlist present in read-only mode
- non-positive timeout

Runtime failures preserve artifacts and surface a concise error:

- launcher missing
- launcher non-zero exit
- malformed or missing receipt
- `success` false
- cancellation or workflow deadline

Scripts may catch normal `WorkflowRuntimeError`. Workflow stop/deadline signals remain uncatchable `WorkflowHalt` subclasses.

## Security boundaries

- No shell command strings; subprocess invocation uses argv and `shell=False`.
- No arbitrary executable option.
- No arbitrary environment injection.
- No commits, pushes, rebases, reviews, tests, or publication behavior.
- No nested `agent() -> Codex` route.
- Contract and receipt files live outside repository.
- Existing launcher gates remain fail-closed and authoritative.

## Documentation

Update the workflow tool description and project documentation to list `codex()` as a native workflow primitive, explain when to use it instead of `agent()`, and retain the rule that direct parent Codex remains preferable for a single bounded stage with no orchestration semantics.

Update the local Dynamic Workflows skill after publication so its prior “no native Codex primitive” guidance is removed and replaced with the shipped contract.

## Verification

TDD coverage must prove:

1. `codex` appears in sandbox globals and validates every public option before launch.
2. A fake launcher receives exact argv, contract content, external artifact paths, and `shell=False` behavior.
3. Success returns the parsed receipt and updates progress/usage metadata.
4. Non-zero, malformed, unsuccessful, timeout, and cancellation paths fail closed while preserving artifacts.
5. Resume caches by complete input plus starting `HEAD`; changed `HEAD` misses.
6. `parallel()` and `pipeline()` can run Codex stages under the shared concurrency cap.
7. Existing runtime, sandbox, manager, display, and full plugin suites remain green.
8. A real read-only smoke runs `codex({"mode": "discover", ...})` against a disposable clean Git repository and returns a valid launcher receipt without mutation.

## Rejected approaches

### Workflow child invokes Codex CLI

Rejected because it pays for two agent envelopes, obscures cost, and violates the direct-launcher ownership boundary.

### Reimplement Codex execution inside the plugin

Rejected because it duplicates launcher safety and receipt logic. The plugin must remain a thin adapter.

### Keep Codex outside workflows permanently

Rejected because repeated parent-managed handoffs undermine durable orchestration and create pressure to route eligible coding back through Hermes children.
