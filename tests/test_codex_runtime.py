from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from hermes_dynamic_workflows.core.config import PluginConfig
from hermes_dynamic_workflows.core.errors import (
    WorkflowDeadlineExceeded,
    WorkflowRuntimeError,
    WorkflowStopped,
)
from hermes_dynamic_workflows.core.types import WorkflowState
from hermes_dynamic_workflows.engine.cache import ResumeCache
from hermes_dynamic_workflows.engine.runtime import WorkflowOptions, run_workflow


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "example.py").write_text("answer = 42\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Codex tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _script(body: str) -> str:
    return 'meta = {"name": "codex-test", "description": "native codex"}\n' + body


def _opts(repo: Path, *, mode: str = "discover", **extra: object) -> dict[str, object]:
    options: dict[str, object] = {
        "mode": mode,
        "workdir": str(repo),
        "contract": "PARENT-LOCKED CONTRACT\n# Goal\nInspect the fixture.\n",
    }
    options.update(extra)
    return options


class FakeCodexRunner:
    def __init__(self, artifact_root: Path, *, changed_paths: list[str] | None = None):
        self.artifact_root = artifact_root
        self.changed_paths = changed_paths or []
        self.requests: list[object] = []
        self._lock = threading.Lock()

    def run(self, request, stop_event: threading.Event, deadline: float) -> dict[str, object]:
        assert not stop_event.is_set()
        assert deadline > time.monotonic()
        with self._lock:
            self.requests.append(request)
            index = len(self.requests)
        artifact_dir = self.artifact_root / str(index)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        contract_path = artifact_dir / "contract.txt"
        receipt_path = artifact_dir / "receipt.json"
        contract_path.write_text(request.contract, encoding="utf-8")
        receipt = {
            "success": True,
            "mode": request.mode,
            "start_head": "fixture-head",
            "end_head": "fixture-head",
            "changed_paths": list(self.changed_paths),
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "reasoning_output_tokens": 5,
                "total_tokens": 23,
                "tool_calls": 2,
                "cached_input_tokens": 3,
                "cache_write_input_tokens": 2,
            },
            "final_message": "fake Codex completed",
            "duration_seconds": 0.01,
            "artifact_paths": {
                "directory": str(artifact_dir),
                "contract": str(contract_path),
                "receipt": str(receipt_path),
            },
        }
        receipt_path.write_text("{}", encoding="utf-8")
        return receipt


class UnsuccessfulCodexRunner(FakeCodexRunner):
    def run(self, request, stop_event: threading.Event, deadline: float) -> dict[str, object]:
        super().run(request, stop_event, deadline)
        return {"success": False, "mode": request.mode, "final_message": "launcher refused"}


class HaltingCodexRunner:
    def __init__(self, halt: type[BaseException]):
        self.halt = halt
        self.started = threading.Event()

    def run(self, request, stop_event: threading.Event, deadline: float) -> dict[str, object]:
        self.started.set()
        while not stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise self.halt("codex stage halted")


class TrackingCodexRunner(FakeCodexRunner):
    def __init__(self, artifact_root: Path):
        super().__init__(artifact_root)
        self.active = 0
        self.max_active = 0

    def run(self, request, stop_event: threading.Event, deadline: float) -> dict[str, object]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            return super().run(request, stop_event, deadline)
        finally:
            with self._lock:
                self.active -= 1


def _run(script: str, runner, **kwargs):
    return run_workflow(
        script,
        WorkflowOptions(
            config=kwargs.pop("config", PluginConfig(require_launch_approval=False)),
            child_runner=object(),
            codex_runner=runner,
            **kwargs,
        ),
    )


def test_codex_is_a_global_and_success_updates_public_worker_metadata(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    artifact_root = tmp_path / "artifacts"
    runner = FakeCodexRunner(artifact_root, changed_paths=["src/example.py"])
    options = _opts(repo, mode="code", allowFiles=["src/example.py"], label="implement", phase="Implement")
    result = _run(_script(f"return await codex({options!r})"), runner)

    assert result.value["success"] is True
    assert result.value["changed_paths"] == ["src/example.py"]
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.mode == "code"
    assert request.workdir == str(repo.resolve())
    assert request.allow_files == ("src/example.py",)
    assert request.contract.startswith("PARENT-LOCKED CONTRACT")
    assert request.timeout == pytest.approx(900, abs=1)

    agent = result.state.snapshot()["agents"][0]
    assert agent["status"] == "done"
    assert agent["label"] == "implement"
    assert agent["phase"] == "Implement"
    assert agent["runner"] == "codex-cli"
    assert agent["agent_type"] == "codex-cli"
    assert agent["provider"] == "openai-codex"
    assert agent["model"] == "gpt-5.6-luna"
    assert agent["reasoning_effort"] == "high"
    assert agent["tokens"] == 23
    assert agent["input_tokens"] == 11
    assert agent["output_tokens"] == 7
    assert agent["reasoning_tokens"] == 5
    assert agent["cache_read_tokens"] == 3
    assert agent["cache_write_tokens"] == 2
    assert agent["tool_calls"] == 2
    assert agent["transcript_path"] == str(artifact_root / "1")
    assert "src/example.py" in agent["result_preview"]


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"mode": "unknown"},
        {"mode": "discover", "workdir": "relative/repo", "contract": "inspect"},
        {"mode": "discover", "workdir": "/tmp", "contract": ""},
        {"mode": "discover", "workdir": "/tmp", "contract": "inspect", "allowFiles": "src/a.py"},
        {"mode": "discover", "workdir": "/tmp", "contract": "inspect", "allowFiles": ["/absolute.py"]},
        {"mode": "code", "workdir": "/tmp", "contract": "implement", "allowFiles": ["../escape.py"]},
        {"mode": "code", "workdir": "/tmp", "contract": "implement", "allowFiles": ["."]},
        {"mode": "code", "workdir": "/tmp", "contract": "implement", "allowFiles": ["src/a.py", "src/a.py"]},
        {"mode": "code", "workdir": "/tmp", "contract": "implement"},
        {"mode": "code", "workdir": "/tmp", "contract": "implement", "allowFiles": []},
        {"mode": "discover", "workdir": "/tmp", "contract": "inspect", "allowFiles": ["src/a.py"]},
        {"mode": "discover", "workdir": "/tmp", "contract": "inspect", "timeout": 0},
        {"mode": "discover", "workdir": "/tmp", "contract": "inspect", "timeout": -1},
    ],
)
def test_invalid_public_options_fail_before_reservation_or_launch(
    tmp_path: Path, options: dict[str, object]
) -> None:
    runner = FakeCodexRunner(tmp_path / "artifacts")
    snapshots: list[WorkflowState] = []
    with pytest.raises(WorkflowRuntimeError):
        _run(_script(f"return await codex({options!r})"), runner, on_update=snapshots.append)
    assert runner.requests == []
    assert all(not snapshot.agents for snapshot in snapshots)


def test_unsuccessful_codex_execution_records_error_and_journal(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    runner = UnsuccessfulCodexRunner(tmp_path / "artifacts")
    events: list[dict[str, object]] = []

    with pytest.raises(WorkflowRuntimeError, match="success|launcher|refused"):
        run_workflow(
            _script(f"return await codex({_opts(repo)!r})"),
            WorkflowOptions(
                config=PluginConfig(require_launch_approval=False),
                child_runner=object(),
                codex_runner=runner,
                on_journal=events.append,
            ),
        )

    assert [event["type"] for event in events] == ["started", "error"]
    assert events[-1]["error"]


def test_resume_cache_requires_same_inputs_and_starting_head(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    cache = ResumeCache()
    first_runner = FakeCodexRunner(tmp_path / "first")
    script = _script(f"return await codex({_opts(repo)!r})")
    first = _run(script, first_runner, resume_cache=cache)

    cached_runner = FakeCodexRunner(tmp_path / "cached")
    cached = _run(script, cached_runner, resume_cache=ResumeCache(cache.current))
    assert cached.value == first.value
    assert cached_runner.requests == []

    identity_variants = (
        _opts(repo, mode="debug"),
        _opts(repo, contract="PARENT-LOCKED DIFFERENT CONTRACT"),
        _opts(repo, timeout=31),
        _opts(repo, mode="code", allowFiles=["src/example.py"]),
        _opts(repo, mode="code", allowFiles=["src/example.py", "README.md"]),
    )
    for index, variant in enumerate(identity_variants):
        variant_runner = FakeCodexRunner(tmp_path / f"variant-{index}")
        _run(_script(f"return await codex({variant!r})"), variant_runner, resume_cache=ResumeCache(cache.current))
        assert len(variant_runner.requests) == 1

    other_repo = _git_repo(tmp_path / "other")
    other_runner = FakeCodexRunner(tmp_path / "other-workdir")
    _run(_script(f"return await codex({_opts(other_repo)!r})"), other_runner, resume_cache=ResumeCache(cache.current))
    assert len(other_runner.requests) == 1

    (repo / "src" / "example.py").write_text("answer = 43\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/example.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repo, check=True)
    changed_head_runner = FakeCodexRunner(tmp_path / "changed-head")
    _run(script, changed_head_runner, resume_cache=ResumeCache(cache.current))
    assert len(changed_head_runner.requests) == 1


def test_parallel_and_pipeline_carry_codex_workers_in_topology_and_share_cap(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    runner = TrackingCodexRunner(tmp_path / "artifacts")
    first = _opts(repo, label="one")
    second = _opts(repo, label="two")
    script = _script(
        "results = await parallel([\n"
        f"    lambda: codex({first!r}),\n"
        f"    lambda: codex({second!r}),\n"
        "])\n"
        "pipeline_results = await pipeline([0, 1], lambda item, original, index: codex({\n"
        "    'mode': 'discover',\n"
        f"    'workdir': {str(repo)!r},\n"
        "    'contract': 'PARENT-LOCKED PIPELINE CONTRACT',\n"
        "    'label': 'pipeline-' + str(index),\n"
        "}))\n"
        "return [results, pipeline_results]"
    )
    result = _run(script, runner, config=PluginConfig(concurrency=1, require_launch_approval=False))

    assert len(result.value[0]) == 2
    assert len(result.value[1]) == 2
    assert runner.max_active == 1
    topologies = result.state.snapshot()["topologies"]
    assert any(topology["kind"] == "parallel" and len(topology["agent_ids"]) == 2 for topology in topologies)
    assert any(topology["kind"] == "pipeline" and len(topology["agent_ids"]) == 2 for topology in topologies)
    assert result.state.snapshot()["totals"]["agents"] == 4


@pytest.mark.parametrize("halt", [WorkflowStopped, WorkflowDeadlineExceeded])
def test_codex_stop_and_deadline_signals_are_not_wrapped_or_swallowed(
    tmp_path: Path, halt: type[BaseException]
) -> None:
    repo = _git_repo(tmp_path)
    runner = HaltingCodexRunner(halt)
    stop_event = threading.Event()
    config = PluginConfig(workflow_timeout_seconds=0.08, require_launch_approval=False)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            run_workflow(
                _script(f"return await codex({_opts(repo)!r})"),
                WorkflowOptions(
                    config=config,
                    child_runner=object(),
                    codex_runner=runner,
                    stop_event=stop_event,
                ),
            )
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert runner.started.wait(2)
    if halt is WorkflowStopped:
        stop_event.set()
    thread.join(3)

    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], halt)
