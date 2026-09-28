"""Append-only JSONL run log. Every event is redacted as it is constructed, never post-processed."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interface_cua.domain.events import EventType, RunEvent
from interface_cua.policy.redaction import Redactor


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def opaque_id(prefix: str, length: int = 10) -> str:
    """Random identifier that always contains a letter, so it can never look like an account number
    (an all-digit id would trip the sensitive-pattern scanner, and rightly so)."""
    h = uuid.uuid4().hex[:length]
    return f"{prefix}-{'s' + h[1:] if h.isdigit() else h}"


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


class RunLog:
    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        mode: str,
        redactor: Redactor,
        *,
        capability: str | None = None,
        control: Callable[[], tuple[str, int]] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.mode = mode
        self.redactor = redactor
        self.capability = capability
        self.session_id: str | None = None
        self._control = control
        self._seq = 0
        self.events: list[RunEvent] = []
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "screenshots").mkdir(exist_ok=True)
        self.path = run_dir / "events.jsonl"

    def bind_control(self, control: Callable[[], tuple[str, int]]) -> None:
        self._control = control

    def emit(
        self,
        type: EventType,
        summary: str,
        *,
        step_id: str | None = None,
        mode: str | None = None,
        **data: Any,
    ) -> RunEvent:
        self._seq += 1
        owner, version = self._control() if self._control else (None, None)
        event = RunEvent(
            ts=now_iso(),
            seq=self._seq,
            run_id=self.run_id,
            mode=(mode or self.mode),
            capability=self.capability,
            session_id=self.session_id,
            step_id=step_id,
            control_owner=owner,
            lease_version=version,
            type=type,
            summary=self.redactor.text(summary),
            data=self.redactor.obj(_jsonable(data)),
        )
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json(exclude_none=True) + "\n")
        return event

    def write_json(self, name: str, data: Any) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(_jsonable(data), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.run_dir).as_posix()
        except ValueError:
            return path.as_posix()


def _jsonable(data: Any) -> Any:
    if hasattr(data, "model_dump"):
        return data.model_dump(mode="json", exclude_none=True)
    if isinstance(data, dict):
        return {str(k): _jsonable(v) for k, v in data.items()}
    if isinstance(data, list | tuple | set):
        return [_jsonable(v) for v in data]
    if isinstance(data, Path):
        return data.as_posix()
    return data
