"""Agent-facing capability catalog (stretch goal): approved artifacts exposed as tool definitions.

The contract is already JSON Schema, so a tool definition is a projection of the artifact - no
second source of truth. Invocation goes through the same deterministic replay engine.
"""

from __future__ import annotations

from typing import Any

from interface_cua.config import Settings
from interface_cua.domain.artifacts import CapabilityArtifact
from interface_cua.storage.capability_store import CapabilityStore


def _strip_extensions(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_extensions(v) for k, v in schema.items() if not k.startswith("x-")}
    if isinstance(schema, list):
        return [_strip_extensions(v) for v in schema]
    return schema


def tool_definition(a: CapabilityArtifact) -> dict[str, Any]:
    outcomes = ", ".join(o.code for o in a.contract.business_outcomes) or "none"
    return {
        "name": a.capability.id.replace("-", "_"),
        "description": (
            f"{a.capability.description} Returns {sorted(a.contract.output_names())} on success; "
            f"may instead return a business outcome ({outcomes}). Side effects: {a.contract.side_effects}. "
            f"Capability {a.ref}, content hash {a.content_hash[:19]}."
        ),
        "input_schema": _strip_extensions(a.contract.inputs),
        "x-output_schema": _strip_extensions(a.contract.outputs),
        "x-capability": {
            "id": a.capability.id,
            "version": a.capability.version,
            "requires_escalation": a.contract.side_effects == "irreversible",
        },
    }


def tool_catalog(settings: Settings) -> list[dict[str, Any]]:
    store = CapabilityStore(settings.capabilities_dir)
    latest: dict[str, CapabilityArtifact] = {}
    for a in store.list():
        if a.is_approved():
            latest[a.capability.id] = a  # list() is version-ordered, so the last approved wins
    return [tool_definition(a) for a in latest.values()]
