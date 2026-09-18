"""Phase 2 acceptance: the artifact schema holds its own invariants.

These tests are the executable half of the six defences documented at the top
of ``schema/artifact.py``. Each one asserts that a specific mistake is
*unrepresentable*, not merely discouraged.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from cua.schema import (
    ActionType,
    Approval,
    ApprovalState,
    Capability,
    InputParam,
    OutputBinding,
    RecordedEvidence,
    Risk,
    Sensitivity,
    Step,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    TextPresent,
    Timing,
    ValueRef,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_capability.v1.json"


@pytest.fixture
def raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def capability(raw: dict) -> Capability:
    return Capability.model_validate(raw)


# ---------------------------------------------------------------- acceptance


def test_json_schema_is_valid_json_schema():
    schema = Capability.model_json_schema()
    # Checks the emitted document against the 2020-12 meta-schema, not just
    # that it is a dict.
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["title"] == "Capability"
    assert "steps" in schema["properties"]
    json.dumps(schema)  # must be serialisable for the catalog endpoint


def test_fixture_round_trips(capability: Capability):
    reloaded = Capability.model_validate_json(capability.model_dump_json())
    assert reloaded == capability


def test_round_trip_preserves_every_authored_key(raw: dict, capability: Capability):
    """Serialising must not quietly drop something the author wrote."""
    dumped = json.loads(capability.model_dump_json())

    def assert_subset(expected, actual, path="$"):
        if isinstance(expected, dict):
            for key, value in expected.items():
                assert key in actual, f"{path}.{key} was dropped on serialisation"
                assert_subset(value, actual[key], f"{path}.{key}")
        elif isinstance(expected, list):
            assert len(expected) == len(actual), f"{path} changed length"
            for i, value in enumerate(expected):
                assert_subset(value, actual[i], f"{path}[{i}]")
        else:
            assert expected == actual, f"{path}: {expected!r} != {actual!r}"

    assert_subset(raw, dumped)


def test_fixture_is_the_shape_we_think_it_is(capability: Capability):
    assert capability.ref == "sample_lookup_balance@1.0.0"
    assert [s.id for s in capability.steps] == [f"s{i}" for i in range(1, 8)]
    assert capability.step("s7").action is ActionType.READ
    assert not capability.has_irreversible_step
    assert capability.brittle_only_steps == []
    assert {o.code for o in capability.outcomes} == {"MEMBER_NOT_FOUND", "PERMISSION_DENIED"}


# ------------------------------------------- defence 1: no inlined regulated data


def test_pii_tagged_literal_is_rejected():
    with pytest.raises(ValidationError, match="may not be tagged pii"):
        ValueRef(literal="123-45-6789", sensitivity=Sensitivity.PII)


def test_secret_tagged_literal_is_rejected():
    with pytest.raises(ValidationError, match="may not be tagged secret"):
        ValueRef(literal="hunter2", sensitivity=Sensitivity.SECRET)


def test_literal_that_looks_like_an_ssn_is_rejected_even_when_untagged():
    """The tag is a promise; the structural screen is the enforcement."""
    with pytest.raises(ValidationError, match="looks like ssn"):
        ValueRef(literal="member 123-45-6789 called in")


def test_literal_that_looks_like_a_card_number_is_rejected():
    with pytest.raises(ValidationError, match="looks like card"):
        ValueRef(literal="4111 1111 1111 1111")


def test_long_identifiers_that_are_not_cards_are_allowed():
    """A screen that fires on every long number would be switched off."""
    assert ValueRef(literal="/members/detail?mbr=1234567890123456").literal


def test_secret_ref_is_forced_to_secret_sensitivity():
    assert ValueRef(secret_ref="APP_PASSWORD").sensitivity is Sensitivity.SECRET


def test_value_ref_requires_exactly_one_source():
    with pytest.raises(ValidationError, match="exactly one"):
        ValueRef()
    with pytest.raises(ValidationError, match="exactly one"):
        ValueRef(param="member_id", literal="10001")


def test_secret_ref_must_look_like_an_env_var():
    with pytest.raises(ValidationError):
        ValueRef(secret_ref="app_password")


def test_example_values_are_forbidden_on_restricted_params():
    with pytest.raises(ValidationError, match="regulated data in the catalog"):
        InputParam(
            name="ssn",
            sensitivity=Sensitivity.PII,
            description="Member SSN",
            example="123-45-6789",
        )


def test_example_must_match_its_own_pattern():
    with pytest.raises(ValidationError, match="does not match its own pattern"):
        InputParam(
            name="member_id",
            pattern=r"^\d{5}$",
            description="Member number",
            example="ABC",
        )


# ------------------------------------------------ defence 2: postconditions


def _timing() -> Timing:
    return Timing(observed_ms_p50=10, timeout_ms=5000)


def test_step_without_a_postcondition_is_rejected():
    with pytest.raises(ValidationError):
        Step(
            id="s1",
            intent="Open the page",
            action=ActionType.NAVIGATE,
            value=ValueRef(literal="http://app:5000/login"),
            timing=_timing(),
        )


def test_a_wait_step_cannot_express_a_fixed_sleep():
    """No target, no value — a wait can only wait on an observable condition."""
    with pytest.raises(ValidationError, match="no way to express a fixed sleep"):
        Step(
            id="s1",
            intent="Wait a bit",
            action=ActionType.WAIT,
            value=ValueRef(literal="3000"),
            postcondition=TextPresent(text="CURRENT BALANCE"),
            timing=_timing(),
        )


def test_timeout_must_be_generous_relative_to_observed_latency():
    with pytest.raises(ValidationError, match="too tight"):
        Timing(observed_ms_p50=6000, timeout_ms=7000)
    assert Timing(observed_ms_p50=6000, timeout_ms=20000).timeout_ms == 20000


@pytest.mark.parametrize(
    "action,kwargs,message",
    [
        (ActionType.CLICK, {}, "click requires a target"),
        (ActionType.TYPE, {"target": "t"}, "type requires a value"),
        (ActionType.READ, {"target": "t"}, "read requires an output binding"),
        (ActionType.NAVIGATE, {"target": "t", "value": "v"}, "navigate addresses a URL"),
    ],
)
def test_step_shape_must_match_its_action(action, kwargs, message):
    target = TargetSpec(
        primary=TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"
        ),
        recorded=RecordedEvidence(
            winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
        ),
    )
    built = {
        "target": target if kwargs.get("target") else None,
        "value": ValueRef(literal="x") if kwargs.get("value") else None,
    }
    with pytest.raises(ValidationError, match=message):
        Step(
            id="s1",
            intent="Do the thing",
            action=action,
            postcondition=TextPresent(text="OK"),
            timing=_timing(),
            **built,
        )


# ---------------------------------------- defence 4: the ladder and its counts


def test_css_candidates_are_always_marked_brittle():
    candidate = TargetCandidate(strategy=TargetStrategy.CSS, value="#ctl00_btnSearch")
    assert candidate.brittle is True


def test_xpath_candidates_are_always_marked_brittle():
    candidate = TargetCandidate(strategy=TargetStrategy.XPATH, value="//td[1]", brittle=False)
    assert candidate.brittle is True


def test_strategy_must_carry_the_fields_it_needs():
    with pytest.raises(ValidationError, match="requires anchor"):
        TargetCandidate(strategy=TargetStrategy.NEAR_TEXT, role="textbox")
    with pytest.raises(ValidationError, match="requires role, name"):
        TargetCandidate(strategy=TargetStrategy.A11Y_ROLE_NAME)


def test_winning_strategy_must_be_in_the_ladder():
    with pytest.raises(ValidationError, match="not in the ladder"):
        TargetSpec(
            primary=TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"
            ),
            recorded=RecordedEvidence(
                winning_strategy=TargetStrategy.XPATH, candidates_seen=1
            ),
        )


def test_duplicate_rungs_are_rejected():
    rung = TargetCandidate(strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH")
    with pytest.raises(ValidationError, match="duplicate candidate"):
        TargetSpec(
            primary=rung,
            fallbacks=[rung.model_copy()],
            recorded=RecordedEvidence(
                winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
            ),
        )


def test_a_selector_only_ladder_is_flagged_for_review(raw: dict):
    raw["steps"][5]["target"]["primary"] = {
        "strategy": "css",
        "value": "#ctl00_ContentPlaceHolder1_btnSearch",
        "matches_at_record": 1,
    }
    raw["steps"][5]["target"]["fallbacks"] = []
    raw["steps"][5]["target"]["recorded"]["winning_strategy"] = "css"
    capability = Capability.model_validate(raw)
    assert [s.id for s in capability.brittle_only_steps] == ["s6"]


def test_candidates_seen_cannot_be_zero():
    with pytest.raises(ValidationError):
        RecordedEvidence(winning_strategy=TargetStrategy.CSS, candidates_seen=0)


# --------------------------------------------- cross-field artifact integrity


def test_undeclared_parameter_reference_is_rejected(raw: dict):
    raw["steps"][4]["value"] = {"param": "account_id"}
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability.model_validate(raw)


def test_undeclared_placeholder_in_a_url_template_is_rejected(raw: dict):
    raw["steps"][5]["postcondition"] = {
        "kind": "url_matches",
        "pattern": "**/members/detail?mbr={branch_code}",
    }
    with pytest.raises(ValidationError, match="undeclared input"):
        Capability.model_validate(raw)


def test_output_must_be_bound_by_a_read_step(raw: dict):
    raw["steps"][6].pop("output")
    raw["steps"][6]["action"] = "assert"
    with pytest.raises(ValidationError, match="no read step binds it"):
        Capability.model_validate(raw)


def test_output_from_step_must_agree_with_the_binding(raw: dict):
    raw["contract"]["outputs"][0]["from_step"] = "s5"
    with pytest.raises(ValidationError, match="but is bound by s7"):
        Capability.model_validate(raw)


def test_renaming_a_binding_orphans_its_declared_output(raw: dict):
    raw["steps"][6]["output"]["name"] = "closing_balance"
    with pytest.raises(ValidationError, match="no read step binds it"):
        Capability.model_validate(raw)


def test_step_cannot_bind_an_output_the_contract_never_declared(raw: dict):
    extra = copy.deepcopy(raw["steps"][6])
    extra["id"] = "s8"
    extra["output"]["name"] = "closing_balance"
    raw["steps"].append(extra)
    with pytest.raises(ValidationError, match="missing from the contract"):
        Capability.model_validate(raw)


def test_duplicate_step_ids_are_rejected(raw: dict):
    raw["steps"][2]["id"] = "s2"
    with pytest.raises(ValidationError, match="duplicate step ids"):
        Capability.model_validate(raw)


def test_duplicate_outcome_codes_are_rejected(raw: dict):
    raw["outcomes"][1]["code"] = "MEMBER_NOT_FOUND"
    with pytest.raises(ValidationError, match="duplicate business outcome"):
        Capability.model_validate(raw)


def test_unknown_keys_are_rejected(raw: dict):
    raw["steps"][0]["sleep_ms"] = 3000
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


def test_provenance_has_nowhere_to_put_a_transcript(raw: dict):
    raw["provenance"]["transcript"] = "system: you are an agent ..."
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


def test_provenance_hash_must_be_a_sha256(raw: dict):
    raw["provenance"]["transcript_sha256"] = "not-a-hash"
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


def test_a_capability_cannot_be_a_variant_of_itself(raw: dict):
    raw["target"]["variant_of"] = raw["id"]
    with pytest.raises(ValidationError, match="variant_of cannot point at"):
        Capability.model_validate(raw)


def test_dismiss_recovery_rule_needs_a_target(raw: dict):
    raw["recoveries"][0].pop("target")
    with pytest.raises(ValidationError, match="dismiss rule needs a target"):
        Capability.model_validate(raw)


def test_recovery_attempts_are_bounded(raw: dict):
    raw["recoveries"][0]["max_attempts"] = 10
    with pytest.raises(ValidationError):
        Capability.model_validate(raw)


# --------------------------------------------------------------- approval


def test_approval_must_be_earned():
    with pytest.raises(ValidationError, match="at least one verified replay"):
        Approval(state=ApprovalState.APPROVED)


def test_approval_with_history_is_accepted():
    approval = Approval(
        state=ApprovalState.APPROVED,
        replays=10,
        failures=1,
        last_verified=datetime(2026, 9, 18, tzinfo=timezone.utc),
        approved_by="ops@example.invalid",
    )
    assert approval.stability == pytest.approx(0.9)


def test_failures_cannot_exceed_replays():
    with pytest.raises(ValidationError, match="failures cannot exceed replays"):
        Approval(replays=1, failures=2)


def test_irreversible_steps_are_visible_to_a_reviewer(raw: dict):
    mutated = copy.deepcopy(raw)
    mutated["steps"][5]["risk"] = "irreversible"
    capability = Capability.model_validate(mutated)
    assert capability.has_irreversible_step
    assert [s.id for s in capability.irreversible_steps] == ["s6"]
    assert capability.approval.state is ApprovalState.DRAFT


# ------------------------------------------------------------- odds and ends


def test_output_binding_pattern_needs_exactly_one_group():
    with pytest.raises(ValidationError, match="exactly one capture group"):
        OutputBinding(name="balance", pattern=r"\d+")
    assert OutputBinding(name="balance", pattern=r"([\d,.]+)").pattern


def test_risk_defaults_to_the_safe_class(capability: Capability):
    assert capability.step("s1").risk is Risk.SAFE_REVERSIBLE
