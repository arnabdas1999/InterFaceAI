"""SQLite audit trail for interventions and lease transitions (listing after a run ends).
Never the source of truth for artifacts."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from interface_cua.domain.interventions import Intervention, LeaseSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
  id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, status TEXT, reason_code TEXT, step_id TEXT,
  created_at REAL, claimed_at REAL, resolved_at REAL, operator_id TEXT, resolution TEXT, body TEXT
);
CREATE TABLE IF NOT EXISTS lease_transitions (
  run_id TEXT, at REAL, from_state TEXT, to_state TEXT, owner TEXT, expected_owner TEXT, version INTEGER, reason TEXT
);
"""


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def upsert(self, iv: Intervention) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO interventions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    iv.id,
                    iv.run_id,
                    iv.kind,
                    iv.status,
                    iv.reason_code,
                    iv.step_id,
                    iv.created_at,
                    iv.claimed_at,
                    iv.resolved_at,
                    iv.operator_id,
                    iv.resolution.kind if iv.resolution else None,
                    iv.model_dump_json(),
                ),
            )

    def lease(self, run_id: str, old: LeaseSnapshot, new: LeaseSnapshot, reason: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO lease_transitions VALUES (?,?,?,?,?,?,?,?)",
                (run_id, time.time(), old.state.value, new.state.value, new.owner, new.expected_owner, new.version, reason),
            )

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, run_id, kind, status, reason_code, step_id, operator_id, resolution, created_at FROM interventions "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ["id", "run_id", "kind", "status", "reason_code", "step_id", "operator_id", "resolution", "created_at"]
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def get(self, intervention_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT body FROM interventions WHERE id = ?", (intervention_id,)).fetchone()
        return json.loads(row[0]) if row else None
