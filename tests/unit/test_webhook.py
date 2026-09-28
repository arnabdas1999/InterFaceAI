"""Signed webhook routing against a real local receiver."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from interface_cua.domain.interventions import ControlState, Intervention, LeaseSnapshot
from interface_cua.handoff.notifier import WebhookNotifier, verify_signature

pytestmark = pytest.mark.unit
PORT = 8798
KEY = "test-signing-key"
RECEIVED: list[dict[str, Any]] = []
FAIL_NEXT = {"n": 0}


def _receiver() -> FastAPI:
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        if FAIL_NEXT["n"] > 0:
            FAIL_NEXT["n"] -= 1
            return Response(status_code=503)
        body = await request.body()
        ok = verify_signature(KEY, request.headers["x-cua-timestamp"], body, request.headers["x-cua-signature"])
        RECEIVED.append({"verified": ok, "body": json.loads(body)})
        return Response(status_code=204 if ok else 401)

    return app


@pytest.fixture(scope="module")
def receiver() -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(_receiver(), host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}/hook"
    server.should_exit = True


def intervention() -> Intervention:
    lease = LeaseSnapshot(
        session_id="s", state=ControlState.HUMAN_PENDING, owner="unassigned", expected_owner="human:any-operator", version=3
    )
    return Intervention(id="iv-1", run_id="rep-1", kind="unknown_state", tenant_id="tenant-a", step_id="s3", reason_code="unknown_state",
                        explanation="modal", control=lease, permitted_resolutions=["resume", "abort"], claim_deadline=time.time() + 60,
                        max_human_active_s=900, created_at=time.time())  # fmt: skip


def test_webhook_delivers_a_signed_redacted_summary(receiver: str) -> None:
    RECEIVED.clear()
    hook = WebhookNotifier(receiver, KEY, backoff_s=0.01)
    hook.notify(intervention(), "http://127.0.0.1:8766")
    assert RECEIVED and RECEIVED[-1]["verified"] is True
    body = RECEIVED[-1]["body"]
    assert body["intervention_id"] == "iv-1" and body["console"].endswith("/console/iv-1")
    assert "screenshot" not in body and "observed" not in body  # routing summary only
    assert hook.deliveries[-1]["status"] == 204


def test_webhook_retries_transient_failures(receiver: str) -> None:
    FAIL_NEXT["n"] = 2
    hook = WebhookNotifier(receiver, KEY, backoff_s=0.01)
    hook.notify(intervention(), "http://x")
    assert hook.deliveries[-1] == {"intervention_id": "iv-1", "status": 204, "attempt": 3}


def test_webhook_failure_never_raises() -> None:
    hook = WebhookNotifier("http://127.0.0.1:1/unreachable", KEY, attempts=2, backoff_s=0.01, timeout_s=0.5)
    hook.notify(intervention(), "http://x")
    assert hook.deliveries[-1]["status"] == "failed"


def test_signature_rejects_tampering_replay_and_plain_http() -> None:
    hook = WebhookNotifier("https://hooks.example/cua", KEY)
    body = hook.payload(intervention(), "http://x")
    ts = str(int(time.time()))
    sig = hook.sign(ts, body)
    assert verify_signature(KEY, ts, body, sig)
    assert not verify_signature(KEY, ts, body.replace(b"iv-1", b"iv-2"), sig)
    old = str(int(time.time()) - 3600)
    assert not verify_signature(KEY, old, body, hook.sign(old, body))
    assert not verify_signature("other-key", ts, body, sig)
    with pytest.raises(ValueError):
        WebhookNotifier("http://hooks.example/cua", KEY)
