"""Thin workflow adapter for Hermes' deterministic Codex launcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..core.errors import WorkflowDeadlineExceeded, WorkflowRuntimeError, WorkflowStopped


@dataclass(frozen=True)
class CodexStageRequest:
    """Validated inputs for one invocation of ``codex-coder.py``."""

    mode: str
    workdir: str
    contract: str
    allow_files: tuple[str, ...] = ()
    timeout: float = 900.0
    model: str = "gpt-5.6-luna"
    reasoning: str = "high"
    accept_existing_changes: bool = False


class CodexStageRunner(Protocol):
    def run(
        self,
        request: CodexStageRequest,
        stop_event: threading.Event,
        deadline: float,
    ) -> dict[str, Any]:
        """Run a validated stage and return its validated launcher receipt."""


def codex_stage_fingerprint_inputs(
    request: CodexStageRequest,
    start_head: str,
) -> dict[str, Any]:
    """Return the complete stable identity for a Codex stage."""

    return {
        "primitive": "codex",
        "mode": request.mode,
        "workdir": request.workdir,
        "contract": request.contract,
        "allowFiles": list(request.allow_files),
        "acceptExistingChanges": request.accept_existing_changes,
        "timeout": request.timeout,
        "start_head": start_head,
    }


def canonical_workdir_and_head(workdir: str) -> tuple[str, str]:
    """Canonicalize a workflow workdir and read its starting Git revision."""

    path = Path(workdir)
    if not path.is_absolute():
        raise WorkflowRuntimeError("codex() workdir must be an absolute path")
    try:
        canonical = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkflowRuntimeError(f"codex() workdir is not accessible: {workdir}") from exc
    if not canonical.is_dir():
        raise WorkflowRuntimeError(f"codex() workdir must be a directory: {workdir}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(canonical),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowRuntimeError(
            f"codex() workdir must be a Git repository with a readable HEAD: {canonical}"
        ) from exc
    head = result.stdout.strip()
    if result.returncode != 0 or not head:
        raise WorkflowRuntimeError(
            f"codex() workdir must be a Git repository with a readable HEAD: {canonical}"
        )
    return str(canonical), head


class SubprocessCodexStageRunner:
    """Invoke ``codex-coder.py`` without duplicating its safety checks."""

    def __init__(self, launcher: Path | str | None = None):
        self.launcher = Path(launcher) if launcher is not None else None

    def run(
        self,
        request: CodexStageRequest,
        stop_event: threading.Event,
        deadline: float,
    ) -> dict[str, Any]:
        artifact_dir = Path(tempfile.mkdtemp(prefix="dynamic-workflow-codex-"))
        contract_path = artifact_dir / "contract.txt"
        output_path = artifact_dir / "receipt.json"
        stdout_path = artifact_dir / "launcher.stdout"
        stderr_path = artifact_dir / "launcher.stderr"
        contract_path.write_text(request.contract, encoding="utf-8")

        launcher = self.launcher or _default_launcher()
        if not launcher.is_file():
            raise WorkflowRuntimeError(
                f"Codex launcher not found: {launcher} "
                f"(artifacts preserved in {artifact_dir})"
            )

        argv = [
            sys.executable,
            str(launcher),
            "--mode",
            request.mode,
            "--workdir",
            request.workdir,
            "--contract-file",
            str(contract_path),
            "--model",
            request.model,
            "--reasoning",
            request.reasoning,
            "--timeout",
            str(request.timeout),
            "--json-output",
            str(output_path),
        ]
        for path in request.allow_files:
            argv.extend(("--allow-file", path))
        if request.accept_existing_changes:
            argv.append("--accept-existing-changes")

        process: subprocess.Popen[bytes] | None = None
        stdout = stdout_path.open("wb")
        stderr = stderr_path.open("wb")
        try:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=request.workdir,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise WorkflowRuntimeError(
                    f"could not launch Codex launcher: {exc} "
                    f"(artifacts preserved in {artifact_dir})"
                ) from exc

            while True:
                if stop_event.is_set():
                    _terminate_process_group(process)
                    raise WorkflowStopped("workflow was stopped")
                if time.monotonic() >= deadline:
                    _terminate_process_group(process)
                    raise WorkflowDeadlineExceeded("Codex workflow stage timed out")
                returncode = process.poll()
                if returncode is not None:
                    break
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        finally:
            stdout.close()
            stderr.close()

        if returncode != 0:
            raise WorkflowRuntimeError(
                f"Codex launcher exited with status {returncode} "
                f"(artifacts preserved in {artifact_dir})"
            )

        try:
            receipt = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowRuntimeError(
                f"Codex launcher receipt is missing or malformed: {output_path} "
                f"(artifacts preserved in {artifact_dir})"
            ) from exc
        if not isinstance(receipt, dict):
            raise WorkflowRuntimeError(
                f"Codex launcher receipt must be a JSON object: {output_path} "
                f"(artifacts preserved in {artifact_dir})"
            )
        if receipt.get("success") is not True:
            raise WorkflowRuntimeError(
                f"Codex launcher receipt reported success=false "
                f"(artifacts preserved in {artifact_dir})"
            )
        return receipt


def _default_launcher() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "scripts" / "codex-coder.py"


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the launcher and every process it spawned."""

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
