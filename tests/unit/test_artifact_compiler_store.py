import json
from pathlib import Path

import pytest

from interface_cua.config import load_settings
from interface_cua.discovery.compiler import AuthoredPatch, apply_patch, compile_trajectory
from interface_cua.discovery.recorder import Trajectory
from interface_cua.discovery.review import render_review
from interface_cua.domain.actions import ActionKind, RiskClass
from interface_cua.domain.artifacts import CapabilityArtifact, RetryPolicy, bump_version
from interface_cua.domain.canonical import canonical_json
from interface_cua.profiles.store import ProfileStore
from interface_cua.storage.capability_store import ArtifactIntegrityError, CapabilityStore

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parents[1] / "fixtures" / "trajectory_balance.json"
ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def profile():  # type: ignore[no-untyped-def]
    return ProfileStore(load_settings()).app_profile("synthcore@1.0.0")


@pytest.fixture(scope="module")
def artifact(profile) -> CapabilityArtifact:  # type: ignore[no-untyped-def]
    return compile_trajectory(Trajectory.model_validate_json(FIXTURE.read_text(encoding="utf-8")), profile)


def test_compiled_artifact_is_parameterized_and_transcript_free(artifact: CapabilityArtifact) -> None:
    text = canonical_json(artifact.model_dump(mode="json", exclude_none=True))
    assert "12345" not in text  # the discovery input never enters the artifact
    assert '"from_input": "member_id"' in text
    assert "rationale" in text and "prompt" not in text.replace("prompt_template_hash", "")
    assert artifact.steps[0].value is not None and artifact.steps[0].value.from_input == "member_id"


def test_compiler_uses_vocabulary_keys_and_outcome_mappings(artifact: CapabilityArtifact) -> None:
    text = json.dumps(artifact.model_dump(mode="json"))
    assert "vocab:share_savings" in text and "vocab:current_balance" in text
    search = next(s for s in artifact.steps if s.states)
    assert {m.outcome_code for m in search.states} == {"member_not_found", "validation_rejected"}
    assert {o.code for o in artifact.contract.business_outcomes} == {"member_not_found", "validation_rejected"}


def test_compiler_policy_is_narrow_and_covers_redirect_routes(artifact: CapabilityArtifact) -> None:
    routes = artifact.policy.allowed_routes
    assert "/members/lookup" in routes  # seen only as a redirect; needed for the not-found outcome
    assert not any(r.startswith("/__admin") for r in routes)
    assert artifact.contract.outputs["properties"]["savings_balance"]["x-cua-type"] == "money"


def test_hash_is_stable_and_tamper_evident(artifact: CapabilityArtifact, tmp_path: Path) -> None:
    assert artifact.hash_ok()
    again = CapabilityArtifact.model_validate(json.loads(canonical_json(artifact.model_dump(mode="json", exclude_none=True))))
    assert again.content_hash == again.compute_hash() == artifact.content_hash
    store = CapabilityStore(tmp_path)
    path = store.save(artifact, render_review(artifact))
    with pytest.raises(FileExistsError):
        store.save(artifact)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["steps"][0]["description"] = "tampered"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        store.load("read-savings-balance", "1.0.0")


def test_lifecycle_and_hash_bound_approval(artifact: CapabilityArtifact, tmp_path: Path) -> None:
    store = CapabilityStore(tmp_path)
    store.save(artifact)
    with pytest.raises(ValueError):
        store.transition(artifact, "approved", actor="x")  # must be validated first
    a = store.transition(artifact, "validated", actor="validator", run_ids=["r1"])
    a = store.transition(a, "approved", actor="alice")
    assert a.is_approved()
    assert store.load("read-savings-balance", "approved-latest").ref == a.ref
    edited = a.model_copy(update={"steps": [a.steps[0].model_copy(update={"description": "edited"}), *a.steps[1:]]})
    assert not edited.is_approved()  # an edit can never inherit an approval


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["steps"][0].update(value={"from_input": "ssn"}), "undeclared input"),
        (lambda d: d["steps"][0].update(idempotent=False, retry={"max_attempts": 2, "backoff_ms": 1}), "non-idempotent"),
        (lambda d: d["steps"][1].update(risk_class="irreversible"), "irreversible"),
        (lambda d: d.update(extract=[]), "without extractors"),
        (lambda d: d["steps"][1]["states"][0].update(outcome_code="nope"), "undeclared outcome"),
        (lambda d: d["capability"].update(version="1.0"), "semver"),
    ],
)
def test_structural_validation_rejects_bad_artifacts(artifact: CapabilityArtifact, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    data = json.loads(json.dumps(artifact.model_dump(mode="json", exclude_none=True)))
    mutate(data)
    with pytest.raises(ValueError, match=message):
        CapabilityArtifact.model_validate(data)


def test_semver_bump_rules(artifact: CapabilityArtifact) -> None:
    contract = artifact.contract.model_copy(update={"side_effects": "reversible"})
    assert bump_version(artifact, artifact.model_copy(update={"contract": contract})) == "2.0.0"
    steps = [artifact.steps[0].model_copy(update={"timeout_ms": 20_000}), *artifact.steps[1:]]
    assert bump_version(artifact, artifact.model_copy(update={"steps": steps})) == "1.1.0"
    assert bump_version(artifact, artifact) == "1.0.1"
    _ = RetryPolicy


def test_authored_patch_cannot_lower_risk(artifact: CapabilityArtifact, profile) -> None:  # type: ignore[no-untyped-def]
    raw = json.loads((ROOT / "apps/synthcore/authored/open-sub-account.commit.json").read_text(encoding="utf-8"))
    raw["append_steps"][0]["risk_class"] = "read_only"  # an author tries to under-declare
    raw["append_steps"][0]["idempotent"] = True
    raw["append_steps"][0]["precondition"] = None
    raw["success"]["checks"] = raw["success"]["checks"][1:2]
    raw.pop("side_effects")
    patch = AuthoredPatch.model_validate(raw)
    v2 = apply_patch(artifact, patch, profile)
    commit = v2.steps[-1]
    assert commit.risk_class == RiskClass.IRREVERSIBLE and not commit.idempotent and commit.review_required
    assert commit.provenance == "authored" and v2.contract.side_effects == "irreversible"
    assert v2.capability.version == "2.0.0" and v2.provenance.parent == artifact.ref
    assert v2.lifecycle.status == "draft" and v2.hash_ok()
    assert commit.action == ActionKind.CLICK
