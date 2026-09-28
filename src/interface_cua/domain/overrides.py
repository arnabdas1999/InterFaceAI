"""Per-tenant override patches: small, reviewed, versioned deltas applied to a base artifact at run time.

An override may only change *how controls are located* (step targets, extractor targets). It can never
touch the contract, policy, risk, steps, or success condition, so a tenant specialization cannot widen
what a capability does. Like artifacts, an override is content-hashed and only applied when an approval
is bound to that exact hash.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.artifacts import Approval, CapabilityArtifact
from interface_cua.domain.canonical import sha256_of
from interface_cua.domain.targets import LocatorCandidate
from interface_cua.domain.versions import version_in_range


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OverrideOp(_Strict):
    op: Literal["replace_candidates", "prepend_candidate"]
    step: str | None = None  # a step target ...
    output: str | None = None  # ... or an extractor target
    candidates: list[LocatorCandidate] = Field(min_length=1)


class OverridePatch(_Strict):
    id: str
    version: str
    tenant_id: str
    capability_id: str
    applies_to_versions: str  # e.g. ">=1.0.0,<2.0.0"
    description: str
    author: str
    ops: list[OverrideOp]
    content_hash: str = ""
    approval: Approval | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def compute_hash(self) -> str:
        return sha256_of(self.model_dump(mode="json", exclude={"content_hash", "approval"}, exclude_none=True))

    def with_hash(self) -> OverridePatch:
        return self.model_copy(update={"content_hash": self.compute_hash()})

    def is_approved(self) -> bool:
        return (
            bool(self.content_hash)
            and self.content_hash == self.compute_hash()
            and self.approval is not None
            and self.approval.content_hash == self.content_hash
        )


class OverrideError(ValueError):
    pass


def apply_overrides(artifact: CapabilityArtifact, patches: list[OverridePatch], tenant_id: str) -> tuple[CapabilityArtifact, list[str]]:
    """Return the effective artifact for this tenant and the refs of the overrides applied.

    The base artifact's content hash and approval stay the identity of the capability; the applied
    override refs (each with its own approved hash) are reported alongside every result.
    """
    applied: list[str] = []
    data = artifact.model_dump(mode="json")
    for patch in patches:
        if patch.capability_id != artifact.capability.id:
            continue
        if patch.tenant_id != tenant_id:
            raise OverrideError(f"{patch.ref} belongs to tenant {patch.tenant_id}, not {tenant_id}")
        if not version_in_range(artifact.capability.version, patch.applies_to_versions):
            continue
        if not patch.is_approved():
            raise OverrideError(f"{patch.ref} is not approved for its current content hash")
        for op in patch.ops:
            if op.step is not None:
                holder = next((s for s in data["steps"] if s["id"] == op.step), None)
                if holder is None or holder.get("target") is None:
                    raise OverrideError(f"{patch.ref}: step {op.step} has no target to override")
                target = holder["target"]
            elif op.output is not None:
                holder = next((e for e in data["extract"] if e["output"] == op.output), None)
                if holder is None:
                    raise OverrideError(f"{patch.ref}: no extractor for output {op.output}")
                target = holder["target"]
            else:
                raise OverrideError(f"{patch.ref}: an op needs a step or an output")
            new = [c.model_dump(mode="json") for c in op.candidates]
            target["candidates"] = new if op.op == "replace_candidates" else [*new, *target["candidates"]]
            holder_precondition = holder.get("precondition") if op.step is not None else None
            if holder_precondition and holder_precondition.get("kind") == "element_present":
                holder_precondition["target"] = target  # the "control exists" precondition follows the target
        applied.append(f"{patch.ref}#{patch.content_hash[7:19]}")
    if not applied:
        return artifact, []
    effective = CapabilityArtifact.model_validate(data)
    # Effective content differs from the approved base on purpose; keep the base identity for approval checks.
    return effective.model_copy(update={"content_hash": artifact.content_hash, "lifecycle": artifact.lifecycle}), applied
