"""A scripted stand-in for a human operator, used by tests and the evidence script.

It only uses the public operator API (the same one the console uses): wait for an intervention,
claim the live session, look at the masked controls, click by coordinates, hand back. Evidence
labels its actions as ``scripted_stand_in``; a real person uses the console or the headed window.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class OperatorStep:
    kind: str  # click_control | type | press | resolve
    name: str | None = None
    role: str | None = None
    text: str | None = None
    key: str | None = None
    resolution: str | None = None
    note: str = ""


class ScriptedOperator:
    def __init__(self, base_url: str, operator_id: str = "scripted-operator", api_token: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.operator_id = operator_id
        self.headers = {"authorization": f"Bearer {api_token}"} if api_token else {}
        self.log: list[dict[str, Any]] = []

    async def wait_for_intervention(self, client: httpx.AsyncClient, timeout_s: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                items = (await client.get(f"{self.base}/interventions")).json()
                open_items = [i for i in items if i["status"] == "open"]
                if open_items:
                    return dict(open_items[0])
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError("no intervention appeared")

    async def run(self, steps: list[OperatorStep], timeout_s: float = 120) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30, headers=self.headers) as client:
            iv = await self.wait_for_intervention(client, timeout_s)
            iv_id = iv["id"]
            token: int | None = None
            if any(s.kind != "resolve" or s.resolution not in {"approve", "deny"} for s in steps):
                r = await client.post(f"{self.base}/interventions/{iv_id}/claim", json={"operator_id": self.operator_id})
                r.raise_for_status()
                token = r.json()["token"]
                self.log.append({"claimed": iv_id, "token": token})
            for s in steps:
                if s.kind == "click_control":
                    controls = (await client.get(f"{self.base}/interventions/{iv_id}/controls")).json()
                    match = next(c for c in controls if c["name"] == s.name and (s.role is None or c["role"] == s.role))
                    r = await client.post(
                        f"{self.base}/interventions/{iv_id}/act",
                        json={"operator_id": self.operator_id, "token": token, "kind": "click", "x": match["x"], "y": match["y"]},
                    )
                elif s.kind == "type":
                    r = await client.post(
                        f"{self.base}/interventions/{iv_id}/act",
                        json={"operator_id": self.operator_id, "token": token, "kind": "type", "text": s.text},
                    )
                elif s.kind == "press":
                    r = await client.post(
                        f"{self.base}/interventions/{iv_id}/act",
                        json={"operator_id": self.operator_id, "token": token, "kind": "press", "key": s.key},
                    )
                else:
                    body: dict[str, Any] = {
                        "operator_id": self.operator_id,
                        "kind": s.resolution,
                        "token": token,
                        "note": f"[scripted_stand_in] {s.note}",
                    }
                    if s.resolution == "approve":
                        body["action_hash"] = (await client.get(f"{self.base}/interventions/{iv_id}")).json()["bound_action_hash"]
                    r = await client.post(f"{self.base}/interventions/{iv_id}/resolve", json=body)
                self.log.append({"step": s.kind, "status": r.status_code, "body": r.json()})
                r.raise_for_status()
            return {"intervention_id": iv_id, "log": self.log}
