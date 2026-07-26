from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_dynamic_workflows.core.errors import (
    WorkflowDeadlineExceeded,
    WorkflowRuntimeError,
    WorkflowStopped,
)
from hermes_dynamic_workflows.engine.codex import (
    CodexStageRequest,
    CodexStageRunner,
    SubprocessCodexStageRunner,
)


FAKE_LAUNCHER = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def option(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]


contract_path = Path(option("--contract-file"))
output_path = Path(option("--json-output"))
contract = contract_path.read_text(encoding="utf-8")

if contract == "nonzero":
    raise SystemExit(23)
if contract == "malformed":
    output_path.write_text("{not-json", encoding="utf-8")
    raise SystemExit(0)
if contract == "missing":
    raise SystemExit(0)
if contract.startswith("stop:") or contract.startswith("deadline:"):
    marker = Path(contract.split(":", 1)[1])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker.with_suffix(".launcher").write_text(str(os.getpid()), encoding="utf-8")
    marker.with_suffix(".child").write_text(str(child.pid), encoding="utf-8")
    while True:
        time.sleep(0.05)

receipt = {
    "success": contract != "unsuccessful",
    "mode": option("--mode"),
    "start_head": "abc123",
    "end_head": "abc123",
    "changed_paths": [],
    "usage": {"input_tokens": 11, "output_tokens": 7, "reasoning_tokens": 5, "total_tokens": 23},
    "final_message": "fake launcher completed",
    "duration_seconds": 0.01,
    "artifact_paths": {"contract": str(contract_path), "receipt": str(output_path)},
    "observed": {
        "argv": sys.argv[1:],
        "contract": contract,
        "contract_file": str(contract_path),
        "json_output": str(output_path),
    },
}
output_path.write_text(json.dumps(receipt), encoding="utf-8")
'''


def _fake_launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "fake-codex-launcher.py"
    launcher.write_text(FAKE_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def _request(repo: Path, contract: str = "PARENT-LOCKED CONTRACT\n") -> CodexStageRequest:
    return CodexStageRequest(
        mode="code",
        workdir=str(repo),
        contract=contract,
        allow_files=("src/example.py", "tests/test_example.py"),
        timeout=30.0,
        model="gpt-5.6-luna",
        reasoning="high",
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Codex tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _wait_for(path: Path, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not path.exists():
        time.sleep(0.01)
    assert path.exists(), f"fake launcher did not create {path}"


def _terminate_if_alive(pid_path: Path) -> None:
    if not pid_path.exists():
        return
    pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    os.kill(pid, signal.SIGKILL)


def test_runner_builds_exact_argv_and_keeps_artifacts_outside_workdir(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    launcher = _fake_launcher(tmp_path)
    request = _request(repo)
    popen_calls: list[dict[str, object]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        popen_calls.append(dict(kwargs))
        return real_popen(*args, **kwargs)

    with patch("hermes_dynamic_workflows.engine.codex.subprocess.Popen", recording_popen):
        receipt = SubprocessCodexStageRunner(launcher=launcher).run(
            request,
            threading.Event(),
            time.monotonic() + 60,
        )

    observed = receipt["observed"]
    assert isinstance(observed, dict)
    assert observed["contract"] == request.contract
    assert Path(observed["contract_file"]).parent != repo
    assert Path(observed["json_output"]).parent != repo
    assert observed["argv"] == [
        "--mode",
        "code",
        "--workdir",
        str(repo),
        "--contract-file",
        observed["contract_file"],
        "--model",
        "gpt-5.6-luna",
        "--reasoning",
        "high",
        "--timeout",
        "30.0",
        "--json-output",
        observed["json_output"],
        "--allow-file",
        "src/example.py",
        "--allow-file",
        "tests/test_example.py",
    ]
    assert popen_calls[0]["shell"] is False
    assert popen_calls[0]["start_new_session"] is True


def test_runner_returns_receipt_but_rejects_unsuccessful_receipt(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    launcher = _fake_launcher(tmp_path)
    request = _request(repo, contract="unsuccessful")

    with pytest.raises(WorkflowRuntimeError, match="success"):
        SubprocessCodexStageRunner(launcher=launcher).run(
            request,
            threading.Event(),
            time.monotonic() + 60,
        )


@pytest.mark.parametrize("contract", ["malformed", "missing"])
def test_runner_fails_closed_for_malformed_or_missing_receipt(tmp_path: Path, contract: str) -> None:
    repo = _git_repo(tmp_path)
    launcher = _fake_launcher(tmp_path)

    with pytest.raises(WorkflowRuntimeError, match="receipt"):
        SubprocessCodexStageRunner(launcher=launcher).run(
            _request(repo, contract=contract),
            threading.Event(),
            time.monotonic() + 60,
        )


def test_runner_reports_nonzero_launcher_exit(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)

    with pytest.raises(WorkflowRuntimeError, match="23|launcher"):
        SubprocessCodexStageRunner(launcher=_fake_launcher(tmp_path)).run(
            _request(repo, contract="nonzero"),
            threading.Event(),
            time.monotonic() + 60,
        )


@pytest.mark.parametrize(
    ("contract", "expected"),
    [("stop", WorkflowStopped), ("deadline", WorkflowDeadlineExceeded)],
)
def test_runner_terminates_launcher_process_group_on_stop_or_deadline(
    tmp_path: Path, contract: str, expected: type[BaseException]
) -> None:
    repo = _git_repo(tmp_path)
    launcher = _fake_launcher(tmp_path)
    marker = tmp_path / "process-state"
    request = _request(repo, contract=f"{contract}:{marker}")
    result: list[BaseException] = []
    stop_event = threading.Event()
    deadline = time.monotonic() + (0.25 if contract == "deadline" else 30)

    def run() -> None:
        try:
            SubprocessCodexStageRunner(launcher=launcher).run(request, stop_event, deadline)
        except BaseException as exc:  # capture the uncatchable workflow halt from a worker thread
            result.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        _wait_for(marker.with_suffix(".child"))
        if contract == "stop":
            stop_event.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(result) == 1
        assert isinstance(result[0], expected)
        child_pid = int(marker.with_suffix(".child").read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        _terminate_if_alive(marker.with_suffix(".launcher"))
        _terminate_if_alive(marker.with_suffix(".child"))


def test_public_runner_protocol_is_implemented_by_subprocess_runner() -> None:
    assert isinstance(SubprocessCodexStageRunner, type)
    assert hasattr(CodexStageRunner, "run")
