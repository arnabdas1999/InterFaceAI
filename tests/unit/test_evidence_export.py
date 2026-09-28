"""The evidence exporter: traces never leave runs/, unscannable files fail closed, leaks block the export."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from interface_cua.config import Settings, load_settings
from interface_cua.evidence.export import LeakDetected, export_run


def make_run(tmp_path: Path, extra: dict[str, bytes] | None = None) -> Settings:
    settings = dataclasses.replace(load_settings(), runs_dir=tmp_path / "runs", evidence_dir=tmp_path / "evidence")
    run = settings.runs_dir / "rep-1"
    (run / "screenshots").mkdir(parents=True)
    result = {
        "run_id": "rep-1",
        "status": "success",
        "evidence": [
            {"kind": "screenshot", "path": "screenshots/1-final.png", "note": "masked"},
            {"kind": "trace", "path": "trace.zip", "note": "Playwright trace"},
        ],
    }
    (run / "run-result.json").write_text(json.dumps(result), encoding="utf-8")
    (run / "events.jsonl").write_text('{"type": "run_finished"}\n', encoding="utf-8")
    (run / "screenshots" / "1-final.png").write_bytes(b"\x89PNG masked")
    (run / "trace.zip").write_bytes(b"PK unmasked DOM and request headers")
    for name, data in (extra or {}).items():
        (run / name).write_bytes(data)
    return settings


def test_trace_is_never_exported_and_its_reference_is_dropped(tmp_path: Path) -> None:
    settings = make_run(tmp_path)
    dest = export_run(settings, "rep-1", "replay-x")
    assert not (dest / "trace.zip").exists()
    assert (settings.runs_dir / "rep-1" / "trace.zip").exists()  # still available locally
    refs = json.loads((dest / "run-result.json").read_text(encoding="utf-8"))["evidence"]
    assert [r["kind"] for r in refs] == ["screenshot"]
    assert (dest / "screenshots" / "1-final.png").exists() and (dest / "events.jsonl").exists()


def test_files_the_scanner_cannot_read_block_the_export(tmp_path: Path) -> None:
    settings = make_run(tmp_path, {"audit.sqlite": b"SQLite format 3\x00 raw rows"})
    with pytest.raises(LeakDetected, match=r"cannot inspect.*audit\.sqlite"):
        export_run(settings, "rep-1", "replay-x")
    assert not (settings.evidence_dir / "replay-x").exists()


def test_a_leaked_value_in_a_text_file_blocks_the_export(tmp_path: Path) -> None:
    settings = make_run(tmp_path, {"notes.txt": b"member Avery Quinn"})
    with pytest.raises(LeakDetected):
        export_run(settings, "rep-1", "replay-x", forbidden=["Avery Quinn"])
    assert not (settings.evidence_dir / "replay-x").exists()
