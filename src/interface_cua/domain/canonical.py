"""Canonical JSON serialization and content hashing (reviewable diffs, deterministic hashes)."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(data: Any, *, indent: int | None = 2) -> str:
    return json.dumps(data, sort_keys=True, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":")) + (
        "\n" if indent else ""
    )


def sha256_of(data: Any) -> str:
    compact = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(compact.encode("utf-8")).hexdigest()
