"""Turning a recorded `TargetSpec` into exactly one live element.

Thin by design: the ladder walk itself lives in `surface.base.walk_ladder`, so
every surface obeys the identical rule. What this module adds is the replay
layer's interpretation of the walk — drift reporting and a failure message a
human can act on.

The two rules that matter are in the walk: **ambiguity is a miss**, and
**every attempt is recorded**. What is added here is that a fallback winning
is neither hidden nor fatal. Replay succeeds and emits a `DriftSignal`;
aggregated across tenants on the same vendor product, a rung that starts
losing is the earliest warning that a product version has moved.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schema import DriftSignal, Observation, Step, UiNode
from ..surface.base import Handle, Surface, walk_ladder


@dataclass
class Resolution:
    handle: Handle | None
    drift: DriftSignal | None = None
    expected: str = ""
    observed: str = ""

    @property
    def ok(self) -> bool:
        return self.handle is not None


class StepResolver:
    def __init__(self, surface: Surface) -> None:
        self._surface = surface

    async def resolve(self, step: Step, observation: Observation) -> Resolution:
        if step.target is None:
            return Resolution(handle=None, expected="a target", observed="step has none")

        outcome = await walk_ladder(self._surface, step.target, step.frame_path)
        primary = step.target.primary

        if outcome.handle is None:
            return Resolution(
                handle=None,
                expected=self._describe_wanted(step),
                observed=self._describe_miss(step, outcome, observation),
            )

        drift = None
        winner = step.target.ladder[outcome.winning_index]
        recorded_winner = step.target.recorded.winning_strategy

        # Drift is "the ladder resolved differently than when it was
        # recorded", not "a fallback won". Those differ: this artifact
        # records a11y_role_name as the preferred rung for a field that has
        # no accessible name, so a fallback wins on *every* run by design.
        # Flagging that as drift would bury the real signal in noise.
        if winner.strategy is not recorded_winner:
            drift = DriftSignal(
                at_step=step.id,
                expected_strategy=recorded_winner,
                winning_strategy=winner.strategy,
                matches=1,
                note=(
                    f"recorded winner {recorded_winner.value} no longer resolves; "
                    f"ladder: {outcome.describe_attempts()}"
                ),
            )
        elif self._count_changed(primary, outcome):
            # The primary still won, but it is matching a different number of
            # nodes than it did at record time. Not a failure yet — the early
            # warning before it becomes one.
            drift = DriftSignal(
                at_step=step.id,
                expected_strategy=primary.strategy,
                winning_strategy=primary.strategy,
                matches=1,
                note=(
                    f"match count moved: recorded {primary.matches_at_record}, "
                    f"now {self._count(outcome, 0)}"
                ),
            )

        return Resolution(handle=outcome.handle, drift=drift)

    # ------------------------------------------------------------- reporting

    @staticmethod
    def _count(outcome, index: int) -> int | None:
        return outcome.attempts[index][1] if index < len(outcome.attempts) else None

    def _count_changed(self, primary, outcome) -> bool:
        recorded = primary.matches_at_record
        actual = self._count(outcome, 0)
        return recorded is not None and actual is not None and recorded != actual

    @staticmethod
    def _describe_wanted(step: Step) -> str:
        primary = step.target.primary
        bits = [primary.strategy.value]
        if primary.role:
            bits.append(f"role={primary.role}")
        if primary.name:
            bits.append(f"name={primary.name!r}")
        if primary.anchor:
            bits.append(f"anchor={primary.anchor!r}")
        if primary.index is not None:
            bits.append(f"index={primary.index}")
        if primary.value:
            bits.append(f"selector={primary.value!r}")
        frame = "/".join(step.frame_path) or "top document"
        return f"{' '.join(bits)} in {frame}"

    @staticmethod
    def _describe_miss(step: Step, outcome, observation: Observation) -> str:
        """Every rung and its count, plus the nodes of the same role that *did*
        exist. Without the second half, a TARGET_NOT_FOUND tells you nothing
        about whether the element moved, was renamed, or never rendered."""
        wanted_role = step.target.primary.role
        siblings: list[UiNode] = [
            n
            for n in observation.nodes
            if n.frame_path == step.frame_path and (not wanted_role or n.role == wanted_role)
        ]
        shown = ", ".join(
            f"{n.role}{'' if n.name is None else ' ' + repr(n.name[:40])}"
            for n in siblings[:8]
        )
        more = f" (+{len(siblings) - 8} more)" if len(siblings) > 8 else ""
        role_label = wanted_role or "any role"
        return (
            f"ladder exhausted [{outcome.describe_attempts()}]; "
            f"{len(siblings)} node(s) with {role_label} present: {shown or 'none'}{more}"
        )
