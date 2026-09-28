"""Routing seam for intervention requests. Console + JSONL outbox now; a pager/Slack/queue
integration is a new ``Notifier`` implementation, not a redesign."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Protocol

import httpx

from interface_cua.domain.interventions import Intervention


class Notifier(Protocol):
    def notify(self, intervention: Intervention, console_url: str) -> None: ...


class ConsoleNotifier:
    def notify(self, intervention: Intervention, console_url: str) -> None:
        iv = intervention
        cli = f"cua operator resolve {iv.id} approve" if iv.kind == "approval_required" else f"cua operator claim {iv.id}"
        print(
            f"\n[INTERVENTION {iv.id}] {iv.kind} at step {iv.step_id}: {iv.explanation}\n"
            f"  capability={iv.capability or iv.goal} reason={iv.reason_code} expected owner={iv.control.expected_owner}\n"
            f"  operator console: {console_url}/console/{iv.id}\n"
            f"  or: {cli}   (identity comes from CUA_OPERATOR_API_TOKEN)\n",
            file=sys.stderr,
            flush=True,
        )


class OutboxNotifier:
    """Append-only outbox a real router (pager, chat, ticketing) would consume."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def notify(self, intervention: Intervention, console_url: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "intervention_id": intervention.id,
            "run_id": intervention.run_id,
            "kind": intervention.kind,
            "reason_code": intervention.reason_code,
            "capability": intervention.capability,
            "step_id": intervention.step_id,
            "console": f"{console_url}/console/{intervention.id}",
            "claim_deadline": intervention.claim_deadline,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class WebhookNotifier:
    """Routes intervention requests to an HTTP endpoint (pager, chat bridge, ticketing, queue).

    The body is the redacted routing summary (never a screenshot or page content). Each request carries
    ``X-CUA-Timestamp`` and ``X-CUA-Signature: sha256=<hex HMAC of "timestamp.body">`` so the receiver
    can authenticate it and reject replays (see ``verify_signature``). Delivery is retried with backoff;
    a failed delivery is recorded but never blocks or fails the escalation itself.
    """

    def __init__(self, url: str, signing_key: str, *, attempts: int = 3, backoff_s: float = 0.5, timeout_s: float = 5.0) -> None:
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("webhook URL must be https (plain http only for localhost)")
        self.url = url
        self._key = signing_key.encode()
        self.attempts = attempts
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.deliveries: list[dict[str, object]] = []

    def payload(self, intervention: Intervention, console_url: str) -> bytes:
        iv = intervention
        body = {
            "type": "intervention.requested",
            "intervention_id": iv.id,
            "run_id": iv.run_id,
            "tenant_id": iv.tenant_id,
            "kind": iv.kind,
            "reason_code": iv.reason_code,
            "capability": iv.capability,
            "step_id": iv.step_id,
            "explanation": iv.explanation,
            "expected_owner": iv.control.expected_owner,
            "permitted_resolutions": iv.permitted_resolutions,
            "claim_deadline": iv.claim_deadline,
            "console": f"{console_url}/console/{iv.id}",
        }
        return json.dumps(body, sort_keys=True).encode()

    def sign(self, timestamp: str, body: bytes) -> str:
        return "sha256=" + hmac.new(self._key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()

    def notify(self, intervention: Intervention, console_url: str) -> None:
        body = self.payload(intervention, console_url)
        last_error = ""
        for attempt in range(1, self.attempts + 1):
            ts = str(int(time.time()))
            try:
                r = httpx.post(
                    self.url,
                    content=body,
                    timeout=self.timeout_s,
                    headers={"content-type": "application/json", "x-cua-timestamp": ts, "x-cua-signature": self.sign(ts, body)},
                )
                if r.status_code < 300:
                    self.deliveries.append({"intervention_id": intervention.id, "status": r.status_code, "attempt": attempt})
                    return
                last_error = f"HTTP {r.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(self.backoff_s * attempt)
        self.deliveries.append({"intervention_id": intervention.id, "status": "failed", "error": last_error, "attempts": self.attempts})


def verify_signature(signing_key: str, timestamp: str, body: bytes, signature: str, *, max_age_s: int = 300) -> bool:
    """Receiver-side check: valid HMAC over "timestamp.body" and a fresh timestamp (replay protection)."""
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    expected = "sha256=" + hmac.new(signing_key.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return age <= max_age_s and hmac.compare_digest(expected, signature)


class FanoutNotifier:
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = notifiers

    def notify(self, intervention: Intervention, console_url: str) -> None:
        for n in self.notifiers:
            try:
                n.notify(intervention, console_url)
            except Exception as exc:
                print(f"[notifier] {type(n).__name__} failed: {type(exc).__name__}", file=sys.stderr)


def default_notifier(outbox: Path) -> Notifier:
    """Console + outbox, plus a signed webhook when CUA_WEBHOOK_URL and its signing secret are configured."""
    import os

    from interface_cua.secrets.provider import EnvSecretProvider

    channels: list[Notifier] = [ConsoleNotifier(), OutboxNotifier(outbox)]
    url = os.environ.get("CUA_WEBHOOK_URL")
    if url:
        key = EnvSecretProvider().get("webhook", "signing_key").reveal()
        channels.append(WebhookNotifier(url, key))
    return FanoutNotifier(*channels)
