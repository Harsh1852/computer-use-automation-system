"""The surface seam holds, and the ladder policy is shared rather than per-surface.

These run with no browser and no driver installed, which is itself part of the
point: everything above the seam is testable without one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.schema import (
    Action,
    ActionType,
    RecordedEvidence,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
)
from cua.surface.base import (
    Gate,
    Handle,
    Lease,
    OpenGate,
    PolicyViolation,
    Surface,
    UnheldLease,
    walk_ladder,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "cua"


# --------------------------------------------------------------- the boundary


def test_no_driver_type_escapes_the_surface_package():
    """`grep -r playwright src/cua | grep -v surface/` must come back empty.

    Asserted here as well as in the Makefile so it fails in CI rather than in
    review. The surface package is the only place allowed to name a driver.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.parent.name == "surface":
            continue
        if "playwright" in path.read_text(encoding="utf-8").lower():
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"driver reference leaked outside surface/: {offenders}"


def test_base_module_imports_no_driver():
    """Importing the protocol must not pull in a browser driver.

    Runs in a subprocess: asserting on `sys.modules` in-process would only
    prove that no *earlier test* had imported the driver, which is a fact
    about test ordering rather than about the seam.
    """
    import subprocess
    import sys

    probe = "import sys, cua.surface.base; print('playwright' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_null_objects_satisfy_the_enforcement_protocols():
    assert isinstance(OpenGate(), Gate)
    assert isinstance(UnheldLease(), Lease)


# ------------------------------------------------------------- ladder policy


def _candidate(strategy: TargetStrategy, **kw) -> TargetCandidate:
    return TargetCandidate(strategy=strategy, **kw)


def _spec(primary: TargetCandidate, *fallbacks: TargetCandidate) -> TargetSpec:
    return TargetSpec(
        primary=primary,
        fallbacks=list(fallbacks),
        recorded=RecordedEvidence(
            winning_strategy=primary.strategy, candidates_seen=1
        ),
    )


class FakeSurface:
    """Only `find` is exercised by the ladder, which is the point of the split."""

    def __init__(self, counts: dict[TargetStrategy, int]) -> None:
        self.counts = counts
        self.calls: list[TargetStrategy] = []

    async def find(self, candidate: TargetCandidate, frame_path: list[str]):
        self.calls.append(candidate.strategy)
        return [
            Handle(
                strategy=candidate.strategy,
                matched=self.counts[candidate.strategy],
                description=f"fake {i}",
            )
            for i in range(self.counts[candidate.strategy])
        ]


async def test_primary_wins_and_no_fallback_is_tried():
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 1, TargetStrategy.CSS: 1})
    spec = _spec(
        _candidate(TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"),
        _candidate(TargetStrategy.CSS, value="#btn"),
    )
    outcome = await walk_ladder(surface, spec, [])

    assert outcome.handle is not None
    assert outcome.winning_index == 0
    assert outcome.drifted is False
    assert surface.calls == [TargetStrategy.A11Y_ROLE_NAME]


async def test_a_fallback_win_is_reported_as_drift():
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 0, TargetStrategy.CSS: 1})
    spec = _spec(
        _candidate(TargetStrategy.A11Y_ROLE_NAME, role="button", name="FIND"),
        _candidate(TargetStrategy.CSS, value="#btn"),
    )
    outcome = await walk_ladder(surface, spec, [])

    assert outcome.winning_index == 1
    assert outcome.drifted is True
    assert outcome.handle.strategy is TargetStrategy.CSS
    assert outcome.describe_attempts() == "a11y_role_name->0; css->1"


async def test_ambiguity_is_a_miss_not_a_coin_flip():
    """Two matches must move down the ladder, never silently take the first."""
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 3, TargetStrategy.CSS: 1})
    spec = _spec(
        _candidate(TargetStrategy.A11Y_ROLE_NAME, role="cell", name="SAVINGS"),
        _candidate(TargetStrategy.CSS, value="#bal"),
    )
    outcome = await walk_ladder(surface, spec, [])

    assert outcome.ambiguous is True
    assert outcome.winning_index == 1
    assert outcome.handle.strategy is TargetStrategy.CSS


async def test_exhausted_ladder_keeps_every_attempt_for_the_error_report():
    surface = FakeSurface({TargetStrategy.A11Y_ROLE_NAME: 2, TargetStrategy.CSS: 0})
    spec = _spec(
        _candidate(TargetStrategy.A11Y_ROLE_NAME, role="cell", name="SAVINGS"),
        _candidate(TargetStrategy.CSS, value="#bal"),
    )
    outcome = await walk_ladder(surface, spec, [])

    assert outcome.handle is None
    assert outcome.winning_index is None
    assert [n for _, n in outcome.attempts] == [2, 0]
    assert outcome.describe_attempts() == "a11y_role_name->2; css->0"


# ------------------------------------------------------------- the chokepoint


class DenyNavigation:
    def check(self, action: Action, context) -> None:
        if action.type is ActionType.NAVIGATE:
            raise PolicyViolation("navigation denied", rule="test")


def test_a_gate_can_refuse_and_says_which_rule_did_it():
    with pytest.raises(PolicyViolation) as caught:
        DenyNavigation().check(Action(type=ActionType.NAVIGATE, url="x"), {})
    assert caught.value.rule == "test"


def test_handles_carry_provenance_but_no_behaviour():
    handle = Handle(
        strategy=TargetStrategy.NEAR_TEXT, matched=1, description="textbox 'x'"
    )
    assert handle.strategy is TargetStrategy.NEAR_TEXT
    # An opaque handle must not hand a caller a way to act on its own.
    assert not [a for a in dir(handle) if a in {"click", "fill", "evaluate", "element"}]


def test_the_web_surface_satisfies_the_protocol_structurally():
    pytest.importorskip("playwright")
    from cua.surface.web_playwright import WebPlaywrightSurface

    assert isinstance(WebPlaywrightSurface(entry_url="http://app:5000"), Surface)


def test_desktop_surface_is_unregistered_and_says_what_it_would_need():
    pytest.importorskip("playwright")
    from cua.schema import SurfaceKind
    from cua.surface import registry

    assert SurfaceKind.LEGACY_WEB in registry.available()
    with pytest.raises(NotImplementedError, match="UIA or AX tree"):
        registry.create(SurfaceKind.DESKTOP, entry_url="x")
