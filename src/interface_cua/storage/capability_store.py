"""File-backed capability registry: ``capabilities/<id>/<version>.json`` + ``.review.md``.

Content is immutable (refuses to overwrite a version; hash verified on load). Only the lifecycle
record is updated, append-only, and approvals are bound to the content hash.
"""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from pathlib import Path

from interface_cua.domain.artifacts import Approval, CapabilityArtifact, LifecycleTransition, Status
from interface_cua.domain.canonical import canonical_json

ALLOWED: dict[str, set[str]] = {
    "draft": {"validated", "deprecated"},
    "validated": {"approved", "deprecated", "draft"},
    "approved": {"deprecated"},
    "deprecated": set(),
}


class ArtifactIntegrityError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


class CapabilityStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, capability_id: str, version: str) -> Path:
        return self.root / capability_id / f"{version}.json"

    def save(self, artifact: CapabilityArtifact, review_md: str | None = None) -> Path:
        if not artifact.hash_ok():
            raise ArtifactIntegrityError("content hash does not match content")
        path = self.path(artifact.capability.id, artifact.capability.version)
        if path.exists():
            raise FileExistsError(f"{artifact.ref} already exists; versions are immutable (bump the version)")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(artifact.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
        if review_md is not None:
            path.with_suffix(".review.md").write_text(review_md, encoding="utf-8")
        return path

    def load_path(self, path: Path) -> CapabilityArtifact:
        artifact = CapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if not artifact.hash_ok():
            raise ArtifactIntegrityError(f"{path}: content hash mismatch (artifact was modified after hashing)")
        return artifact

    def versions(self, capability_id: str) -> list[str]:
        d = self.root / capability_id
        if not d.exists():
            return []
        return sorted((p.stem for p in d.glob("*.json") if not p.stem.endswith(".review")), key=_semver_key)

    def load(self, capability_id: str, version: str = "approved-latest") -> CapabilityArtifact:
        if version in {"latest", "approved-latest"}:
            candidates = self.versions(capability_id)
            for v in reversed(candidates):
                art = self.load_path(self.path(capability_id, v))
                if version == "latest" or art.is_approved():
                    return art
            raise FileNotFoundError(f"no {'approved ' if version == 'approved-latest' else ''}version of {capability_id}")
        return self.load_path(self.path(capability_id, version))

    def list(self) -> builtins.list[CapabilityArtifact]:
        out: builtins.list[CapabilityArtifact] = []
        if not self.root.exists():
            return out
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for v in self.versions(d.name):
                try:
                    out.append(self.load_path(self.path(d.name, v)))
                except (ArtifactIntegrityError, ValueError):
                    continue
        return out

    def transition(
        self, artifact: CapabilityArtifact, to: Status, *, actor: str, note: str = "", run_ids: builtins.list[str] | None = None
    ) -> CapabilityArtifact:
        current = artifact.lifecycle.status
        if to not in ALLOWED[current]:
            raise ValueError(f"illegal lifecycle transition {current} -> {to}")
        if not artifact.hash_ok():
            raise ArtifactIntegrityError("content hash mismatch")
        lifecycle = artifact.lifecycle.model_copy(deep=True)
        lifecycle.status = to
        lifecycle.transitions.append(LifecycleTransition(to=to, at=_now(), actor=actor, note=note, run_ids=run_ids or []))
        if to == "approved":
            lifecycle.approvals.append(Approval(approver=actor, at=_now(), content_hash=artifact.content_hash, note=note))
        updated = artifact.model_copy(update={"lifecycle": lifecycle})
        path = self.path(artifact.capability.id, artifact.capability.version)
        path.write_text(canonical_json(updated.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
        return updated
