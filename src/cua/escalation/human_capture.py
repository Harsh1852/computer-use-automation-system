"""Recording what the human did, in the same timeline as what the machine did.

An escalation that hands over a browser and records nothing produces an
evidence trail with a hole in the middle: the run stops, something happens,
the run resumes with different state and no explanation. Capturing the
operator's actions closes that hole, and it is what makes the handoff
auditable rather than merely functional.

The values are redacted, always. What gets recorded is the *identity* of the
control — role, accessible name, frame path — and the shape of what was
entered, never the content. "The operator typed 8 characters into the field
beside PASSWORD" is exactly the right amount of information: it explains the
resume, and it is safe to keep.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class HumanStep(BaseModel):
    """One operator action, shaped like a `Step` so the timeline reads evenly."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    kind: str
    """click | change | submit"""

    role: str
    name: str | None = None
    frame_path: list[str] = []
    url: str | None = None

    value: str | None = None
    """Always a shape, never content: ``<redacted:8 chars>``."""

    sensitive: bool = False
    """True when the control was a password field: not even a length is kept."""


def _redact(payload: dict[str, Any]) -> tuple[str | None, bool]:
    if payload.get("is_password"):
        return None, True
    length = payload.get("value_len")
    if not length:
        return None, False
    return f"<redacted:{length} chars>", False


class HumanCapture:
    """Sink for operator actions. Writes to the run log as they happen.

    Logging on arrival rather than at the end is what interleaves human and
    automation steps in `run.jsonl` — the timeline shows who did what, in
    order, without anything having to merge two lists afterwards.
    """

    def __init__(
        self,
        log: Any,
        *,
        is_active: Callable[[], bool] | None = None,
        on_step: Callable[[HumanStep], None] | None = None,
    ) -> None:
        self._log = log
        self._is_active = is_active
        self._on_step = on_step
        self.steps: list[HumanStep] = []
        self.ignored = 0

    def __call__(self, payload: dict[str, Any]) -> None:
        self.record(payload)

    def record(self, payload: dict[str, Any]) -> None:
        # The listeners stay installed after hand-back — reinstalling them per
        # escalation would be fragile — so the gate is the lease, not the
        # listener. Without it, every automation click after a resume is
        # logged as something a person did, and the timeline stops being an
        # answer to "who did what".
        if self._is_active is not None and not self._is_active():
            self.ignored += 1
            return

        value, sensitive = _redact(payload)
        step = HumanStep(
            at=datetime.now(timezone.utc),
            kind=str(payload.get("kind", "unknown")),
            role=str(payload.get("role", "unknown")),
            name=payload.get("name") or None,
            frame_path=list(payload.get("frame_path") or []),
            url=payload.get("url"),
            value=value,
            sensitive=sensitive,
        )
        self.steps.append(step)
        self._log.event(
            "human.action",
            kind=step.kind,
            role=step.role,
            name=step.name,
            frame="/".join(step.frame_path) or "(top)",
            value=("<password>" if step.sensitive else step.value),
        )
        if self._on_step is not None:
            self._on_step(step)

    def as_timeline(self) -> list[dict[str, Any]]:
        return [s.model_dump(mode="json") for s in self.steps]
