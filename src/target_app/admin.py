"""Out-of-band admin client for the synthetic target (demo scripts and tests only).

This talks to /__admin directly over HTTP. The automation browser can never reach /__admin: it is
denied by the global policy and is not on any artifact allowlist.
"""

from __future__ import annotations

from typing import Any

import httpx


def arm(base_url: str, name: str, count: int = 1, **params: Any) -> dict[str, Any]:
    r = httpx.post(f"{base_url}/__admin/faults", json={"name": name, "count": count, "params": params}, timeout=5)
    r.raise_for_status()
    return dict(r.json())


def clear(base_url: str) -> None:
    httpx.delete(f"{base_url}/__admin/faults", timeout=5).raise_for_status()


def reset(base_url: str) -> None:
    httpx.post(f"{base_url}/__admin/reset", timeout=5).raise_for_status()


def ledger(base_url: str) -> list[dict[str, Any]]:
    r = httpx.get(f"{base_url}/__admin/ledger", timeout=5)
    r.raise_for_status()
    return list(r.json()["entries"])


def health(base_url: str) -> dict[str, Any]:
    r = httpx.get(f"{base_url}/__admin/health", timeout=5)
    r.raise_for_status()
    return dict(r.json())
