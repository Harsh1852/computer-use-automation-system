"""Overlays, tool specs and approval — the parts that need no browser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.catalog import (
    CapabilityRegistry,
    InsertedStep,
    Overlay,
    OverlayError,
    StabilityReport,
    apply_overlay,
    catalog_entry,
    parameter_schema,
    promote,
    tool_spec,
)
from cua.replay import AssistedFallback
from cua.schema import (
    ActionType,
    ApprovalState,
    Capability,
    Risk,
    Step,
    Success,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    TextPresent,
    Timing,
    ValueRef,
)

BASE = Path("artifacts/lookup_member_balance.v1.json")
SUMMIT = Path("artifacts/overlays/summit-cu.json")


@pytest.fixture
def capability() -> Capability:
    return Capability.model_validate_json(BASE.read_text(encoding="utf-8"))


@pytest.fixture
def overlay() -> Overlay:
    return Overlay.load(SUMMIT)


# --------------------------------------------------------------- overlays


def test_the_overlay_renames_controls_without_touching_the_flow(capability, overlay):
    tenant = apply_overlay(capability, overlay)

    assert tenant.target.tenant_id == "summit-cu"
    assert tenant.target.vendor_product == capability.target.vendor_product
    assert len(tenant.steps) == len(capability.steps), "no step added or removed"
    assert [s.action for s in tenant.steps] == [s.action for s in capability.steps]
    assert [s.risk for s in tenant.steps] == [s.risk for s in capability.steps]


def test_frame_names_and_anchors_are_remapped(capability, overlay):
    tenant = apply_overlay(capability, overlay)
    member_step = next(s for s in tenant.steps if s.value and s.value.param == "member_id")

    assert member_step.frame_path == ["main"]
    near = next(
        c for c in member_step.target.ladder if c.strategy is TargetStrategy.NEAR_TEXT
    )
    assert near.anchor == "ACCOUNT NUMBER", "Summit calls the same field something else"


def test_routes_pick_up_the_tenant_prefix(capability, overlay):
    tenant = apply_overlay(capability, overlay)
    urls = [
        s.postcondition.pattern
        for s in tenant.steps
        if s.postcondition.kind == "url_matches"
    ]
    assert any("/t/summit-cu/" in u for u in urls), urls
    assert tenant.target.entry_point == "http://app:5000/t/summit-cu/login"
    assert tenant.steps[0].value.literal == "http://app:5000/t/summit-cu/login"


def test_the_base_artifact_is_never_mutated(capability, overlay):
    before = capability.model_dump_json()
    apply_overlay(capability, overlay)
    assert capability.model_dump_json() == before


def test_an_overlay_for_the_wrong_capability_is_refused(capability, overlay):
    wrong = overlay.model_copy(update={"capability_id": "something_else"})
    with pytest.raises(OverlayError, match="targets"):
        apply_overlay(capability, wrong)


def _select_step() -> Step:
    return Step(
        id="s99",
        intent="Choose the servicing branch this tenant requires",
        action=ActionType.SELECT,
        frame_path=["contentFrame"],
        target=TargetSpec(
            primary=TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT, role="combobox",
                anchor="BRANCH", index=0, matches_at_record=1,
            ),
            recorded={"winning_strategy": "near_text", "candidates_seen": 1},
        ),
        value=ValueRef(literal="001 - MAIN"),
        postcondition=TextPresent(text="BRANCH"),
        risk=Risk.SAFE_REVERSIBLE,
        timing=Timing(observed_ms_p50=50, timeout_ms=5000),
    )


def test_an_overlay_may_add_a_field_this_tenant_requires(capability, overlay):
    """Summit's sub-account form has a BRANCH dropdown the base does not."""
    extended = overlay.model_copy(
        update={"inserted_steps": [InsertedStep(after="s5", step=_select_step())]}
    )
    tenant = apply_overlay(capability, extended)

    assert len(tenant.steps) == len(capability.steps) + 1
    assert [s.id for s in tenant.steps] == [f"s{i + 1}" for i in range(len(tenant.steps))]

    inserted = tenant.steps[5]
    assert inserted.action is ActionType.SELECT
    assert inserted.target.primary.anchor == "BRANCH"
    assert inserted.frame_path == ["main"], "an inserted step is remapped too"

    # Renumbering must not orphan the output binding.
    produced = {s.output.name for s in tenant.steps if s.output}
    assert produced == {f.name for f in tenant.contract.outputs}


def test_an_overlay_may_not_insert_a_click():
    """That is a flow change, and a flow change requires a fork.

    The refusal surfaces as a pydantic ValidationError because the rule is a
    model validator - OverlayError is a ValueError and pydantic wraps it.
    What matters is that the overlay cannot be constructed at all.
    """
    from pydantic import ValidationError

    click = _select_step().model_copy(
        update={"action": ActionType.CLICK, "value": None}
    )
    with pytest.raises(ValidationError, match="only insert a type or select"):
        InsertedStep(after="s5", step=click)


def test_an_overlay_may_not_introduce_a_state_changing_step():
    from pydantic import ValidationError

    risky = _select_step().model_copy(update={"risk": Risk.STATE_CHANGING})
    with pytest.raises(ValidationError, match="safe_reversible"):
        InsertedStep(after="s5", step=risky)


def test_an_inserted_step_must_attach_to_a_step_that_exists(capability, overlay):
    extended = overlay.model_copy(
        update={"inserted_steps": [InsertedStep(after="s99", step=_select_step())]}
    )
    with pytest.raises(OverlayError, match="does not contain"):
        apply_overlay(capability, extended)


# --------------------------------------------------------------- registry


def test_the_registry_refuses_a_tenant_with_no_variant():
    """Running one tenant's locators against another is worse than refusing:
    it is how automation types into the wrong field and calls it success."""
    registry = CapabilityRegistry()
    assert registry.get("lookup_member_balance", "summit-cu") is not None
    assert registry.get("lookup_member_balance", "some-other-bank") is None


def test_the_registry_lists_the_tenants_a_capability_serves():
    assert CapabilityRegistry().tenants_for("lookup_member_balance") == [
        "meridian",
        "summit-cu",
    ]


# -------------------------------------------------------------- tool specs


def test_the_tool_spec_is_mechanical_from_the_contract(capability):
    spec = tool_spec(capability)
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "lookup_member_balance"

    params = spec["function"]["parameters"]
    assert params["required"] == ["member_id"]
    assert params["properties"]["member_id"]["pattern"] == "^[0-9]{5}$"
    assert params["additionalProperties"] is False
    json.dumps(spec)  # must be handed straight to a model


def test_declared_outcomes_reach_the_calling_agent(capability):
    """A model that does not know MEMBER_NOT_FOUND is possible will treat it
    as a failure and retry, which is the conflation we are avoiding."""
    description = tool_spec(capability)["function"]["description"]
    assert "MEMBER_NOT_FOUND" in description
    assert "do not retry" in description.lower()


def test_an_irreversible_capability_says_so_to_its_caller():
    subaccount = Capability.model_validate_json(
        Path("artifacts/open_subaccount.v1.json").read_text(encoding="utf-8")
    )
    description = tool_spec(subaccount)["function"]["description"]
    assert "irreversible" in description.lower()

    entry = catalog_entry(subaccount)
    assert entry["irreversible_steps"]
    assert entry["approval"] in ("draft", "approved")


def test_a_regulated_parameter_never_carries_an_example(capability):
    """The schema forbids it, so the catalog cannot leak one either."""
    for name, prop in parameter_schema(capability)["properties"].items():
        param = next(p for p in capability.contract.inputs if p.name == name)
        if param.sensitivity.restricted:
            assert "examples" not in prop


# -------------------------------------------------------------- stability


def _success(outputs: dict) -> Success:
    return Success(
        capability_id="c", capability_version="1.0.0", run_id="r",
        evidence_ref="e", outputs=outputs, duration_ms=100,
    )


def _report(**kw) -> StabilityReport:
    return StabilityReport(capability_id="c", capability_version="1.0.0", **kw)


def test_a_read_only_capability_must_return_the_same_answer_every_time():
    report = _report(declared_outputs=["balance"], creates_something=False)
    for value in ("1234.56", "1234.56", "9999.99"):
        report.record(_success({"balance": value}))

    ok, reason = report.qualifies(min_runs=3)
    assert not ok
    assert "disagreed" in reason


def test_a_creating_capability_must_return_an_answer_not_the_same_answer():
    """A new account number is supposed to be new. Requiring identical
    outputs would make a correct capability unapprovable."""
    report = _report(declared_outputs=["new_account_number"], creates_something=True)
    for value in ("10001-S91", "10001-S92", "10001-S93"):
        report.record(_success({"new_account_number": value}))

    ok, reason = report.qualifies(min_runs=3)
    assert ok, reason
    assert "values differ" in reason


def test_a_creating_capability_that_returns_nothing_is_not_approved():
    report = _report(declared_outputs=["new_account_number"], creates_something=True)
    report.record(_success({"new_account_number": "10001-S91"}))
    report.record(_success({}))
    report.record(_success({"new_account_number": "10001-S93"}))

    ok, reason = report.qualifies(min_runs=3)
    assert not ok
    assert "2/3" in reason


def test_a_consistent_business_outcome_is_stable_but_not_approvable():
    """Asking for a member who does not exist should answer the same way
    every time — stable, and not a reason to approve the capability."""
    from cua.schema import BusinessOutcomeResult

    report = _report()
    for _ in range(3):
        report.record(
            BusinessOutcomeResult(
                capability_id="c", capability_version="1.0.0", run_id="r",
                evidence_ref="e", code="MEMBER_NOT_FOUND", message="m", at_step="s6",
            )
        )
    assert report.stability == 1.0
    ok, reason = report.qualifies(min_runs=3)
    assert not ok
    assert "modal outcome" in reason


def test_promotion_records_history_even_when_it_refuses(capability):
    report = _report(declared_outputs=["savings_balance"])
    report.record(_success({"savings_balance": "1.00"}))

    updated, ok, reason = promote(capability, report, min_runs=3)
    assert not ok
    assert updated.approval.state is ApprovalState.DRAFT
    assert updated.approval.replays == capability.approval.replays + 1
    assert updated.approval.notes == reason
    assert updated.approval.approved_by is None


def test_promotion_sets_the_state_the_gate_reads(capability):
    report = _report(declared_outputs=["savings_balance"])
    for _ in range(4):
        report.record(_success({"savings_balance": "1234.56"}))

    updated, ok, _ = promote(capability, report, min_runs=3)
    assert ok
    assert updated.approval.state is ApprovalState.APPROVED
    assert updated.approval.last_verified is not None
    assert updated.approval.stability == 1.0


# ------------------------------------------------------- assisted fallback


def test_the_fallback_is_unavailable_without_configuration(monkeypatch):
    """The default replay path has no model in it at all."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert AssistedFallback().available is False


def test_the_fallback_may_run_only_once_per_run():
    fallback = AssistedFallback(client=object(), model="test-model")
    assert fallback.available is True

    fallback._record(
        type("S", (), {"id": "s6"})(), "a button", 3, 11, True, "found it"
    )
    assert fallback.spent is True
    assert fallback.available is False, "a drift absorber that repeats is a model in the loop"


def test_every_invocation_is_evidence_whether_or_not_it_helped():
    fallback = AssistedFallback(client=object(), model="test-model")
    fallback._record(type("S", (), {"id": "s6"})(), "a button", 3, -1, False, "declined")

    evidence = fallback.as_evidence()
    assert len(evidence) == 1
    assert evidence[0]["resolved"] is False
    assert evidence[0]["note"] == "declined"
    assert evidence[0]["model"] == "test-model"


async def test_the_fallback_can_only_return_a_node_from_what_it_was_shown():
    """It cannot propose an action, skip a step, or write a selector: the
    return type makes those unexpressible."""
    from cua.schema import Observation, UiNode

    class Model:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                async def create(**_kw):
                    content = json.dumps({"ref": 42})
                    message = type("M", (), {"content": content})()
                    return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    step = Step(
        id="s6",
        intent="Run the member search",
        action=ActionType.CLICK,
        frame_path=[],
        target=TargetSpec(
            primary=TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="FIND"
            ),
            recorded={"winning_strategy": "a11y_role_name", "candidates_seen": 1},
        ),
        postcondition=TextPresent(text="x"),
        timing=Timing(observed_ms_p50=10, timeout_ms=5000),
    )
    observation = Observation(
        url="http://app:5000/app",
        nodes=[UiNode(ref=7, role="button", name="SEARCH")],
    )

    fallback = AssistedFallback(client=Model(), model="test-model")
    candidate = await fallback.reidentify(step, observation)

    assert candidate is None, "ref 42 was never offered, so it must be refused"
    assert fallback.as_evidence()[0]["chosen_ref"] == 42
    assert "not offered" in fallback.as_evidence()[0]["note"]
