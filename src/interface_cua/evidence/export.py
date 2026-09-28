"""Export a run into evidence/, gated by a leak scanner over every text file.

The scanner fails the export if any configured secret, any raw declared-input value tagged as
sensitive in the run's inputs, or any sensitive pattern (account numbers, SSN-like, bearer
tokens, session ids) appears in a text file being exported.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from interface_cua.config import Settings
from interface_cua.domain.profiles import AppProfile
from interface_cua.profiles.store import ProfileStore
from interface_cua.secrets.provider import EnvSecretProvider

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".html", ".txt"}
GENERIC_LEAKS = [
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"(?i)jsessionid=[0-9a-f]{6,}"),
    re.compile(r"(?i)SYNTHSESSID=[\w-]{8,}"),
    re.compile(r"sk-ant-[\w-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API keys
]


MASKED_IMAGES = {".png"}  # screenshots are masked when captured (adapter overlay), before they reach disk
LOCAL_ONLY = ("trace.zip",)


class LeakDetected(Exception):
    pass


def _drop_trace_refs(result_file: Path) -> None:
    """The exported result must not point at an evidence file that was deliberately left behind."""
    if not result_file.exists():
        return
    data = json.loads(result_file.read_text(encoding="utf-8"))
    refs = data.get("evidence") or []
    kept = [r for r in refs if r.get("kind") != "trace"]
    if len(kept) != len(refs):
        data["evidence"] = kept
        result_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def scan_text(text: str, *, secrets: list[str], forbidden: list[str], profile: AppProfile | None) -> list[str]:
    findings = [f"secret value #{i}" for i, s in enumerate(secrets) if s and s in text]
    findings += [f"forbidden literal #{i}" for i, v in enumerate(forbidden) if v and re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", text)]
    findings += [f"pattern {p.pattern[:30]}" for p in GENERIC_LEAKS if p.search(text)]
    if profile is not None:
        for sp in profile.sensitive_fields.patterns:
            if re.search(sp.pattern, text):
                findings.append(f"sensitive pattern '{sp.kind}'")
    return findings


def scan_dir(path: Path, *, secrets: list[str], forbidden: list[str], profile: AppProfile | None) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    for f in path.rglob("*"):
        if f.is_file() and f.suffix in TEXT_SUFFIXES:
            hits = scan_text(f.read_text(encoding="utf-8", errors="replace"), secrets=secrets, forbidden=forbidden, profile=profile)
            if hits:
                report[str(f.relative_to(path))] = hits
    return report


def export_run(settings: Settings, run_id: str, folder: str, *, forbidden: list[str] | None = None) -> Path:
    """Traces are never exported: a Playwright trace holds unmasked DOM and request headers, and the
    scanner cannot read inside the archive. It stays local-only in ``runs/<run_id>/`` (git-ignored).
    Fails closed: every exported file must be a scanned text file or a masked screenshot."""
    src = settings.runs_dir / run_id
    if not src.exists():
        raise FileNotFoundError(src)
    dest = settings.evidence_dir / folder
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*LOCAL_ONLY))
    unscannable = sorted(str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file() and f.suffix not in TEXT_SUFFIXES | MASKED_IMAGES)
    if unscannable:
        shutil.rmtree(dest)
        raise LeakDetected(f"export of {run_id} blocked; files the leak scanner cannot inspect: {unscannable}")
    _drop_trace_refs(dest / "run-result.json")
    store = ProfileStore(settings)
    profile = store.app_profile("synthcore")
    secrets = EnvSecretProvider().all_values("synthcore/operator")
    report = scan_dir(dest, secrets=secrets, forbidden=forbidden or [], profile=profile)
    if report:
        shutil.rmtree(dest)
        raise LeakDetected(f"export of {run_id} blocked; findings: {report}")
    return dest
