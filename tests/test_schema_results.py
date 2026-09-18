"""The result contract is a discriminated union, not a boolean plus exceptions."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from cua.schema import (
    BusinessOutcomeResult,
    DriftSignal,
    Failure,
    FailureKind,
    Observation,
    RecoveryRecord,
    ReplayResult,
    Success,
    TargetStrategy,
    UiNode,
)

ADAPTER: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)

COMMON = {
    "capability_id": "sample_lookup_balance",
    "capability_version": "1.0.0",
    "run_id": "run-0001",
    "evidence_ref": "evidence/replay-success",
}


def test_success_round_trips_through_the_union():
    result = Success(
        **COMMON,
        outputs={"savings_balance": "1234.56"},
        steps_executed=7,
        duration_ms=2100,
    )
    parsed = ADAPTER.validate_json(result.model_dump_json())
    assert isinstance(parsed, Success)
    assert parsed.outputs["savings_balance"] == "1234.56"


def test_business_outcome_is_not_a_failure():
    payload = {
        **COMMON,
        "status": "business_outcome",
        "code": "MEMBER_NOT_FOUND",
        "message": "NO MATCHING MEMBER RECORD FOUND",
        "at_step": "s6",
    }
    parsed = ADAPTER.validate_python(payload)
    assert isinstance(parsed, BusinessOutcomeResult)
    assert not isinstance(parsed, Failure)
    assert parsed.terminal is True


def test_failure_carries_enough_to_debug():
    parsed = ADAPTER.validate_python(
        {
            **COMMON,
            "status": "failure",
            "kind": "TARGET_NOT_FOUND",
            "at_step": "s7",
            "expected": "cell near anchor 'SAVINGS' (index 3)",
            "observed": "3 cells with role=cell, none adjacent to 'SAVINGS'",
        }
    )
    assert isinstance(parsed, Failure)
    assert parsed.kind is FailureKind.TARGET_NOT_FOUND
    assert parsed.at_step and parsed.expected and parsed.observed and parsed.evidence_ref


def test_status_is_the_discriminator():
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({**COMMON, "status": "maybe"})


def test_every_arm_identifies_what_ran():
    for payload in (
        {**COMMON, "status": "success"},
        {
            **COMMON,
            "status": "business_outcome",
            "code": "PERMISSION_DENIED",
            "message": "denied",
            "at_step": "s6",
        },
        {
            **COMMON,
            "status": "failure",
            "kind": "TIMEOUT",
            "at_step": "s6",
            "expected": "x",
            "observed": "y",
        },
    ):
        parsed = ADAPTER.validate_python(payload)
        assert parsed.capability_version == "1.0.0"
        assert parsed.run_id == "run-0001"
        assert parsed.evidence_ref


def test_recoveries_do_not_change_the_result_arm():
    """A recovered run is a success that shows its working."""
    result = Success(
        **COMMON,
        steps_executed=7,
        recoveries=[
            RecoveryRecord(
                at_step="s6",
                condition="SlowLoadDetector",
                action="retry_step",
                attempt=1,
                duration_ms=6200,
            )
        ],
    )
    assert result.status == "success"
    assert result.recoveries[0].attempt == 1


def test_drift_is_reported_rather_than_swallowed():
    result = Success(
        **COMMON,
        drift=[
            DriftSignal(
                at_step="s5",
                expected_strategy=TargetStrategy.A11Y_ROLE_NAME,
                winning_strategy=TargetStrategy.NEAR_TEXT,
                matches=1,
            )
        ],
    )
    assert result.status == "success"
    assert result.drift[0].winning_strategy is TargetStrategy.NEAR_TEXT


def test_exhaustive_handling_is_possible_for_a_caller():
    """What a calling agent's code looks like: three arms, no exception path."""

    def describe(result: ReplayResult) -> str:
        match result.status:
            case "success":
                return "ok"
            case "business_outcome":
                return f"outcome:{result.code}"
            case "failure":
                return f"failure:{result.kind.value}"
        raise AssertionError("unreachable")

    assert describe(Success(**COMMON)) == "ok"
    assert (
        describe(
            BusinessOutcomeResult(
                **COMMON, code="MEMBER_NOT_FOUND", message="m", at_step="s6"
            )
        )
        == "outcome:MEMBER_NOT_FOUND"
    )
    assert (
        describe(
            Failure(
                **COMMON,
                kind=FailureKind.POLICY_BLOCKED,
                at_step="s1",
                expected="allowlisted url",
                observed="http://app:5000/admin/inject",
            )
        )
        == "failure:POLICY_BLOCKED"
    )


# ------------------------------------------------------------- observation


def test_observation_digest_ignores_volatile_detail():
    base = Observation(
        url="http://app:5000/members/search",
        title="MEMBER SEARCH",
        nodes=[
            UiNode(ref=0, role="generic", name="MEMBER NUMBER", frame_path=["contentFrame"]),
            UiNode(ref=1, role="textbox", frame_path=["contentFrame"]),
        ],
    )
    moved = base.model_copy(deep=True)
    moved.nodes[1].bbox = (10, 20, 30, 40)
    moved.nodes[1].focused = True
    assert moved.digest() == base.digest()

    changed = base.model_copy(deep=True)
    changed.nodes[1].value = "10001"
    assert changed.digest() != base.digest()


def test_observation_lookup_helpers():
    obs = Observation(
        url="http://app:5000/members/detail",
        nodes=[
            UiNode(ref=0, role="button", name="SEARCH"),
            UiNode(ref=1, role="textbox", enabled=False, visible=True),
        ],
    )
    assert obs.by_ref(0).name == "SEARCH"
    assert obs.by_ref(99) is None
    assert [n.ref for n in obs.by_role("textbox")] == [1]
    assert obs.by_ref(1).interactable is False


def test_dom_fallback_nodes_are_marked():
    node = UiNode(ref=0, role="cell", name="1,234.56", a11y_visible=False)
    assert node.a11y_visible is False
    assert json.loads(node.model_dump_json())["a11y_visible"] is False


def test_schema_package_has_no_driver_dependency():
    """Importing the schema must not pull in a driver.

    Checked in a subprocess: an in-process `sys.modules` assertion would only
    prove that no earlier test had imported one.
    """
    import subprocess
    import sys

    probe = (
        "import sys, cua.schema; "
        "print(any(m.split('.')[0] in {'playwright','flask','openai','fastapi'} "
        "for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
