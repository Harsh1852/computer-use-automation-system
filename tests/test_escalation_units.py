"""The control-transfer model, without a browser.

The lease is the whole escalation design in one object, so it is worth
testing on its own: who may act, what an expired hold does, and what a
handoff records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cua.escalation import (
    AUTOMATION,
    Handback,
    HumanCapture,
    InterventionReason,
    InterventionRequest,
    InterventionStore,
    LeaseManager,
    LeaseViolation,
)


def _lease() -> LeaseManager:
    return LeaseManager("sess-1", hold_seconds=60)


# ------------------------------------------------------- the single writer


def test_automation_holds_the_session_by_default():
    lease = _lease()
    assert lease.lease.holder == AUTOMATION
    lease.assert_holder(AUTOMATION)  # does not raise
    assert lease.lease.expires_at is None, "automation holds are not time-boxed"


def test_automation_cannot_act_while_an_operator_holds_it():
    """The invariant the whole handoff rests on. `Surface.act()` asks this
    before touching the browser, so a race is an exception, not a bug hunt."""
    lease = _lease()
    lease.take("operator investigating")

    with pytest.raises(LeaseViolation, match="operator holds"):
        lease.assert_holder(AUTOMATION)


def test_the_operator_token_is_what_authorises_an_operator():
    lease = _lease()
    token = lease.take("investigating")

    lease.assert_holder(token)
    with pytest.raises(LeaseViolation):
        lease.assert_holder("some-other-token")


def test_release_returns_control_and_records_the_note():
    lease = _lease()
    token = lease.take("investigating")
    lease.release(token, "signed back in")

    assert lease.lease.holder == AUTOMATION
    assert lease.operator_note == "signed back in"
    lease.assert_holder(AUTOMATION)


def test_only_the_current_holder_may_release():
    lease = _lease()
    lease.take("investigating")
    with pytest.raises(LeaseViolation, match="token does not match"):
        lease.release("stale-token")


def test_taking_twice_is_refused():
    lease = _lease()
    lease.take("first")
    with pytest.raises(LeaseViolation, match="already holds"):
        lease.take("second")


# ------------------------------------------------------------- hand-back


async def test_a_release_wakes_the_parked_executor():
    lease = _lease()
    token = lease.take("investigating")

    async def operator():
        await asyncio.sleep(0.05)
        lease.release(token, "fixed")

    asyncio.get_running_loop().create_task(operator())
    assert await lease.wait_for_handback(timeout_s=5) is Handback.RESUMED


async def test_an_abort_is_distinct_from_a_release():
    lease = _lease()
    token = lease.take("investigating")
    lease.abort(token, "cannot be fixed")
    assert await lease.wait_for_handback(timeout_s=5) is Handback.ABORTED


async def test_an_expired_hold_terminates_rather_than_reverting():
    """The property that matters most. If a person took control because
    something was wrong and then walked away, resuming automation is the
    worst available option: the page is in whatever state they left it."""
    lease = _lease()
    lease.take("investigating")

    assert await lease.wait_for_handback(timeout_s=0.1) is Handback.TIMED_OUT
    assert lease.lease.holder == "operator", "control was never handed back"
    with pytest.raises(LeaseViolation):
        lease.assert_holder(AUTOMATION)


def test_an_operator_hold_is_time_boxed():
    lease = LeaseManager("sess-1", hold_seconds=1)
    lease.take("investigating")
    assert lease.lease.expires_at is not None
    assert 0 < lease.lease.seconds_remaining <= 1


# --------------------------------------------------------- human capture


class _Log:
    """Mirrors `RunLog.event(event, **fields)` exactly.

    The first parameter has to be called `event`, not `name`: a stub with a
    different signature would have hidden that `name` is a perfectly legal
    field for a caller to pass.
    """

    def __init__(self):
        self.events = []

    def event(self, event, **fields):
        self.events.append((event, fields))


def test_a_password_entry_records_the_field_but_not_even_its_length():
    log = _Log()
    capture = HumanCapture(log)
    capture({"kind": "change", "role": "textbox", "name": "PASSWORD",
             "is_password": True, "value_len": 8, "frame_path": []})

    step = capture.steps[0]
    assert step.sensitive is True
    assert step.value is None
    assert step.name == "PASSWORD", "which field was touched is the point"


def test_other_values_are_recorded_as_a_shape_never_content():
    log = _Log()
    capture = HumanCapture(log)
    capture({"kind": "change", "role": "textbox", "name": "MEMBER NUMBER",
             "is_password": False, "value_len": 5, "frame_path": ["contentFrame"]})

    step = capture.steps[0]
    assert step.value == "<redacted:5 chars>"
    assert step.frame_path == ["contentFrame"]


def test_human_actions_land_in_the_run_log_as_they_happen():
    """Logging on arrival is what interleaves the human's steps with the
    machine's, rather than merging two lists afterwards."""
    log = _Log()
    capture = HumanCapture(log)
    capture({"kind": "click", "role": "button", "name": "SIGN ON",
             "is_password": False, "value_len": 0, "frame_path": []})

    assert log.events[0][0] == "human.action"
    assert log.events[0][1]["name"] == "SIGN ON"


# ------------------------------------------------------------ the store


def _request(session="sess-1") -> InterventionRequest:
    return InterventionRequest(
        id="iv-1", session_id=session, capability_id="c", capability_version="1.0.0",
        goal="g", at_step="s4", step_intent="Sign on", reason=InterventionReason.UNRECOVERABLE,
        observed="login page reappeared", expected="an authenticated session",
        redacted_params={"member_id": "10001"}, created_at=datetime.now(timezone.utc),
    )


def test_an_open_request_is_findable_by_session_and_closes_once():
    store = InterventionStore()
    store.add(_request())

    assert store.by_session("sess-1").id == "iv-1"
    store.resolve("iv-1", "resumed", "signed back in")

    assert store.by_session("sess-1") is None, "a resolved request is not open"
    assert store.get("iv-1").resolution == "resumed"
    assert store.get("iv-1").operator_note == "signed back in"


def test_the_request_carries_enough_context_to_act_on():
    request = _request()
    for field in ("capability_id", "at_step", "step_intent", "expected", "observed"):
        assert getattr(request, field), f"{field} is what makes the request actionable"
    assert request.redacted_params["member_id"] == "10001"
