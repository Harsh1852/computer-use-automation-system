"""Discovery, with and without a real model.

The expensive half of this phase is one genuine LLM run, and it lives in
`test_discovery_llm.py`. What is tested here is everything that must hold
regardless of which model drove the run: what the model is allowed to see,
what the recorder writes down, and — the one that matters — that the emitted
artifact actually replays.

The scripted-model test drives the *real* browser against the *real*
application with a fixed sequence of tool calls. It exercises every line of
the recorder without spending a token, which is what makes it safe to assert
on the shape of the artifact in detail.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest
from pydantic import BaseModel

from cua.discovery import Recorder, render_for_model, substitute_secrets
from cua.discovery.agent import DiscoveryAgent
from cua.discovery.catalogue import outcomes_for, recoveries_for
from cua.evidence import RunLog
from cua.schema import (
    Capability,
    NameSource,
    Observation,
    Risk,
    Sensitivity,
    SurfaceKind,
    TargetStrategy,
    UiNode,
)

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")


# --------------------------------------------------------- what the model sees


def _screen() -> Observation:
    return Observation(
        url="http://app:5000/app",
        title="MEMBER SEARCH",
        frame_urls={"": "http://app:5000/app"},
        nodes=[
            UiNode(
                ref=8, role="row", name="MEMBER NUMBER", frame_path=["contentFrame"], order=8
            ),
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


def test_the_model_is_given_a_near_hint_for_unnamed_fields():
    rendered = render_for_model(_screen())
    assert '[10] textbox      (no name) near="MEMBER NUMBER"' in rendered


def test_the_model_never_sees_markup_or_ids():
    rendered = render_for_model(_screen())
    for forbidden in ("<td", "<input", "ctl00", "css", "xpath", "#"):
        assert forbidden not in rendered.lower()


def test_credentials_reach_the_browser_but_not_the_model():
    text, env_var = substitute_secrets("{{APP_PASSWORD}}", {"APP_PASSWORD": "s3cret"})
    assert (text, env_var) == ("s3cret", "APP_PASSWORD")
    assert substitute_secrets("10001", {}) == ("10001", None)


def test_an_unset_credential_is_an_error_not_a_guess():
    with pytest.raises(KeyError, match="APP_PASSWORD"):
        substitute_secrets("{{APP_PASSWORD}}", {})


def test_no_progress_is_detected_by_observation_digest():
    same = ["a", "a", "a"]
    assert DiscoveryAgent._stalled(same)
    assert not DiscoveryAgent._stalled(["a", "a", "b"])
    assert not DiscoveryAgent._stalled(["a", "a"])


def test_labels_are_preferred_over_data_as_anchors():
    """The cell nearest a balance holds the account nickname, which differs
    per member. Anchoring there yields an artifact that works for one member."""
    assert Recorder._looks_like_a_label("CURRENT BALANCE")
    assert Recorder._looks_like_a_label("SAVINGS")
    assert not Recorder._looks_like_a_label("10001-S01")
    assert not Recorder._looks_like_a_label("Primary Share")


# ------------------------------------------------------------ scripted model


class _Fn(BaseModel):
    name: str
    arguments: str


class _Call(BaseModel):
    id: str
    type: str = "function"
    function: _Fn


class _Msg(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[_Call] | None = None


class _Response:
    def __init__(self, message: _Msg) -> None:
        self.choices = [type("C", (), {"message": message})()]
        self.usage = None


class ScriptedModel:
    """Stands in for the model: a fixed plan, chosen against the live screen.

    Each step is a callable given the current observation, so the script
    refers to elements the way the model does — by what they look like —
    rather than by refs that would change if the page did.
    """

    def __init__(self, plan: list[Callable[[Observation], tuple[str, dict]]], toolbox) -> None:
        self.plan = plan
        self.toolbox = toolbox
        self.index = 0
        self.chat = type("Chat", (), {"completions": self})()

    async def create(self, **_kw: Any) -> _Response:
        if self.index >= len(self.plan):
            return _Response(_Msg(content="no plan left"))
        name, args = self.plan[self.index](self.toolbox.last_observation)
        self.index += 1
        return _Response(
            _Msg(
                tool_calls=[
                    _Call(id=f"c{self.index}", function=_Fn(name=name, arguments=json.dumps(args)))
                ]
            )
        )


def ref_of(obs: Observation, role: str, *, name=None, near=None, frame=None) -> int:
    for node in obs.nodes:
        if node.role != role:
            continue
        if name is not None and node.name != name:
            continue
        if frame is not None and node.frame_path != frame:
            continue
        if near is not None:
            peers = [
                n
                for n in obs.nodes
                if n.container_ref == node.container_ref and n.name == near
            ]
            if not peers:
                continue
        return node.ref
    raise AssertionError(f"no {role} name={name} near={near} frame={frame}")


def lookup_balance_plan() -> list[Callable[[Observation], tuple[str, dict]]]:
    content = ["contentFrame"]
    return [
        lambda o: ("navigate", {"url": f"{APP}/login"}),
        lambda o: ("type", {"ref": ref_of(o, "textbox", near="USER ID"), "text": "{{APP_USER}}"}),
        lambda o: ("type", {"ref": ref_of(o, "textbox", near="PASSWORD"), "text": "{{APP_PASSWORD}}"}),
        lambda o: ("click", {"ref": ref_of(o, "button", name="SIGN ON")}),
        lambda o: ("type", {"ref": ref_of(o, "textbox", frame=content), "text": "10001"}),
        lambda o: ("click", {"ref": ref_of(o, "button", name="SEARCH", frame=content)}),
        lambda o: ("read", {"ref": ref_of(o, "cell", name="1,234.56", frame=content),
                            "output_name": "savings_balance"}),
        lambda o: (
            "done",
            {
                "summary": "Look up a member and read their savings balance.",
                "inputs": [
                    {"name": "member_id", "description": "Five-digit member number", "example": "10001"}
                ],
                "outputs": [
                    {"name": "savings_balance", "description": "Current balance of the savings sub-account"}
                ],
            },
        ),
    ]


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


@pytest.fixture
async def recorded(tmp_path):
    """Drive the real surface with a scripted model and emit a capability."""
    pytest.importorskip("playwright")
    from cua.surface import registry

    admin("/admin/reset")
    run_dir = tmp_path / "discovery"
    surface = registry.create(
        SurfaceKind.LEGACY_WEB, entry_url=f"{APP}/login", run_dir=str(run_dir)
    )
    await surface.start()
    log = RunLog(run_dir, "scripted")
    try:
        recorder = Recorder(
            surface,
            goal="Look up member 10001 and read their current savings balance.",
            entry_url=f"{APP}/login",
            tenant_id="meridian",
            vendor_product="MERIDIAN CoreBank",
            product_version="7.4.11",
            surface_kind=SurfaceKind.LEGACY_WEB,
        )
        agent = DiscoveryAgent(
            surface, recorder, goal=recorder.goal, entry_url=recorder.entry_url,
            run_dir=run_dir, log=log, client=object(), model="scripted-model",
            secrets={"APP_USER": os.environ.get("APP_USER", "svc_agent"),
                     "APP_PASSWORD": os.environ.get("APP_PASSWORD", "demo1234")},
        )
        agent.client = ScriptedModel(lookup_balance_plan(), agent.tools)
        result = await agent.run()
        assert result.ok, f"scripted run did not finish: {result.reason}"

        capability = recorder.build_capability(
            capability_id="scripted_lookup_balance",
            proposal=result.proposal,
            run_id="scripted",
            model="scripted-model",
            transcript_sha256=result.transcript_sha256,
            known_outcomes=outcomes_for("MERIDIAN CoreBank", recorder._content_frame()),
            known_recoveries=recoveries_for("MERIDIAN CoreBank"),
        )
        yield capability, recorder, run_dir
    finally:
        log.close()
        await surface.close()
        admin("/admin/reset")


pytestmark = pytest.mark.integration


# ---------------------------------------------------------- what was recorded


async def test_the_recorder_emits_a_valid_draft_capability(recorded):
    capability, _, _ = recorded
    assert Capability.model_validate_json(capability.model_dump_json()) == capability
    assert capability.approval.state.value == "draft"
    assert capability.provenance.model == "scripted-model"
    assert len(capability.provenance.transcript_sha256) == 64


async def test_credentials_become_secret_refs_never_literals(recorded):
    capability, _, run_dir = recorded
    secrets = [
        s.value.secret_ref for s in capability.steps if s.value and s.value.secret_ref
    ]
    assert secrets == ["APP_USER", "APP_PASSWORD"]

    serialised = capability.model_dump_json()
    password = os.environ.get("APP_PASSWORD", "demo1234")
    assert password not in serialised
    assert password not in (run_dir / "transcript.jsonl").read_text(encoding="utf-8")


async def test_the_typed_value_became_a_parameter_everywhere(recorded):
    capability, _, _ = recorded
    assert [i.name for i in capability.contract.inputs] == ["member_id"]
    assert capability.contract.inputs[0].pattern == "^[0-9]{5}$"

    typed = [s for s in capability.steps if s.value and s.value.param == "member_id"]
    assert typed, "the typed member number was not parameterised"

    # The same value appears in the URL the search landed on; a checkpoint
    # left concrete would make the artifact work for exactly one member.
    urls = [
        s.postcondition.pattern
        for s in capability.steps
        if s.postcondition.kind == "url_matches"
    ]
    assert any("{member_id}" in u for u in urls), urls
    assert not any("10001" in u for u in urls), urls


async def test_the_full_ladder_is_recorded_with_its_match_counts(recorded):
    capability, _, _ = recorded
    search = next(s for s in capability.steps if s.intent.startswith("Click button 'SEARCH'"))
    counts = {c.strategy.value: c.matches_at_record for c in search.target.ladder}

    assert counts["a11y_role_name"] == 1
    assert "css" in counts, "the surface-native last-resort rung should be recorded"
    assert search.target.recorded.winning_strategy is TargetStrategy.A11Y_ROLE_NAME


async def test_an_unnamed_field_is_recorded_with_a_near_text_rung(recorded):
    capability, _, _ = recorded
    member_field = next(
        s for s in capability.steps if s.value and s.value.param == "member_id"
    )
    ladder = {c.strategy.value: c for c in member_field.target.ladder}

    assert "near_text" in ladder, "a field with no accessible name needs an anchor"
    assert ladder["near_text"].anchor == "MEMBER NUMBER"
    assert ladder["near_text"].matches_at_record == 1
    assert member_field.target.recorded.winning_strategy is TargetStrategy.NEAR_TEXT


async def test_the_read_is_anchored_on_a_label_not_on_the_value(recorded):
    capability, _, _ = recorded
    read = next(s for s in capability.steps if s.output is not None)
    near = next(c for c in read.target.ladder if c.strategy is TargetStrategy.NEAR_TEXT)

    assert near.anchor == "SAVINGS", "anchored on the nickname, which differs per member"
    assert "1,234.56" not in capability.model_dump_json(), "the value was baked in"
    assert read.postcondition.kind == "text_present"


async def test_outputs_are_declared_and_tagged_conservatively(recorded):
    capability, _, _ = recorded
    balance = next(f for f in capability.contract.outputs if f.name == "savings_balance")
    assert balance.sensitivity is Sensitivity.PII
    assert balance.from_step == next(s.id for s in capability.steps if s.output)


async def test_risk_is_classified_per_step(recorded):
    capability, _, _ = recorded
    kinds = {s.action.value: s.risk for s in capability.steps}
    assert kinds["read"] is Risk.SAFE_REVERSIBLE
    assert kinds["type"] is Risk.SAFE_REVERSIBLE
    assert kinds["click"] is Risk.STATE_CHANGING
    assert not capability.has_irreversible_step


async def test_product_knowledge_is_attached_not_invented(recorded):
    """A happy-path run cannot observe 'no such member'; the outcome taxonomy
    is per-product configuration, and says so."""
    capability, _, _ = recorded
    codes = {o.code for o in capability.outcomes}
    assert {"MEMBER_NOT_FOUND", "PERMISSION_DENIED"} <= codes
    assert [r.id for r in capability.recoveries] == ["dismiss_maintenance_notice"]


async def test_evidence_contains_the_transcript_and_the_steps(recorded):
    _, recorder, run_dir = recorded
    assert (run_dir / "transcript.jsonl").exists()
    assert list(run_dir.glob("screenshots/*.png"))
    assert len(recorder.steps) == 7


# ------------------------------------------------------- and it must replay


async def test_the_recorded_artifact_replays_deterministically(recorded, tmp_path):
    """The whole point. A recording that cannot be replayed is a log file."""
    from cua.replay import ReplayExecutor
    from cua.schema import Success
    from cua.surface import registry

    capability, _, _ = recorded
    admin("/admin/reset")
    run_dir = str(tmp_path / "replay-of-recording")
    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        run_dir=run_dir,
    )
    await surface.start()
    log = RunLog(run_dir, "replay")
    try:
        result = await ReplayExecutor(surface, run_dir=run_dir, log=log).run(
            capability, {"member_id": "10002"}
        )
    finally:
        log.close()
        await surface.close()

    # A different member than the one recorded: proof the parameterisation is
    # real rather than the recording happening to work on its own data.
    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "88.00"}
