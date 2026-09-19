"""The handoff, end to end, on a live session.

The demo the brief singles out as the thing most submissions fake:

1. a replay is running
2. the session dies underneath it
3. the executor detects the auth wall, pauses, and raises an intervention
4. a human takes control of the *same live browser*, fixes it, releases
5. the executor resumes and returns Success
6. the evidence shows both sets of actions in one timeline

The operator here is a script rather than a person, and it drives the
console's own API — the same endpoints the HTML buttons post to. What it
cannot do is act while automation holds the lease, and what automation
cannot do is act while the operator holds it; both are asserted below.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path

import pytest

from cua.escalation import (
    AUTOMATION,
    HumanCapture,
    InterventionStore,
    LeaseManager,
    LeaseViolation,
    OperatorEscalator,
)
from cua.evidence import RunLog
from cua.replay import ReplayExecutor
from cua.schema import (
    Action,
    ActionType,
    Capability,
    Failure,
    FailureKind,
    Success,
    TargetCandidate,
    TargetStrategy,
)

pytestmark = pytest.mark.integration

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")
ARTIFACT = Path("artifacts/lookup_member_balance.v1.json")


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


@pytest.fixture
def capability() -> Capability:
    return Capability.model_validate_json(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture
async def session(tmp_path, capability):
    """A replay wired for escalation: lease, store, console, human capture."""
    pytest.importorskip("playwright")
    from cua.surface import registry

    admin("/admin/reset")
    run_dir = str(tmp_path / "escalation")
    lease = LeaseManager("sess-test", hold_seconds=120)
    store = InterventionStore()
    log = RunLog(run_dir, "escalation-test")
    human = HumanCapture(log, is_active=lambda: lease.held_by_operator)

    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        run_dir=run_dir,
        lease=lease,
    )
    await surface.start()
    escalator = OperatorEscalator(
        lease=lease, store=store, surface=surface, capability=capability,
        params={"member_id": "10001"}, log=log, human=human, timeout_s=60,
    )
    executor = ReplayExecutor(
        surface, run_dir=run_dir, log=log, escalator=escalator
    )
    try:
        yield {
            "surface": surface, "lease": lease, "store": store, "human": human,
            "executor": executor, "run_dir": Path(run_dir), "log": log,
        }
    finally:
        log.close()
        await surface.close()
        admin("/admin/reset")


async def wait_for_intervention(store: InterventionStore, timeout_s: float = 45):
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if request := store.by_session("sess-test"):
            return request
        await asyncio.sleep(0.1)
    raise AssertionError("no intervention was raised")


async def operator_signs_back_in(surface, token: str) -> None:
    """What a person does through noVNC, done through the lease instead.

    In production the operator drives the browser directly and this API is
    not involved at all. The lease check is identical either way.
    """
    async def one(candidate, frame_path):
        matches = await surface.find(candidate, frame_path)
        assert len(matches) == 1, f"operator could not find {candidate.anchor or candidate.name}"
        return matches[0]

    await surface.act_as_operator(
        token, Action(type=ActionType.NAVIGATE, url=f"{APP}/login"), None
    )
    await surface.observe()
    user = await one(
        TargetCandidate(strategy=TargetStrategy.NEAR_TEXT, role="textbox",
                        anchor="USER ID", index=0), [])
    await surface.act_as_operator(
        token,
        Action(type=ActionType.TYPE, text=os.environ.get("APP_USER", "svc_agent")),
        user,
    )
    await surface.observe()
    pwd = await one(
        TargetCandidate(strategy=TargetStrategy.NEAR_TEXT, role="textbox",
                        anchor="PASSWORD", index=0), [])
    await surface.act_as_operator(
        token,
        Action(type=ActionType.TYPE,
               text=os.environ.get("APP_PASSWORD", "demo1234"), sensitive=True),
        pwd,
    )
    await surface.observe()
    button = await one(
        TargetCandidate(strategy=TargetStrategy.A11Y_ROLE_NAME, role="button",
                        name="SIGN ON"), [])
    await surface.act_as_operator(token, Action(type=ActionType.CLICK), button)


# --------------------------------------------------------------- the demo


async def test_a_dead_session_is_handed_to_a_human_and_the_run_resumes(session, capability):
    surface, lease, store = session["surface"], session["lease"], session["store"]

    # The session dies underneath the run: the next servicing request bounces
    # to sign-on with YOUR SESSION HAS TIMED OUT.
    admin("/admin/inject", {"fault": "session_expired", "once": True})

    run = asyncio.get_running_loop().create_task(
        session["executor"].run(capability, {"member_id": "10001"})
    )

    request = await wait_for_intervention(store)
    assert request.reason.value == "UNRECOVERABLE"
    assert request.at_step and request.step_intent
    assert request.screenshot_ref, "an operator needs to see what stopped it"
    assert request.redacted_params == {"member_id": "10001"}

    # Automation is parked. It is not merely polite about it.
    assert lease.lease.holder == AUTOMATION
    token = lease.take("operator investigating")
    with pytest.raises(LeaseViolation, match="operator holds"):
        await surface.act(Action(type=ActionType.NAVIGATE, url=f"{APP}/app"), None)

    # The human fixes the thing that broke, on the same live browser.
    await operator_signs_back_in(surface, token)
    lease.release(token, "signed back in after the session timed out")

    result = await asyncio.wait_for(run, timeout=90)

    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "1234.56"}
    assert len(result.escalations) == 1
    assert result.escalations[0].outcome == "released"
    assert result.escalations[0].operator_note.startswith("signed back in")
    assert result.escalations[0].human_steps > 0


async def test_the_evidence_shows_one_interleaved_timeline(session, capability):
    admin("/admin/inject", {"fault": "session_expired", "once": True})
    run = asyncio.get_running_loop().create_task(
        session["executor"].run(capability, {"member_id": "10001"})
    )
    await wait_for_intervention(session["store"])
    token = session["lease"].take("operator investigating")
    await operator_signs_back_in(session["surface"], token)
    session["lease"].release(token, "signed back in")
    await asyncio.wait_for(run, timeout=90)

    events = [
        json.loads(line)
        for line in (session["run_dir"] / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    names = [e["event"] for e in events]

    # The order is the claim: the machine stops, a person acts, the machine
    # continues — visible in one file without merging anything.
    assert "intervention.open" in names
    assert "human.action" in names
    assert "intervention.closed" in names
    assert names.index("intervention.open") < names.index("human.action")
    assert names.index("human.action") < names.index("intervention.closed")
    assert names.index("intervention.closed") < names.index("run.finish")

    human = [e for e in events if e["event"] == "human.action"]
    assert any(e.get("name") == "SIGN ON" for e in human), "the operator's click"
    password = [e for e in human if e.get("name") == "PASSWORD"]
    assert password and password[0]["value"] == "<password>"

    log_text = (session["run_dir"] / "run.jsonl").read_text(encoding="utf-8")
    assert os.environ.get("APP_PASSWORD", "demo1234") not in log_text


async def test_an_abandoned_hold_fails_the_run_rather_than_resuming_it(
    session, capability, monkeypatch
):
    """An expired hold must never silently return control. The page is in
    whatever state the operator left it; carrying on is the worst option."""
    from cua.escalation import intervention as intervention_module

    monkeypatch.setattr(
        intervention_module.OperatorEscalator, "timeout_s", 1.0, raising=False
    )
    session["executor"].escalator.timeout_s = 1.0
    admin("/admin/inject", {"fault": "session_expired", "once": True})

    result = await asyncio.wait_for(
        session["executor"].run(capability, {"member_id": "10001"}), timeout=90
    )

    assert isinstance(result, Failure), result
    assert result.kind is FailureKind.HUMAN_TIMEOUT
    assert result.escalations[0].outcome == "timed_out"
    assert session["lease"].lease.holder == AUTOMATION, (
        "nobody ever took control, so the lease never moved"
    )


async def test_an_abort_stops_the_run_without_resuming(session, capability):
    admin("/admin/inject", {"fault": "session_expired", "once": True})
    run = asyncio.get_running_loop().create_task(
        session["executor"].run(capability, {"member_id": "10001"})
    )
    await wait_for_intervention(session["store"])

    token = session["lease"].take("operator investigating")
    session["lease"].abort(token, "core is down, not fixable from here")
    result = await asyncio.wait_for(run, timeout=90)

    assert isinstance(result, Failure), result
    assert result.escalations[0].outcome == "aborted"
    assert "core is down" in result.observed


# -------------------------------------------------------- the console API


async def test_the_console_drives_the_same_lease_the_executor_waits_on(session, capability):
    """The HTML buttons and this API post to the same functions. If the
    console could not move the lease, nothing else here would matter."""
    from fastapi.testclient import TestClient

    from cua.escalation.operator_app import create_operator_app

    app = create_operator_app(
        lease=session["lease"], store=session["store"], human=session["human"]
    )
    client = TestClient(app)

    admin("/admin/inject", {"fault": "session_expired", "once": True})
    run = asyncio.get_running_loop().create_task(
        session["executor"].run(capability, {"member_id": "10001"})
    )
    await wait_for_intervention(session["store"])

    listing = client.get("/api/interventions").json()
    assert listing and listing[0]["at_step"]

    taken = client.post("/api/session/sess-test/take").json()
    token = taken["token"]
    assert str(taken["interactive_url"]).startswith("http://localhost:6081/")
    assert session["lease"].held_by_operator

    # The detail page now offers the interactive endpoint, not the view-only
    # one — the UI follows the lease rather than deciding it.
    page = client.get("/session/sess-test").text
    assert "6081" in page and "you are driving this session" in page

    await operator_signs_back_in(session["surface"], token)
    client.post(
        "/api/session/sess-test/release",
        json={"token": token, "note": "signed back in via console"},
    )

    result = await asyncio.wait_for(run, timeout=90)
    assert isinstance(result, Success), result
    assert result.escalations[0].operator_note == "signed back in via console"


def test_the_console_shows_the_view_only_endpoint_until_control_transfers():
    from fastapi.testclient import TestClient

    from datetime import datetime, timezone

    from cua.escalation import InterventionReason, InterventionRequest
    from cua.escalation.operator_app import create_operator_app

    lease = LeaseManager("sess-x", hold_seconds=60)
    store = InterventionStore()
    store.add(
        InterventionRequest(
            id="iv-x", session_id="sess-x", capability_id="c",
            capability_version="1.0.0", goal="g", at_step="s4",
            step_intent="Sign on", reason=InterventionReason.UNRECOVERABLE,
            observed="login page reappeared", expected="an authenticated session",
            created_at=datetime.now(timezone.utc),
        )
    )
    client = TestClient(create_operator_app(lease=lease, store=store))

    page = client.get("/session/sess-x").text
    assert "6080" in page, "monitor endpoint while automation holds the session"
    assert "6081" not in page
    assert "take control to interact" in page
