"""Earning `approved` by replaying, rather than by asserting.

This is what makes the policy asymmetry enforceable. Replay may perform an
irreversible step only if the capability is approved; approval is only granted
after the capability has demonstrated it replays. Without this, "approved" is
a field somebody sets by hand and the gate is checking a promise.

A stability score is the fraction of runs that produced the *modal* answer.
Deliberately not "fraction that succeeded": a capability invoked with a member
who does not exist should return `MEMBER_NOT_FOUND` every single time, and
that is perfectly stable. A run that answers differently on identical inputs
is the thing worth counting, because determinism is the claim being tested.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..schema import ApprovalState, Capability, ReplayResult

DEFAULT_THRESHOLD = 0.9
MIN_RUNS = 3


@dataclass
class StabilityReport:
    capability_id: str
    capability_version: str
    runs: int = 0
    outcomes: Counter = field(default_factory=Counter)
    failures: int = 0
    durations_ms: list[int] = field(default_factory=list)
    drift_signals: int = 0
    recoveries: int = 0
    outputs_seen: set[str] = field(default_factory=set)
    declared_outputs: list[str] = field(default_factory=list)
    outputs_complete: int = 0
    creates_something: bool = False
    """True when the capability has an irreversible step.

    A capability whose purpose is to *create* something returns a different
    value every run - a new account number is supposed to be new. Requiring
    identical outputs from it would make a correct capability unapprovable,
    so for those the criterion is that every declared output came back
    populated, not that it came back the same."""

    def record(self, result: ReplayResult) -> None:
        self.runs += 1
        label = result.status
        if result.status == "business_outcome":
            label = f"business_outcome:{result.code}"
        if result.status == "failure":
            label = f"failure:{result.kind.value}"
            self.failures += 1
        self.outcomes[label] += 1
        self.durations_ms.append(result.duration_ms)
        self.drift_signals += len(getattr(result, "drift", []) or [])
        self.recoveries += len(getattr(result, "recoveries", []) or [])
        if result.status == "success":
            self.outputs_seen.add(repr(sorted(result.outputs.items())))
            if all(str(result.outputs.get(n, "")).strip() for n in self.declared_outputs):
                self.outputs_complete += 1

    # ------------------------------------------------------------- scoring

    @property
    def modal_outcome(self) -> str | None:
        return self.outcomes.most_common(1)[0][0] if self.outcomes else None

    @property
    def stability(self) -> float:
        """Agreement with the modal answer. 1.0 means every run agreed."""
        if not self.runs:
            return 0.0
        return self.outcomes.most_common(1)[0][1] / self.runs

    @property
    def flakiness(self) -> float:
        return round(1.0 - self.stability, 4)

    @property
    def deterministic_outputs(self) -> bool:
        """Every successful run returned the same values."""
        return len(self.outputs_seen) <= 1

    @property
    def outputs_acceptable(self) -> tuple[bool, str]:
        """The right output criterion for this kind of capability."""
        successes = self.outcomes.get("success", 0)
        if not self.declared_outputs or not successes:
            return True, "no declared outputs to check"
        if self.creates_something:
            if self.outputs_complete == successes:
                return True, (
                    f"every run returned all of {self.declared_outputs} "
                    "(values differ, as they must for a capability that creates)"
                )
            return False, (
                f"only {self.outputs_complete}/{successes} runs returned every "
                f"declared output"
            )
        if self.deterministic_outputs:
            return True, "identical outputs on every run"
        return False, "successful runs disagreed about their outputs"

    @property
    def median_ms(self) -> int:
        if not self.durations_ms:
            return 0
        ordered = sorted(self.durations_ms)
        return ordered[len(ordered) // 2]

    def summary(self) -> dict[str, Any]:
        return {
            "capability": f"{self.capability_id}@{self.capability_version}",
            "runs": self.runs,
            "modal_outcome": self.modal_outcome,
            "stability": round(self.stability, 4),
            "flakiness": self.flakiness,
            "failures": self.failures,
            "deterministic_outputs": self.deterministic_outputs,
            "outputs_acceptable": self.outputs_acceptable[1],
            "creates_something": self.creates_something,
            "median_ms": self.median_ms,
            "drift_signals": self.drift_signals,
            "recoveries": self.recoveries,
            "outcomes": dict(self.outcomes),
        }

    # ----------------------------------------------------------- promotion

    def qualifies(
        self, *, threshold: float = DEFAULT_THRESHOLD, min_runs: int = MIN_RUNS
    ) -> tuple[bool, str]:
        if self.runs < min_runs:
            return False, f"only {self.runs} run(s); at least {min_runs} required"
        if self.modal_outcome != "success":
            return False, f"modal outcome is {self.modal_outcome!r}, not success"
        if self.stability < threshold:
            return False, f"stability {self.stability:.2f} is below {threshold:.2f}"
        outputs_ok, outputs_reason = self.outputs_acceptable
        if not outputs_ok:
            return False, outputs_reason
        return True, f"{self.outcomes['success']}/{self.runs} runs succeeded; {outputs_reason}"


def promote(
    capability: Capability,
    report: StabilityReport,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_runs: int = MIN_RUNS,
    approved_by: str = "make stability",
) -> tuple[Capability, bool, str]:
    """Return the capability with its approval updated, and why.

    Always records the replay history, whether or not it promotes: a
    capability that failed its stability check should carry that fact, not
    look untested.
    """
    ok, reason = report.qualifies(threshold=threshold, min_runs=min_runs)
    approval = capability.approval.model_copy(
        update={
            "state": ApprovalState.APPROVED if ok else ApprovalState.DRAFT,
            "replays": capability.approval.replays + report.runs,
            "failures": capability.approval.failures + report.failures,
            "last_verified": datetime.now(timezone.utc),
            "approved_by": approved_by if ok else None,
            "notes": reason,
        }
    )
    return capability.model_copy(update={"approval": approval}), ok, reason
