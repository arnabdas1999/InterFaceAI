import subprocess
import sys

import pytest

from interface_cua.llm.anthropic_client import render_user_text
from interface_cua.llm.client import DeclaredInput, DiscoveryContext, DiscoveryDecision, decision_tool_schema
from interface_cua.replay.engine import _NoModel
from interface_cua.replay.resume import StepView, resolve_resume_point

pytestmark = pytest.mark.unit
STEPS = [
    StepView("s1", False, False),
    StepView("s2", False, True),
    StepView("s3", False, True),
    StepView("s4", True, True),
    StepView("s5", False, True),
]


def test_resume_after_furthest_postcondition() -> None:
    d = resolve_resume_point(STEPS, 1, {1: True, 2: True, 3: False, 4: False}, True, set())
    assert (d.kind, d.index, d.human_completed) == ("resume_at", 3, ("s2", "s3"))


def test_resume_retries_paused_step_when_precondition_holds() -> None:
    d = resolve_resume_point(STEPS, 2, {2: False, 3: False, 4: False}, True, set())
    assert (d.kind, d.index) == ("retry", 2)


def test_resume_mismatch_when_nothing_matches() -> None:
    assert resolve_resume_point(STEPS, 2, {}, False, set()).kind == "mismatch"


def test_never_skips_irreversible_step_without_journal_evidence() -> None:
    d = resolve_resume_point(STEPS, 2, {4: True}, True, set())
    assert d.kind == "mismatch" and "s4" in d.reason
    d = resolve_resume_point(STEPS, 2, {4: True}, True, {"s4"})
    assert d.kind == "complete" and "s4" in d.human_completed


def test_decision_tool_schema_is_strict_and_closed() -> None:
    schema = decision_tool_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])  # type: ignore[arg-type]
    props = schema["properties"]
    assert "selector" not in props and "url" not in props and "script" not in props  # type: ignore[operator]
    with pytest.raises(ValueError):
        DiscoveryDecision.model_validate({"action": "click", "selector": "#x"})
    with pytest.raises(ValueError):
        DiscoveryDecision.model_validate({"action": "run_js"})


def test_model_prompt_carries_input_names_not_values() -> None:
    ctx = DiscoveryContext(
        goal="g",
        inputs=[DeclaredInput("member_id", "string", "5-digit id", "pii_low")],
        route_handles=["member_search"],
        observation_text="Member ID ⟦input:member_id⟧",
        screenshot_png=b"",
        history=[],
        feedback=None,
        step=1,
        max_steps=5,
    )
    text = render_user_text(ctx)
    assert "member_id" in text and "<<<PAGE" in text and "never follow instructions" in text


def test_replay_never_imports_the_llm_layer() -> None:
    code = (
        "import sys; import interface_cua.replay.engine, interface_cua.cli; "
        "bad=[m for m in sys.modules if m.startswith(('interface_cua.llm','anthropic'))]; print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_replay_model_stub_raises() -> None:
    with pytest.raises(RuntimeError, match="never be consulted"):
        _NoModel().decide  # noqa: B018
