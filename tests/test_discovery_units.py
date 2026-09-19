"""Discovery logic that needs neither a browser nor a model.

Runs in the driver-free image, so the recorder's judgement — what the model
may see, what gets bound to what — is verified without either dependency.
"""

from __future__ import annotations

import pytest

from cua.discovery import Recorder, render_for_model, substitute_secrets, system_prompt
from cua.discovery.agent import DiscoveryAgent
from cua.discovery.recorder import RecordedStep
from cua.schema import (
    ActionType,
    NameSource,
    Observation,
    Step,
    TextPresent,
    Timing,
    UiNode,
    ValueRef,
)


def _screen() -> Observation:
    return Observation(
        url="http://app:5000/app",
        title="MEMBER SEARCH",
        frame_urls={"": "http://app:5000/app"},
        nodes=[
            UiNode(ref=8, role="row", name="MEMBER NUMBER", frame_path=["contentFrame"], order=8),
            UiNode(
                ref=9, role="cell", name="MEMBER NUMBER", frame_path=["contentFrame"],
                order=9, container_ref=8,
            ),
            UiNode(
                ref=10, role="textbox", frame_path=["contentFrame"], order=10,
                container_ref=8, name_source=NameSource.NONE,
            ),
            UiNode(
                ref=11, role="button", name="SEARCH", frame_path=["contentFrame"],
                order=11, container_ref=8, name_source=NameSource.VALUE,
            ),
        ],
    )


# --------------------------------------------------------- what the model sees


def test_the_model_is_given_a_near_hint_for_unnamed_fields():
    assert '[10] textbox      (no name) near="MEMBER NUMBER"' in render_for_model(_screen())


def test_the_model_never_sees_markup_or_ids():
    rendered = render_for_model(_screen()).lower()
    for forbidden in ("<td", "<input", "ctl00", "css", "xpath", "#"):
        assert forbidden not in rendered


def test_credentials_reach_the_browser_but_not_the_model():
    assert substitute_secrets("{{APP_PASSWORD}}", {"APP_PASSWORD": "s3cret"}) == (
        "s3cret",
        "APP_PASSWORD",
    )
    assert substitute_secrets("10001", {}) == ("10001", None)


def test_an_unset_credential_is_an_error_not_a_guess():
    with pytest.raises(KeyError, match="APP_PASSWORD"):
        substitute_secrets("{{APP_PASSWORD}}", {})


# ------------------------------------------------------------- the two modes


def test_unattended_discovery_is_told_never_to_act_irreversibly():
    unattended = system_prompt(False)
    assert "Do NOT perform irreversible actions" in unattended
    assert "SUPERVISED" not in unattended


def test_supervised_recording_is_an_explicit_opt_in():
    supervised = system_prompt(True)
    assert "SUPERVISED recording session" in supervised
    assert "Do NOT perform irreversible actions" not in supervised


# --------------------------------------------------------------- stopping


def test_no_progress_is_detected_by_observation_digest():
    assert DiscoveryAgent._stalled(["a", "a", "a"])
    assert not DiscoveryAgent._stalled(["a", "a", "b"])
    assert not DiscoveryAgent._stalled(["a", "a"])


def test_reads_do_not_count_toward_the_no_progress_stop():
    """A read deliberately leaves the page identical. Counting it meant a run
    that read three values off one confirmation screen was killed as stalled
    while making perfect progress."""
    from cua.discovery.tools import MUTATING_TOOLS

    assert "read" not in MUTATING_TOOLS
    assert {"navigate", "click", "type", "select"} == MUTATING_TOOLS


# ----------------------------------------------------------------- anchors


def test_labels_are_preferred_over_data_as_anchors():
    assert Recorder._looks_like_a_label("CURRENT BALANCE")
    assert Recorder._looks_like_a_label("SAVINGS")
    assert not Recorder._looks_like_a_label("10001-S01")
    assert not Recorder._looks_like_a_label("Primary Share")


# -------------------------------------------------------- parameter binding


def _recorder_with_typed(values: list[str]) -> Recorder:
    recorder = Recorder(
        surface=None,  # _bind_inputs never touches the surface
        goal="g",
        entry_url="http://app:5000/login",
        tenant_id="meridian",
        vendor_product="MERIDIAN CoreBank",
        product_version="7.4.11",
        surface_kind="legacy_web",
    )
    recorder.steps = [
        RecordedStep(
            step=Step(
                id=f"s{i + 1}",
                intent="typed something",
                action=ActionType.ASSERT,
                value=ValueRef(literal=value),
                postcondition=TextPresent(text="x"),
                timing=Timing(observed_ms_p50=10, timeout_ms=5000),
            ),
            node_name=None,
            typed_text=value,
            output_name=None,
            ladder_counts={},
        )
        for i, value in enumerate(values)
    ]
    return recorder


def test_inputs_bind_to_the_value_the_run_actually_supplied():
    recorder = _recorder_with_typed(["10001", "SAVINGS", "VACATION", "50.00"])
    bound = recorder._bind_inputs(
        [
            {"name": "member_number", "description": "d", "example": "10001"},
            {"name": "account_type", "description": "d", "example": "SAVINGS"},
            {"name": "nickname", "description": "d", "example": "VACATION"},
            {"name": "initial_deposit", "description": "d", "example": "50.00"},
        ]
    )
    assert {k: v[0] for k, v in bound.items()} == {
        "member_number": "10001",
        "account_type": "SAVINGS",
        "nickname": "VACATION",
        "initial_deposit": "50.00",
    }


def test_an_unmatched_example_is_dropped_rather_than_bound_to_the_wrong_field():
    """The regression that matters.

    An earlier version fell back to "take the next unclaimed value", which
    bound `account_type` to the nickname field and `nickname` to the deposit
    field. Every replay would then have typed the wrong value into the wrong
    box. A dropped parameter is a visible gap; a mis-bound one is a landmine.
    """
    recorder = _recorder_with_typed(["VACATION", "50.00"])
    bound = recorder._bind_inputs(
        [
            {"name": "account_type", "description": "d", "example": "SAVINGS"},
            {"name": "nickname", "description": "d", "example": "VACATION"},
        ]
    )

    assert "account_type" not in bound, "an unmatched example must never be bound"
    assert bound["nickname"][0] == "VACATION"
    assert any("account_type" in note for note in recorder.skipped)


def test_a_parameter_the_run_never_supplied_is_reported_not_invented():
    recorder = _recorder_with_typed([])
    bound = recorder._bind_inputs(
        [{"name": "branch_code", "description": "d", "example": "001"}]
    )
    assert bound == {}
    assert recorder.skipped and "branch_code" in recorder.skipped[0]
