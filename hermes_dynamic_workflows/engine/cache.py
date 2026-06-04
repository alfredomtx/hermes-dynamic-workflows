"""Agent-call cache used by resumeFromRunId.

Content-addressed: results are keyed by a fingerprint of (prompt, opts), not by
execution-order sequence. parallel()/pipeline() reserve agents in
thread-scheduling order, so a sequence-keyed cache misaligns on re-run and
disables resume after the first parallel block. Two agent() calls with an
identical fingerprint have identical inputs and are therefore interchangeable,
so on resume we hand each one any not-yet-consumed cached result for that
fingerprint (FIFO). Dependencies are handled implicitly: when an upstream
result flows into a downstream prompt, the downstream fingerprint changes if
the upstream changed, so it misses and re-runs.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any


class ResumeCache:
    def __init__(self, previous: dict[str, Any] | None = None):
        # fingerprint -> list of cached results not yet consumed this run
        self._previous: dict[str, list[Any]] = _normalize_previous(previous or {})
        # fingerprint -> list of results produced this run (persisted for the
        # next resume)
        self.current: dict[str, list[Any]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_run(cls, run_record: dict[str, Any] | None) -> "ResumeCache":
        if not run_record:
            return cls()
        cache = run_record.get("agentCache")
        return cls(cache if isinstance(cache, dict) else {})

    def get(self, fingerprint: str) -> Any:
        with self._lock:
            bucket = self._previous.get(fingerprint)
            if bucket:
                return bucket.pop(0)
            return _MISS

    def put(self, fingerprint: str, result: Any) -> None:
        with self._lock:
            self.current.setdefault(fingerprint, []).append(_jsonable(result))


def _normalize_previous(raw: Any) -> dict[str, list[Any]]:
    """Load a stored agentCache ({fingerprint: [results]}).

    Tolerant of malformed on-disk data (partial/crashed/hand-edited runs):
    anything that isn't a fingerprint->list entry is ignored, which just yields
    a cache miss and a benign live re-run rather than a crash.
    """
    grouped: dict[str, list[Any]] = {}
    if not isinstance(raw, dict):
        return grouped
    for key, value in raw.items():
        if isinstance(value, list):
            grouped.setdefault(str(key), []).extend(value)
    return grouped


class _Miss:
    pass


_MISS = _Miss()


def is_cache_miss(value: Any) -> bool:
    return isinstance(value, _Miss)


def agent_fingerprint(prompt: str, opts: dict[str, Any]) -> str:
    payload = {
        "prompt": prompt,
        "opts": _jsonable(opts),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return repr(value)
