"""Regenerate the committed JSON Schemas from the Pydantic models (the models are the source of truth)."""

from __future__ import annotations

import json
from pathlib import Path

from interface_cua.domain.artifacts import CapabilityArtifact
from interface_cua.domain.events import RunEvent
from interface_cua.domain.interventions import Intervention
from interface_cua.domain.profiles import AppProfile, TenantProfile
from interface_cua.domain.results import RunResultEnvelope

OUT = Path(__file__).resolve().parents[1] / "schemas"
MODELS = {
    "capability.schema.json": CapabilityArtifact,
    "app-profile.schema.json": AppProfile,
    "tenant-profile.schema.json": TenantProfile,
    "run-result.schema.json": RunResultEnvelope,
    "intervention.schema.json": Intervention,
    "events.schema.json": RunEvent,
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, model in MODELS.items():
        schema = model.model_json_schema(mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://github.com/interface-cua/schemas/{name}"
        # LF on every OS, so CI's regenerate-and-compare check is byte-identical on Linux.
        (OUT / name).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote schemas/{name}")


if __name__ == "__main__":
    main()
