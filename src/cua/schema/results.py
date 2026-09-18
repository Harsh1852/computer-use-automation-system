"""The replay result contract.

Three arms, discriminated on ``status``, not a boolean plus exceptions:

- ``success`` — the flow completed and the declared outputs are attached.
- ``business_outcome`` — the application gave a legitimate answer that is not
  success. "No such member" is information the caller asked for. Modelling it
  as an error is the mistake this union exists to prevent.
- ``failure`` — the run stopped. Carries step, expected, observed and an
  evidence reference, because a failure a human cannot debug is only half
  reported.

A caller pattern-matches on ``status`` and the type checker enforces that all
three arms are handled.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .artifact import TargetStrategy


class FailureKind(str, Enum):
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    """No rung of the locator ladder resolved to exactly one node in time."""

    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    """The action ran but its postcondition never became true."""

    TIMEOUT = "TIMEOUT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    SURFACE_ERROR = "SURFACE_ERROR"
    """The surface itself broke: browser crash, frame detached, transport gone."""

    HUMAN_TIMEOUT = "HUMAN_TIMEOUT"
    """An operator took the lease and let it expire. Never silently reverts."""

    UNRECOVERABLE_CONDITION = "UNRECOVERABLE_CONDITION"


class RecoveryRecord(BaseModel):
    """A handled transient condition.

    Recoveries are logged and returned but do not change the result arm: the
    caller gets ``Success``, and the recovery list is how an operator finds out
    the run had to work for it.
    """

    model_config = ConfigDict(extra="forbid")

    at_step: str
    rule_id: str | None = None
    condition: str
    """Detector or rule that fired, e.g. ``ModalDetector``."""
    action: str
    """What was done: dismiss, retry_step, reload, re_resolve."""
    attempt: int = Field(default=1, ge=1)
    duration_ms: int = Field(default=0, ge=0)
    note: str | None = None


class DriftSignal(BaseModel):
    """A fallback rung won where the primary was expected to.

    Replay still succeeds — this is reported, not swallowed. Aggregated across
    tenants running the same vendor product, a rising count on one locator is
    the earliest signal that a product version has moved.
    """

    model_config = ConfigDict(extra="forbid")

    at_step: str
    expected_strategy: TargetStrategy
    winning_strategy: TargetStrategy
    matches: int = Field(ge=1)
    note: str | None = None


class EscalationRecord(BaseModel):
    """A handoff to a human, and what came back."""

    model_config = ConfigDict(extra="forbid")

    at_step: str
    reason: str
    requested_at: datetime
    resolved_at: datetime | None = None
    operator_note: str | None = None
    human_steps: int = Field(default=0, ge=0)
    outcome: Literal["released", "aborted", "timed_out"] | None = None


class _ResultBase(BaseModel):
    """Fields every arm carries, so no result is untraceable."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    capability_version: str
    run_id: str
    duration_ms: int = Field(default=0, ge=0)
    evidence_ref: str
    """Directory under evidence/ holding run.jsonl, screenshots and the manifest."""


class Success(_ResultBase):
    status: Literal["success"] = "success"
    outputs: dict[str, Any] = {}
    steps_executed: int = Field(default=0, ge=0)
    recoveries: list[RecoveryRecord] = []
    drift: list[DriftSignal] = []
    escalations: list[EscalationRecord] = []


class BusinessOutcomeResult(_ResultBase):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    at_step: str
    terminal: bool = True
    steps_executed: int = Field(default=0, ge=0)
    recoveries: list[RecoveryRecord] = []
    drift: list[DriftSignal] = []


class Failure(_ResultBase):
    status: Literal["failure"] = "failure"
    kind: FailureKind
    at_step: str
    expected: str
    observed: str
    steps_executed: int = Field(default=0, ge=0)
    recoveries: list[RecoveryRecord] = []
    drift: list[DriftSignal] = []
    escalations: list[EscalationRecord] = []


ReplayResult = Annotated[
    Union[Success, BusinessOutcomeResult, Failure],
    Field(discriminator="status"),
]
