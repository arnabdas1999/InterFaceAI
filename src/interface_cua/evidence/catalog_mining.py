"""Catalog mining: turn unknown page states met at run time into reviewable catalog candidates.

When replay or discovery stops on an unrecognized state, the page's blocking overlay (or its
headings) is summarized, redacted, and stored with a suggested detector and handler. Repeated
occurrences accumulate on the same candidate (keyed by a signature hash). An engineer promotes a
candidate into the app profile's state catalog after review; nothing is added automatically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SIGNATURE_JS = r"""() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
  const overlays = Array.from(document.querySelectorAll('body *')).filter(el => {
    const cs = getComputedStyle(el);
    return (cs.position === 'fixed' || el.getAttribute('role') === 'dialog') && visible(el) && norm(el.innerText);
  });
  const root = overlays[0] || null;
  const scope = root || document.body;
  const buttons = Array.from(scope.querySelectorAll('button, input[type=button], input[type=submit], a[href]'))
    .filter(visible).map(b => norm(b.value || b.innerText)).filter(Boolean).slice(0, 8);
  const checks = Array.from(scope.querySelectorAll('input[type=checkbox]')).filter(visible)
    .map(c => norm(c.parentElement ? c.parentElement.innerText : '')).filter(Boolean).slice(0, 4);
  const firstLeaf = el => Array.from(el.querySelectorAll('*')).find(e => !e.children.length && norm(e.innerText));
  const title = root ? norm((firstLeaf(root) || root).innerText).slice(0, 120)
                     : Array.from(document.querySelectorAll('h1,h2,h3,.lbl')).map(h => norm(h.innerText)).filter(Boolean)[0] || '';
  return { overlay: !!root, title, buttons, checkboxes: checks, text: norm(scope.innerText).slice(0, 600) };
}"""


def suggest_state(sig: dict[str, Any]) -> dict[str, Any]:
    """A starting point for the reviewer, in the app profile's own catalog schema."""
    title = sig.get("title") or "unknown state"
    state_id = "".join(ch if ch.isalnum() else "_" for ch in title.lower()).strip("_")[:40] or "unknown_state"
    detector: dict[str, Any] = {"kind": "text_in_region", "region": {"any_frame": True}, "pattern": title}
    handler = None
    if sig.get("overlay") and len(sig.get("buttons", [])) == 1 and not sig.get("checkboxes"):
        button = sig["buttons"][0]
        handler = {
            "kind": "dismiss",
            "target": {
                "description": f"{button} button",
                "candidates": [
                    {"strategy": "role_name", "role": "button", "name": button, "score": 0.9, "rationale": "Only control on the overlay."}
                ],
            },
        }
    return {
        "id": state_id,
        "description": f"Mined from runtime: '{title}'. Classify and review before adding to the catalog.",
        "detector": detector,
        "classification": "REVIEW: business_outcome | recoverable | hard_failure",
        "scope": "global" if sig.get("overlay") else "step",
        "handler": handler,
        "note": "an overlay with an attestation checkbox needs a human decision: keep it as an escalation, do not auto-dismiss"
        if sig.get("checkboxes") else None,
    }  # fmt: skip


def record_candidate(store_dir: Path, sig: dict[str, Any], *, route: str, run_id: str, step_id: str | None, reason: str,
                     app_profile: str) -> Path:  # fmt: skip
    key = json.dumps({"route": route, "title": sig.get("title"), "buttons": sig.get("buttons"), "profile": app_profile}, sort_keys=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / f"{digest}.json"
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if path.exists():
        cand = json.loads(path.read_text(encoding="utf-8"))
        cand["occurrences"].append({"run_id": run_id, "step_id": step_id, "reason": reason, "at": now})
    else:
        cand = {
            "candidate_id": digest, "app_profile": app_profile, "route": route, "signature": sig,
            "suggested_state": suggest_state(sig), "status": "needs_review",
            "occurrences": [{"run_id": run_id, "step_id": step_id, "reason": reason, "at": now}],
        }  # fmt: skip
    path.write_text(json.dumps(cand, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def list_candidates(store_dir: Path) -> list[dict[str, Any]]:
    if not store_dir.exists():
        return []
    items = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(store_dir.glob("*.json"))]
    return sorted(items, key=lambda c: -len(c["occurrences"]))
