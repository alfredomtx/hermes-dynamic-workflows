"""Small cross-process request/response queue for the standalone TUI."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .store import WorkflowStore, sanitize_filename, utc_now_iso


CONTROL_ACTIONS = frozenset({"stop", "pause", "resume", "restart"})
DEFAULT_REQUEST_TTL_SECONDS = 15.0


class ControlClient:
    def __init__(self, store: WorkflowStore | None = None):
        self.store = store or WorkflowStore()

    def request(
        self,
        *,
        owner: str,
        run_id: str,
        action: str,
        expected_status: str | None = None,
        wait_seconds: float = 1.5,
    ) -> dict[str, Any]:
        clean_owner = _clean_owner(owner)
        clean_action = str(action or "").strip().lower()
        if not clean_owner:
            return {"ok": False, "message": "This run has no live Hermes control endpoint."}
        if clean_action not in CONTROL_ACTIONS:
            return {"ok": False, "message": f"Unsupported workflow control action: {action}"}

        request_id = f"ctl_{uuid.uuid4().hex[:16]}"
        request = {
            "requestId": request_id,
            "owner": clean_owner,
            "runId": str(run_id or ""),
            "action": clean_action,
            "expectedStatus": str(expected_status or ""),
            "createdAt": utc_now_iso(),
            "expiresAtEpoch": time.time() + DEFAULT_REQUEST_TTL_SECONDS,
        }
        request_path = _request_dir(self.store, clean_owner) / f"{request_id}.json"
        response_path = _response_dir(self.store, clean_owner) / f"{request_id}.json"
        _write_json_atomic(request_path, request)

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while time.monotonic() < deadline:
            response = _read_json(response_path)
            if response is not None:
                try:
                    response_path.unlink()
                except OSError:
                    pass
                return response
            time.sleep(0.05)
        return {
            "ok": False,
            "pending": True,
            "requestId": request_id,
            "message": "Control request queued; Hermes has not responded yet.",
        }


class ControlListener:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        owner: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        interval_seconds: float = 0.1,
    ):
        self.store = store
        self.owner = _clean_owner(owner)
        self.handler = handler
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"workflow-control-{self.owner[:24]}",
            daemon=True,
        )

    def start(self) -> None:
        for path in (_request_dir(self.store, self.owner), _response_dir(self.store, self.owner)):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def process_once(self) -> int:
        processed = 0
        request_dir = _request_dir(self.store, self.owner)
        for path in sorted(request_dir.glob("ctl_*.json")):
            request = _read_json(path)
            if request is None:
                continue
            request_id = str(request.get("requestId") or path.stem)
            response_path = _response_dir(self.store, self.owner) / f"{sanitize_filename(request_id)}.json"
            try:
                if float(request.get("expiresAtEpoch") or 0) < time.time():
                    response = {
                        "ok": False,
                        "message": "Control request expired before Hermes processed it.",
                    }
                else:
                    response = self.handler(request)
            except BaseException as exc:
                response = {
                    "ok": False,
                    "message": f"Control action failed: {type(exc).__name__}: {exc}",
                }
            response.update(
                {
                    "requestId": request_id,
                    "owner": self.owner,
                    "respondedAt": utc_now_iso(),
                }
            )
            _write_json_atomic(response_path, response)
            try:
                path.unlink()
            except OSError:
                pass
            processed += 1
        return processed

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.process_once()
            except Exception:
                pass


def new_control_owner() -> str:
    import os

    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _request_dir(store: WorkflowStore, owner: str) -> Path:
    return store.root / "control" / "requests" / _clean_owner(owner)


def _response_dir(store: WorkflowStore, owner: str) -> Path:
    return store.root / "control" / "responses" / _clean_owner(owner)


def _clean_owner(owner: Any) -> str:
    raw = str(owner or "").strip()
    return sanitize_filename(raw) if raw else ""


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
