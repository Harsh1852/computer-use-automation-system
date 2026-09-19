"""Policy: one enforcement point, one redaction sink, and no leaked secrets.

The gate tests are written against the real `policy.yaml` rather than a
fixture, because the thing worth asserting is that *the shipped policy*
refuses what it claims to refuse.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from cua.policy import Mode, PolicyGate, Policy, Redactor, load_policy
from cua.policy.redaction import SECRET_MASK, default_redactor
from cua.schema import Action, ActionType, Risk
from cua.surface.base import PolicyViolation

ADMIN = "http://app:5000/admin/inject"
MEMBERS = "http://app:5000/members/detail?mbr=10001"
CONFIRM = "http://app:5000/members/subaccount/confirm"


def gate(mode: Mode = Mode.REPLAY, approved: bool = False) -> PolicyGate:
    return PolicyGate(load_policy(), mode=mode, artifact_approved=approved)


def navigate(url: str) -> Action:
    return Action(type=ActionType.NAVIGATE, url=url)


def click(risk: Risk = Risk.SAFE_REVERSIBLE) -> Action:
    return Action(type=ActionType.CLICK, risk=risk, intent="click something")


# --------------------------------------------------------------- allowlist


def test_the_admin_route_is_refused():
    """The stated acceptance: the agent cannot reach fault injection."""
    with pytest.raises(PolicyViolation) as caught:
        gate().check(navigate(ADMIN), {"url": "http://app:5000/app"})
    assert caught.value.rule == "allowlist.denied_patterns"


def test_a_denial_cannot_be_undone_by_a_broader_allow():
    """`/members/**` allows a lot; `**/admin/**` still wins."""
    policy = Policy.model_validate(
        {
            "allowlist": {
                "url_patterns": ["http://app:5000/**"],
                "denied_patterns": ["**/admin/**"],
                "action_types": ["navigate"],
            }
        }
    )
    with pytest.raises(PolicyViolation, match="denied pattern"):
        PolicyGate(policy).check(navigate(ADMIN), {})


def test_a_url_outside_the_allowlist_is_refused():
    with pytest.raises(PolicyViolation) as caught:
        gate().check(navigate("http://evil.example/steal"), {})
    assert caught.value.rule == "allowlist.url_patterns"


def test_permitted_routes_pass():
    gate().check(navigate(MEMBERS), {})
    gate().check(navigate("http://app:5000/login"), {})


def test_a_glob_does_not_leak_across_hosts():
    """`fnmatch` alone would let `*` swallow a host separator."""
    with pytest.raises(PolicyViolation):
        gate().check(navigate("http://app:5000.evil.test/members/x"), {})


def test_acting_on_a_page_outside_the_allowlist_is_refused():
    """If the app redirects somewhere unsanctioned, the next click stops the
    run rather than typing into whatever arrived."""
    with pytest.raises(PolicyViolation, match="act on"):
        gate().check(click(), {"url": "http://elsewhere.test/page"})


def test_a_disallowed_action_type_is_refused():
    policy = Policy.model_validate(
        {"allowlist": {"url_patterns": ["**"], "action_types": ["read", "wait"]}}
    )
    with pytest.raises(PolicyViolation) as caught:
        PolicyGate(policy).check(click(), {"url": MEMBERS})
    assert caught.value.rule == "allowlist.action_types"


# ------------------------------------------------------------ risk grading


def test_risk_is_derived_from_the_label_not_only_the_artifact():
    """An artifact is data. A recording that under-declared a CONFIRM button
    as safe should still be caught by the gate."""
    under_declared = click(Risk.SAFE_REVERSIBLE)
    derived = gate().derived_risk(under_declared, {"url": MEMBERS, "label": "CONFIRM"})
    assert derived is Risk.IRREVERSIBLE

    with pytest.raises(PolicyViolation, match="irreversible"):
        gate(Mode.DISCOVERY).check(under_declared, {"url": MEMBERS, "label": "CONFIRM"})


def test_risk_is_derived_from_the_route():
    assert gate().derived_risk(click(), {"url": CONFIRM}) is Risk.IRREVERSIBLE


def test_the_gate_takes_the_higher_of_declared_and_derived():
    declared_high = click(Risk.IRREVERSIBLE)
    assert gate().effective_risk(declared_high, {"url": MEMBERS}) is Risk.IRREVERSIBLE

    derived_high = click(Risk.SAFE_REVERSIBLE)
    assert gate().effective_risk(derived_high, {"url": CONFIRM}) is Risk.IRREVERSIBLE


# ----------------------------------------------------------- the asymmetry


def test_discovery_may_never_perform_an_irreversible_action():
    with pytest.raises(PolicyViolation) as caught:
        gate(Mode.DISCOVERY).check(click(Risk.IRREVERSIBLE), {"url": MEMBERS})
    assert caught.value.rule == "enforcement.discovery.irreversible"


def test_replay_of_a_draft_artifact_may_not_either():
    with pytest.raises(PolicyViolation, match="still a draft") as caught:
        gate(Mode.REPLAY, approved=False).check(click(Risk.IRREVERSIBLE), {"url": MEMBERS})
    assert caught.value.rule == "enforcement.replay.irreversible"


def test_replay_of_an_approved_artifact_may_proceed():
    """The asymmetry's other half: a human has already reviewed exactly which
    step is irreversible, so an approved capability is allowed to do it."""
    gate(Mode.REPLAY, approved=True).check(click(Risk.IRREVERSIBLE), {"url": MEMBERS})


def test_state_changing_actions_are_allowed_in_both_modes():
    for mode in (Mode.DISCOVERY, Mode.REPLAY):
        gate(mode).check(click(Risk.STATE_CHANGING), {"url": MEMBERS})


def test_reauthentication_is_off_by_default_in_the_shipped_policy():
    assert gate().reauth_allowed is False


# ------------------------------------------------------------- redaction


def test_a_secret_is_scrubbed_wherever_it_appears():
    redactor = Redactor({}, ["hunter2xyz"])
    assert redactor.scrub_text("password=hunter2xyz;") == f"password={SECRET_MASK};"
    assert redactor.scrub({"a": {"b": ["hunter2xyz"]}}) == {"a": {"b": [SECRET_MASK]}}


def test_longer_secrets_are_scrubbed_before_shorter_ones():
    """Scrubbing a short secret first would leave the tail of a longer one."""
    redactor = Redactor({}, ["abcd", "abcdefgh"])
    assert "efgh" not in redactor.scrub_text("token abcdefgh")


def test_regulated_shapes_are_caught_even_when_nobody_registered_them():
    redactor = default_redactor({"APP_PASSWORD": "demo1234"})
    assert "123-45-6789" not in redactor.scrub_text("ssn 123-45-6789")
    assert "<redacted:ssn>" in redactor.scrub_text("ssn 123-45-6789")


def test_the_redactor_is_a_structlog_processor():
    redactor = Redactor({"ssn": r"\d{3}-\d{2}-\d{4}"}, ["s3cret-value"])
    out = redactor(None, "info", {"event": "x", "a": "s3cret-value", "b": "123-45-6789"})
    assert out["a"] == SECRET_MASK
    assert out["b"] == "<redacted:ssn>"
    assert out["event"] == "x"


def test_redaction_is_on_by_default_at_the_sink(tmp_path, monkeypatch):
    """The point of putting it at the sink: a caller who forgets is covered."""
    from cua.evidence import RunLog

    monkeypatch.setenv("APP_PASSWORD", "totally-secret-pw")
    log = RunLog(tmp_path / "run", "r1")
    log.event("careless", note="the password is totally-secret-pw")
    log.close()

    written = (tmp_path / "run" / "run.jsonl").read_text(encoding="utf-8")
    assert "totally-secret-pw" not in written
    assert SECRET_MASK in written


def test_redaction_can_be_turned_off_only_explicitly(tmp_path, monkeypatch):
    from cua.evidence import RunLog

    monkeypatch.setenv("APP_PASSWORD", "totally-secret-pw")
    log = RunLog(tmp_path / "run", "r1", redact=False)
    assert log.redactor is None
    log.close()


# -------------------------------------------------- the secret-leak sweep


def _secret_values() -> dict[str, str]:
    policy = load_policy()
    return {
        name: value
        for name in policy.redaction.secret_env
        if (value := os.environ.get(name)) and len(value) >= 6
    }


def test_no_configured_secret_appears_in_artifacts_or_evidence():
    """Sweeps every committed byte for every secret this deployment holds.

    Not just the seeded password: whatever `policy.yaml` names as secret,
    including the model API key, must be absent from the two directories
    that get committed.
    """
    secrets = _secret_values()
    secrets.setdefault("APP_PASSWORD_DEFAULT", "demo1234")

    offenders: list[str] = []
    for root in (Path("artifacts"), Path("evidence")):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix == ".png":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name, value in secrets.items():
                if value in text:
                    offenders.append(f"{path} contains {name}")

    assert offenders == [], "\n".join(offenders)


def test_artifacts_reference_credentials_only_by_name():
    """A capability may name an environment variable; it may never hold one."""
    import json

    for path in Path("artifacts").glob("*.json"):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        refs = [
            step["value"]["secret_ref"]
            for step in artifact["steps"]
            if step.get("value") and step["value"].get("secret_ref")
        ]
        for ref in refs:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", ref), f"{path}: {ref!r}"
        literals = [
            step["value"]["literal"]
            for step in artifact["steps"]
            if step.get("value") and step["value"].get("literal")
        ]
        for literal in literals:
            assert "demo1234" not in literal, f"{path} inlines a credential"
