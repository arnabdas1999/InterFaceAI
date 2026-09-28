"""Drift report: replay health per capability x tenant, computed from persisted run results.

Drift shows up first as *fallback usage* (a lower-ranked locator candidate had to be used) and as
rising recovery counts, before it shows up as failures. This report surfaces those signals so a
capability can be re-validated or re-recorded (as a new version) before it breaks.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def load_results(run_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    out = []
    for d in run_dirs:
        f = d / "run-result.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("mode") in {"replay", "validation"}:
                out.append(data)
    return out


def drift_report(results: list[dict[str, Any]], *, failure_rate_alert: float = 0.2) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        groups[(r.get("capability") or "?", r.get("tenant_id") or "?")].append(r)
    rows = []
    for (cap, tenant), rs in sorted(groups.items()):
        statuses = Counter(r["status"] for r in rs)
        failures = Counter(r.get("code") for r in rs if r["status"] == "failure")
        outcomes = Counter(r.get("code") for r in rs if r["status"] == "business_outcome")
        recoveries = Counter(x["code"] for r in rs for x in r.get("recoveries", []))
        drift = Counter(f"{x.get('step_id')}:{x['code']}" for r in rs for x in r.get("drift", []))
        overrides = sorted({o for r in rs for o in r.get("overrides", [])})
        durations = [r["duration_ms"] for r in rs if r["status"] == "success"]
        completed = sum(statuses.values()) - statuses.get("rejected", 0)
        failure_rate = (statuses.get("failure", 0) / completed) if completed else 0.0
        alerts = []
        if drift:
            alerts.append("locator drift: fallback candidates in use - review and re-validate the locators (new minor version)")
        if failure_rate > failure_rate_alert:
            alerts.append(f"failure rate {failure_rate:.0%} above {failure_rate_alert:.0%}")
        if failures.get("app_incompatible"):
            alerts.append("application fingerprint outside the supported range - re-record or extend compatibility")
        rows.append({
            "capability": cap, "tenant": tenant, "runs": len(rs), "statuses": dict(statuses),
            "failure_rate": round(failure_rate, 3), "failures": dict(failures), "business_outcomes": dict(outcomes),
            "recoveries": dict(recoveries), "drift_signals": dict(drift), "overrides": overrides,
            "success_duration_ms": {"mean": int(sum(durations) / len(durations)), "max": max(durations)} if durations else None,
            "alerts": alerts,
        })  # fmt: skip
    return {"groups": rows, "runs": len(results)}


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Replay drift report", "", f"{report['runs']} replay/validation runs.", ""]
    lines.append("| Capability | Tenant | Runs | Success | Failure rate | Recoveries | Drift signals | Overrides | Alerts |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for g in report["groups"]:
        rec = ", ".join(f"{k}x{v}" for k, v in g["recoveries"].items()) or "-"
        drift = ", ".join(f"{k}x{v}" for k, v in g["drift_signals"].items()) or "-"
        lines.append(
            f"| `{g['capability']}` | {g['tenant']} | {g['runs']} | {g['statuses'].get('success', 0)} | {g['failure_rate']:.0%} | "
            f"{rec} | {drift} | {len(g['overrides'])} | {'; '.join(g['alerts']) or '-'} |"
        )
    return "\n".join(lines) + "\n"
