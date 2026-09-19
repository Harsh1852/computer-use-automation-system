"""Policy enforced against the running application.

Three claims, each checked end to end rather than at the gate in isolation:
the admin route cannot be reached, a draft capability cannot perform its
irreversible step, and re-authentication happens only when policy allows it.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from cua.evidence import RunLog
from cua.policy import Mode, PolicyGate, Policy
from cua.replay import ReplayExecutor
from cua.schema import (
    ApprovalState,
    Capability,
    Failure,
    FailureKind,
    RecoveryAction,
    RecoveryRule,
    Success,
    TextPresent,
)

pytestmark = pytest.mark.integration

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


def load(name: str) -> Capability:
    return Capability.model_validate_json(Path(name).read_text(encoding="utf-8"))


@pytest.fixture
async def run(tmp_path):
    pytest.importorskip("playwright")
    from cua.surface import registry

    admin("/admin/reset")

    async def go(capability, params, *, gate=None, reauth=False, fault=None):
        if fault:
            admin("/admin/inject", {"fault": fault, "once": True})
        run_dir = str(tmp_path / (fault or "clean"))
        surface = registry.create(
            capability.target.surface_kind,
            entry_url=capability.target.entry_point,
            run_dir=run_dir,
            gate=gate
            or PolicyGate(
                mode=Mode.REPLAY,
                artifact_approved=capability.approval.state is ApprovalState.APPROVED,
            ),
        )
        await surface.start()
        log = RunLog(run_dir, "policy-test")
        try:
            executor = ReplayExecutor(
                surface, run_dir=run_dir, log=log, reauth_allowed=reauth
            )
            return await executor.run(capability, params), Path(run_dir)
        finally:
            log.close()
            await surface.close()

    yield go
    admin("/admin/reset")


# ------------------------------------------------------------ the allowlist


async def test_the_agent_cannot_reach_the_admin_route(run):
    """Acceptance: navigating to /admin/inject is POLICY_BLOCKED."""
    result, _ = await run(load("tests/fixtures/reach_admin.v1.json"), {})

    assert isinstance(result, Failure), result
    assert result.kind is FailureKind.POLICY_BLOCKED
    assert result.at_step == "s1"
    assert "denied pattern" in result.observed
    assert "allowlist.denied_patterns" in result.observed


async def test_a_permitted_capability_is_unaffected_by_the_gate(run):
    result, _ = await run(load("artifacts/lookup_member_balance.v1.json"), {"member_id": "10001"})
    assert isinstance(result, Success), result


# --------------------------------------------------------- the asymmetry


async def test_a_draft_capability_stops_before_its_irreversible_step(run):
    """The sub-account capability reaches the review screen and no further,
    because nobody has approved it yet."""
    # Built as a draft here rather than relying on the shipped artifact's
    # approval state, which `make stability` legitimately changes.
    shipped = load("artifacts/open_subaccount.v1.json")
    assert shipped.has_irreversible_step
    capability = shipped.model_copy(
        update={
            "approval": shipped.approval.model_copy(
                update={"state": ApprovalState.DRAFT, "approved_by": None}
            )
        }
    )

    result, _ = await run(
        capability,
        {
            "member_number": "10001",
            "sub_account_type": "SAVINGS",
            "nickname": "POLICYGATE",
            "initial_deposit": "60.00",
            "funding_source_account": "10001-C01",
        },
        gate=PolicyGate(mode=Mode.REPLAY, artifact_approved=False),
    )

    assert isinstance(result, Failure), result
    assert result.kind is FailureKind.POLICY_BLOCKED
    assert "still a draft" in result.observed
    assert result.at_step == capability.irreversible_steps[0].id
    assert result.steps_executed == len(capability.steps) - 2, (
        "everything up to the confirmation should have run"
    )


async def test_the_same_capability_proceeds_once_approved(run, tmp_path):
    """The other half of the asymmetry: approval is what unlocks it."""
    capability = load("artifacts/open_subaccount.v1.json")
    approved = capability.model_copy(
        update={
            "approval": capability.approval.model_copy(
                update={
                    "state": ApprovalState.APPROVED,
                    "replays": 5,
                    "last_verified": "2026-09-19T00:00:00Z",
                }
            )
        }
    )

    result, _ = await run(
        approved,
        {
            "member_number": "10004",
            "sub_account_type": "CHECKING",
            "nickname": "APPROVED",
            "initial_deposit": "80.00",
            "funding_source_account": "10004-S01",
        },
        gate=PolicyGate(mode=Mode.REPLAY, artifact_approved=True),
    )

    assert isinstance(result, Success), result
    assert result.outputs["new_account_number"].startswith("10004-")


# --------------------------------------------------- re-authentication


def _with_reauth_rule(capability: Capability) -> Capability:
    """A declared, reviewable rule naming exactly which steps sign on."""
    return capability.model_copy(
        update={
            "recoveries": [
                *capability.recoveries,
                RecoveryRule(
                    id="reauthenticate",
                    description="The console drops sessions; sign back in.",
                    when=TextPresent(text="YOUR SESSION HAS TIMED OUT"),
                    do=RecoveryAction.REAUTHENTICATE,
                    steps=["s1", "s2", "s3", "s4"],
                    max_attempts=1,
                ),
            ]
        }
    )


async def test_reauth_does_not_happen_when_policy_forbids_it(run):
    """The shipped policy says no, so a dead session escalates instead — and
    with no operator attached, the run stops rather than guessing."""
    capability = _with_reauth_rule(load("artifacts/lookup_member_balance.v1.json"))
    result, _ = await run(capability, {"member_id": "10001"}, reauth=False, fault="session_expired")

    assert isinstance(result, Failure), result
    assert not [r for r in result.recoveries if r.rule_id == "reauthenticate"]


async def test_reauth_happens_when_policy_allows_it(run):
    """Same artifact, same fault, one policy flag different."""
    capability = _with_reauth_rule(load("artifacts/lookup_member_balance.v1.json"))
    result, _ = await run(capability, {"member_id": "10001"}, reauth=True, fault="session_expired")

    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "1234.56"}
    reauth = [r for r in result.recoveries if r.rule_id == "reauthenticate"]
    assert reauth, "the declared rule should have fired"
    assert "re-ran sign-on steps" in reauth[0].note


def test_a_reauth_rule_must_name_its_steps():
    """The executor may not guess which steps re-enter a credential."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must name the steps"):
        RecoveryRule(
            id="reauthenticate",
            when=TextPresent(text="TIMED OUT"),
            do=RecoveryAction.REAUTHENTICATE,
        )


# ------------------------------------------------------------- redaction


async def test_a_live_run_leaks_no_secret_into_its_evidence(run):
    _, run_dir = await run(load("artifacts/lookup_member_balance.v1.json"), {"member_id": "10001"})
    policy = Policy.load("policy.yaml")

    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in run_dir.rglob("*")
        if p.is_file() and p.suffix != ".png"
    )
    for name in policy.redaction.secret_env:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            assert value not in blob, f"{name} leaked into the run evidence"
