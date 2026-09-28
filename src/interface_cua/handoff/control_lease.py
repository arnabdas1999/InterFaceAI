"""Fenced control lease. The version is the fencing token: every change is compare-and-set on it and
bumps it, and every surface action must present the current token. "Automation never acts while a
human owns the session" is therefore enforced at the adapter, not a convention."""

from __future__ import annotations

import time
from collections.abc import Callable

from interface_cua.domain.interventions import ALLOWED_TRANSITIONS, ControlState, LeaseSnapshot


class StaleLeaseError(PermissionError):
    """Raised when an actor presents a token that is no longer current (or is not the owner)."""


class LeaseConflict(RuntimeError):
    """Compare-and-set failed: someone else changed the lease first (e.g. a concurrent claim)."""


class ControlLease:
    def __init__(self, session_id: str, on_change: Callable[[LeaseSnapshot, LeaseSnapshot, str], None] | None = None):
        self._snap = LeaseSnapshot(
            session_id=session_id,
            state=ControlState.AUTOMATION_ACTIVE,
            owner="automation",
            expected_owner="automation",
            version=1,
        )
        self._on_change = on_change

    @property
    def snapshot(self) -> LeaseSnapshot:
        return self._snap

    @property
    def version(self) -> int:
        return self._snap.version

    @property
    def owner(self) -> str:
        return self._snap.owner

    def set_listener(self, on_change: Callable[[LeaseSnapshot, LeaseSnapshot, str], None]) -> None:
        self._on_change = on_change

    def transition(
        self,
        to: ControlState,
        *,
        expect_version: int,
        owner: str,
        expected_owner: str | None = None,
        reason: str,
        expires_in_s: float | None = None,
    ) -> LeaseSnapshot:
        cur = self._snap
        if cur.version != expect_version:
            raise LeaseConflict(f"lease version is {cur.version}, caller expected {expect_version}")
        if to not in ALLOWED_TRANSITIONS[cur.state]:
            raise LeaseConflict(f"illegal control transition {cur.state} -> {to}")
        now = time.time()
        new = LeaseSnapshot(
            session_id=cur.session_id,
            state=to,
            owner=owner,
            expected_owner=expected_owner or owner,
            version=cur.version + 1,
            expires_at=(now + expires_in_s) if expires_in_s else None,
            last_heartbeat=now if owner.startswith("human:") else None,
        )
        self._snap = new
        if self._on_change:
            self._on_change(cur, new, reason)
        return new

    def check(self, token: int, actor: str) -> None:
        """Gate for every surface action. ``actor`` is "automation" or "human:<id>"."""
        cur = self._snap
        if token != cur.version:
            raise StaleLeaseError(f"stale control token {token}; current is {cur.version} (owner {cur.owner})")
        if actor != cur.owner:
            raise StaleLeaseError(f"{actor} is not the owner of the session (owner {cur.owner})")
        if cur.state not in {ControlState.AUTOMATION_ACTIVE, ControlState.HUMAN_ACTIVE}:
            raise StaleLeaseError(f"no actions allowed in control state {cur.state}")
        if cur.expires_at is not None and time.time() > cur.expires_at:
            raise StaleLeaseError("lease expired")

    def heartbeat(self, token: int, actor: str, extend_s: float) -> None:
        self.check(token, actor)
        now = time.time()
        self._snap = self._snap.model_copy(update={"last_heartbeat": now, "expires_at": now + extend_s})
