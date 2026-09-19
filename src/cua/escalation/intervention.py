"""Routing a stuck run to a person, and waiting for them.

The request carries enough for somebody who was not watching to act: which
capability and version, the goal, the step and its human-readable intent, why
it stopped, what was expected against what was observed, a screenshot, an
accessibility snapshot, and the parameters — redacted by their declared
sensitivity, so an operator can see *which member* without the console
becoming a place regulated data accumulates.

`OperatorEscalator` is the implementation of the `Escalator` seam the executor
has been calling since the replay phase. Nothing in the executor changed shape
to accommodate it.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..evidence.manifest import redact_params
from ..schema import Capability, EscalationRecord, FailureKind
from .human_capture import HumanCapture
from .lease import Handback, LeaseManager

DEFAULT_INTERVENTION_TIMEOUT_S = 900


class InterventionReason(str, Enum):
    STUCK_DISCOVERY = "STUCK_DISCOVERY"
    UNRECOVERABLE = "UNRECOVERABLE"
    RISKY_APPROVAL = "RISKY_APPROVAL"
    POLICY_BLOCKED = "POLICY_BLOCKED"


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    capability_id: str
    capability_version: str
    goal: str

    at_step: str
    step_intent: str
    reason: InterventionReason
    observed: str
    expected: str

    screenshot_ref: str | None = None
    a11y_snapshot_ref: str | None = None
    redacted_params: dict[str, str] = {}
    created_at: datetime

    resolved_at: datetime | None = None
    resolution: str | None = None
    operator_note: str | None = None
    escalation_count: int = 1

    @property
    def open(self) -> bool:
        return self.resolved_at is None


class InterventionStore:
    """Every request raised in this process, open ones first.

    In-memory on purpose. Persisting interventions would mean building a queue,
    which the brief explicitly says is not rewarded; the seam is that the
    console talks to this object and nothing else.
    """

    def __init__(self) -> None:
        self._requests: dict[str, InterventionRequest] = {}

    def add(self, request: InterventionRequest) -> None:
        self._requests[request.id] = request

    def get(self, request_id: str) -> InterventionRequest | None:
        return self._requests.get(request_id)

    def by_session(self, session_id: str) -> InterventionRequest | None:
        return next(
            (r for r in self._requests.values() if r.session_id == session_id and r.open),
            None,
        )

    def all(self) -> list[InterventionRequest]:
        return sorted(
            self._requests.values(), key=lambda r: (not r.open, r.created_at), reverse=False
        )

    def open_requests(self) -> list[InterventionRequest]:
        return [r for r in self._requests.values() if r.open]

    def resolve(self, request_id: str, resolution: str, note: str | None = None) -> None:
        request = self._requests.get(request_id)
        if request is None:
            return
        request.resolved_at = datetime.now(timezone.utc)
        request.resolution = resolution
        request.operator_note = note


_REASON_BY_FINDING = {
    "auth_wall": InterventionReason.UNRECOVERABLE,
    "modal": InterventionReason.UNRECOVERABLE,
    "http_error": InterventionReason.UNRECOVERABLE,
    "error_banner": InterventionReason.UNRECOVERABLE,
}


class OperatorEscalator:
    """Raises an intervention, parks the run, and resumes it on hand-back."""

    def __init__(
        self,
        *,
        lease: LeaseManager,
        store: InterventionStore,
        surface: Any,
        capability: Capability,
        params: Mapping[str, Any],
        log: Any,
        human: HumanCapture,
        goal: str = "",
        timeout_s: int = DEFAULT_INTERVENTION_TIMEOUT_S,
    ) -> None:
        self.lease = lease
        self.store = store
        self.surface = surface
        self.capability = capability
        self.params = dict(params)
        self.log = log
        self.human = human
        self.goal = goal or capability.description
        self.timeout_s = timeout_s

    async def escalate(self, context: Any):
        # Imported here to keep the escalation package importable without
        # pulling the executor in; the executor owns these two shapes.
        from ..replay.executor import EscalationDecision

        # Order matters. The hand-back signal is armed, automation is stopped
        # and capture is installed *before* the request is published — a fast
        # operator (or an auto-approving policy) can resolve an intervention
        # the instant it appears, and arming afterwards would wipe that
        # resolution and hang the run until the timeout.
        self.lease.reset_for_next_escalation()
        await self.surface.pause()
        await self.surface.watch_human_actions(self.human)
        before = len(self.human.steps)

        request = await self._raise(context)
        self.log.event(
            "intervention.open",
            id=request.id,
            step=request.at_step,
            reason=request.reason.value,
            expected=request.expected,
            observed=request.observed,
        )

        outcome = await self.lease.wait_for_handback(self.timeout_s)
        human_steps = len(self.human.steps) - before

        await self.surface.resume()
        self.store.resolve(request.id, outcome.value, self.lease.operator_note)

        record = EscalationRecord(
            at_step=context.step.id,
            reason=request.reason.value,
            requested_at=request.created_at,
            resolved_at=datetime.now(timezone.utc),
            operator_note=self.lease.operator_note,
            human_steps=human_steps,
            outcome={
                Handback.RESUMED: "released",
                Handback.ABORTED: "aborted",
                Handback.TIMED_OUT: "timed_out",
            }[outcome],
        )
        self.log.event(
            "intervention.closed",
            id=request.id,
            outcome=record.outcome,
            human_steps=human_steps,
            note=record.operator_note,
        )

        return EscalationDecision(
            resumed=outcome is Handback.RESUMED,
            record=record,
            note=self.lease.operator_note or outcome.value,
            failure_kind=(
                FailureKind.HUMAN_TIMEOUT if outcome is Handback.TIMED_OUT else None
            ),
        )

    async def _raise(self, context: Any) -> InterventionRequest:
        snapshot = await self.surface.snapshot(f"{context.step.id}-intervention")
        request = InterventionRequest(
            id=f"iv-{secrets.token_hex(4)}",
            session_id=self.lease.lease.session_id,
            capability_id=self.capability.id,
            capability_version=self.capability.version,
            goal=self.goal,
            at_step=context.step.id,
            step_intent=context.step.intent,
            reason=_REASON_BY_FINDING.get(
                context.finding.kind.value, InterventionReason.UNRECOVERABLE
            ),
            observed=context.finding.observed or context.finding.message,
            expected=context.finding.expected or "the run to continue",
            screenshot_ref=snapshot.screenshot_ref,
            a11y_snapshot_ref=snapshot.a11y_ref,
            redacted_params={
                k: str(v) for k, v in redact_params(self.capability, self.params).items()
            },
            created_at=datetime.now(timezone.utc),
            escalation_count=context.escalation_count,
        )
        self.store.add(request)
        return request
