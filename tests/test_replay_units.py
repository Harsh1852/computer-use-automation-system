"""Replay logic that needs no browser: conditions, detectors, bounds, binding.

The parts worth testing without a live surface are the ones where a wrong
answer is silent — precedence between detectors, the bounds on recovery, and
whether a secret can reach a log line.
"""

from __future__ import annotations

import pytest

from cua.replay import (
    ConditionEvaluator,
    DetectorConfig,
    DetectorSet,
    FindingKind,
    RecoveryEngine,
    ScanContext,
    StepResolver,
    coerce,
    extract_value,
    interpolate,
)
from cua.schema import (
    BusinessOutcome,
    Dialog,
    Observation,
    ParamType,
    RecordedEvidence,
    RecoveryAction,
    RecoveryRule,
    Sensitivity,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    TextPresent,
    Transform,
    UiNode,
    UrlMatches,
    ValueRef,
)
from cua.surface.base import Handle

DETAIL_URL = "http://app:5000/members/detail?mbr=10001"


def obs(**kw) -> Observation:
    base = dict(
        url="http://app:5000/app",
        frame_urls={"": "http://app:5000/app", "contentFrame": DETAIL_URL},
        nodes=[
            UiNode(ref=0, role="cell", name="CURRENT BALANCE", frame_path=["contentFrame"]),
            UiNode(ref=1, role="cell", name="1,234.56", frame_path=["contentFrame"]),
            UiNode(ref=2, role="link", name="MEMBER SEARCH", frame_path=["navFrame"]),
        ],
    )
    base.update(kw)
    return Observation(**base)


# ------------------------------------------------------------- conditions


async def test_text_present_is_scoped_to_its_frame():
    ev = ConditionEvaluator()
    inside = await ev.check(
        TextPresent(text="MEMBER SEARCH", frame_path=["navFrame"]), obs(), params={}
    )
    outside = await ev.check(
        TextPresent(text="MEMBER SEARCH", frame_path=["contentFrame"]), obs(), params={}
    )
    assert inside.ok and not outside.ok
    assert "in frame contentFrame" in outside.expected


async def test_url_matches_can_name_the_frame():
    """The frameset's top URL never changes, so a checkpoint that cannot
    name the content frame cannot express reaching the detail screen."""
    ev = ConditionEvaluator()
    top = await ev.check(
        UrlMatches(pattern="*/members/detail*"), obs(), params={}
    )
    framed = await ev.check(
        UrlMatches(pattern="*/members/detail*", frame_path=["contentFrame"]),
        obs(),
        params={},
    )
    assert not top.ok, "top document is still at /app"
    assert framed.ok


async def test_url_pattern_interpolates_declared_params():
    ev = ConditionEvaluator()
    verdict = await ev.check(
        UrlMatches(pattern="*/detail?mbr={member_id}", frame_path=["contentFrame"]),
        obs(),
        params={"member_id": "10001"},
    )
    assert verdict.ok
    assert "10001" in verdict.expected


async def test_a_failed_condition_says_what_it_wanted_and_what_it_saw():
    ev = ConditionEvaluator()
    verdict = await ev.check(TextPresent(text="NO SUCH TEXT"), obs(), params={})
    assert not verdict.ok
    assert "NO SUCH TEXT" in verdict.expected
    assert "not found" in verdict.observed


def test_interpolate_leaves_unknown_placeholders_visible():
    assert interpolate("/x?{a}&{b}", {"a": "1"}) == "/x?1&{b}"


# --------------------------------------------------------------- detectors


def _outcomes():
    return [
        BusinessOutcome(
            code="PERMISSION_DENIED",
            description="not entitled",
            detect=TextPresent(
                text="YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD",
                frame_path=["contentFrame"],
            ),
        )
    ]


async def test_a_declared_outcome_beats_the_http_error_that_carries_it():
    """The permission-denied page *is* an HTTP 403. Reporting it as a failure
    instead of an answer is the mistake this ordering exists to prevent."""
    denied = obs(
        http_status=403,
        frame_statuses={"contentFrame": 403},
        nodes=[
            UiNode(
                ref=0,
                role="cell",
                name="YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD",
                frame_path=["contentFrame"],
            )
        ],
    )
    detectors = DetectorSet(_outcomes(), ConditionEvaluator())

    finding = await detectors.scan(denied, ScanContext(authenticated=True))
    assert finding.kind is FindingKind.BUSINESS_OUTCOME
    assert finding.code == "PERMISSION_DENIED"

    # Both detectors genuinely fire; precedence is what decides.
    kinds = {f.kind for f in await detectors.scan_all(denied, ScanContext(authenticated=True))}
    assert FindingKind.HTTP_ERROR in kinds


async def test_an_undeclared_http_error_is_a_finding_and_names_the_frame():
    detectors = DetectorSet([], ConditionEvaluator())
    finding = await detectors.scan(
        obs(http_status=500, frame_statuses={"contentFrame": 500}),
        ScanContext(authenticated=True),
    )
    assert finding.kind is FindingKind.HTTP_ERROR
    assert "contentFrame" in finding.observed
    assert DETAIL_URL in finding.observed


async def test_the_login_page_is_only_an_auth_wall_after_sign_on():
    detectors = DetectorSet([], ConditionEvaluator())
    at_login = obs(frame_urls={"": "http://app:5000/login"})

    assert await detectors.scan(at_login, ScanContext(authenticated=False)) is None
    finding = await detectors.scan(at_login, ScanContext(authenticated=True))
    assert finding.kind is FindingKind.AUTH_WALL


async def test_session_timeout_text_fires_regardless_of_state():
    detectors = DetectorSet([], ConditionEvaluator())
    timed_out = obs(nodes=[UiNode(ref=0, role="cell", name="YOUR SESSION HAS TIMED OUT")])
    finding = await detectors.scan(timed_out, ScanContext(authenticated=False))
    assert finding.kind is FindingKind.AUTH_WALL


async def test_a_modal_is_detected_structurally():
    detectors = DetectorSet([], ConditionEvaluator())
    finding = await detectors.scan(
        obs(dialogs=[Dialog(title="SYSTEM MAINTENANCE NOTICE", text="...")]),
        ScanContext(),
    )
    assert finding.kind is FindingKind.MODAL


async def test_error_banner_patterns_are_configurable():
    detectors = DetectorSet(
        [], ConditionEvaluator(), DetectorConfig(error_patterns=(r"\bDECLINED\b",))
    )
    finding = await detectors.scan(
        obs(nodes=[UiNode(ref=0, role="cell", name="TRANSFER DECLINED BY CORE")]),
        ScanContext(),
    )
    assert finding.kind is FindingKind.ERROR_BANNER
    assert "DECLINED" in finding.observed


# ---------------------------------------------------------------- recovery


class FakeSurface:
    kind = "legacy_web"

    def __init__(self, counts=None):
        self.counts = counts or {}
        self.actions = []

    async def find(self, candidate, frame_path):
        n = self.counts.get(candidate.strategy, 0)
        return [
            Handle(strategy=candidate.strategy, matched=n, description=f"fake{i}", ref=i)
            for i in range(n)
        ]

    async def act(self, action, handle):
        self.actions.append(action.type)
        from cua.schema import ActionResult

        return ActionResult(ok=True, action=action.type)


def _dismiss_rule(max_attempts: int = 1) -> RecoveryRule:
    return RecoveryRule(
        id="dismiss_notice",
        when=TextPresent(text="SYSTEM MAINTENANCE NOTICE"),
        do=RecoveryAction.DISMISS,
        max_attempts=max_attempts,
        target=TargetSpec(
            primary=TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="DISMISS"
            ),
            recorded=RecordedEvidence(
                winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
            ),
        ),
    )


async def test_recovery_only_fires_for_a_declared_rule():
    engine = RecoveryEngine(
        surface=FakeSurface(), evaluator=ConditionEvaluator(), rules=[_dismiss_rule()]
    )
    notice = obs(nodes=[UiNode(ref=0, role="cell", name="SYSTEM MAINTENANCE NOTICE")])

    assert await engine.match(notice, {}) is not None
    assert await engine.match(obs(), {}) is None, "undeclared conditions must not recover"


async def test_a_rule_is_bounded_per_step(step_factory):
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 1})
    rule = _dismiss_rule(max_attempts=1)
    engine = RecoveryEngine(surface=surface, evaluator=ConditionEvaluator(), rules=[rule])
    step = step_factory()
    notice = obs(nodes=[UiNode(ref=0, role="cell", name="SYSTEM MAINTENANCE NOTICE")])

    first = await engine.apply(rule, step, notice, {})
    second = await engine.apply(rule, step, notice, {})

    assert first.applied and first.record.attempt == 1
    assert not second.applied and second.exhausted


async def test_the_run_has_a_global_recovery_budget(step_factory):
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 1})
    rule = _dismiss_rule(max_attempts=2)
    engine = RecoveryEngine(
        surface=surface, evaluator=ConditionEvaluator(), rules=[rule], budget=1
    )
    notice = obs(nodes=[UiNode(ref=0, role="cell", name="SYSTEM MAINTENANCE NOTICE")])

    assert (await engine.apply(rule, step_factory("s1"), notice, {})).applied
    spent = await engine.apply(rule, step_factory("s2"), notice, {})
    assert not spent.applied and "budget" in spent.detail


# ---------------------------------------------------------------- resolver


async def test_drift_is_recorded_when_the_recorded_winner_stops_working(step_factory):
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 0, TargetStrategy.CSS: 1})
    spec = TargetSpec(
        primary=TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"
        ),
        fallbacks=[TargetCandidate(strategy=TargetStrategy.CSS, value="#btn")],
        recorded=RecordedEvidence(
            winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
        ),
    )
    resolution = await StepResolver(surface).resolve(step_factory(target=spec), obs())

    assert resolution.ok
    assert resolution.drift is not None
    assert resolution.drift.winning_strategy is TargetStrategy.CSS


async def test_no_drift_when_the_ladder_resolves_as_recorded(step_factory):
    """This artifact records a11y_role_name as preferred for a field with no
    accessible name, so a fallback wins on every run by design. Calling that
    drift would bury the real signal."""
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 0, TargetStrategy.NEAR_TEXT: 1})
    spec = TargetSpec(
        primary=TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME, role="textbox", name="MEMBER NUMBER"
        ),
        fallbacks=[
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="textbox",
                anchor="MEMBER NUMBER",
                index=0,
                matches_at_record=1,
            )
        ],
        recorded=RecordedEvidence(
            winning_strategy=TargetStrategy.NEAR_TEXT, candidates_seen=1
        ),
    )
    resolution = await StepResolver(surface).resolve(step_factory(target=spec), obs())

    assert resolution.ok
    assert resolution.drift is None


async def test_a_miss_reports_the_ladder_and_the_nodes_that_were_there(step_factory):
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 0})
    spec = TargetSpec(
        primary=TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME, role="cell", name="CLOSING BALANCE"
        ),
        recorded=RecordedEvidence(
            winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
        ),
    )
    step = step_factory(target=spec, frame_path=["contentFrame"])
    resolution = await StepResolver(surface).resolve(step, obs())

    assert not resolution.ok
    assert "a11y_role_name->0" in resolution.observed
    assert "CURRENT BALANCE" in resolution.observed, "same-role nodes must be listed"


# ------------------------------------------------------------ value binding


@pytest.mark.parametrize(
    "raw,transform,expected",
    [
        ("  1,234.56  ", Transform.MONEY, "1234.56"),
        ("Balance: 402.10", Transform.MONEY, "402.10"),
        ("  padded  ", Transform.STRIP, "padded"),
        ("acct 10001-S01", Transform.DIGITS, "1000101"),
        ("mixed Case", Transform.UPPER, "MIXED CASE"),
        (None, Transform.MONEY, ""),
    ],
)
def test_transforms(raw, transform, expected):
    assert extract_value(raw, None, transform) == expected


def test_extraction_pattern_selects_its_capture_group():
    assert extract_value("ACCT 10001-S01 OPEN", r"(\d{5}-\w\d\d)", Transform.NONE) == "10001-S01"


@pytest.mark.parametrize(
    "value,param_type,expected",
    [("1234.56", ParamType.NUMBER, 1234.56), ("42", ParamType.INTEGER, 42),
     ("yes", ParamType.BOOLEAN, True), ("x", ParamType.STRING, "x")],
)
def test_coercion_follows_the_declared_output_type(value, param_type, expected):
    assert coerce(value, param_type) == expected


def test_a_secret_ref_is_never_the_literal_value():
    ref = ValueRef(secret_ref="APP_PASSWORD")
    assert ref.sensitivity is Sensitivity.SECRET
    assert "demo1234" not in ref.model_dump_json()
