"""Who is allowed to drive the session.

The lease is the whole control-transfer model in one object. Exactly one
holder at a time, and `Surface.act()` asserts the caller is that holder before
it touches the browser — so "the automation and the human both acted at once"
is not a race to be avoided by convention, it is a `LeaseViolation`.

Two properties are deliberate and worth defending:

* **An expired operator hold terminates the run.** It does not revert to
  automation. If a person took control because something was wrong and then
  walked away, resuming automation is the single worst thing to do: the page
  is in whatever half-finished state they left it, and the executor's model of
  where it is no longer matches reality.
* **Hand-back is explicit.** Automation resumes because an operator released,
  never because a timer elapsed or the page happened to look right again.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AUTOMATION = "automation"
OPERATOR = "operator"

DEFAULT_HOLD_SECONDS = 600


class LeaseViolation(RuntimeError):
    """Something tried to act while it did not hold the lease."""


class Handback(str, Enum):
    RESUMED = "resumed"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"


class SessionLease(BaseModel):
    """The serialisable state of the lease, as the console and evidence see it."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    holder: Literal["automation", "operator"] = AUTOMATION
    token: str
    since: datetime
    reason: str | None = None
    expires_at: datetime | None = None
    """Operator holds are time-boxed. Automation holds are not."""

    @property
    def seconds_remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds()

    @property
    def expired(self) -> bool:
        remaining = self.seconds_remaining
        return remaining is not None and remaining <= 0


class LeaseManager:
    """Runtime owner of the lease and the hand-back signal."""

    def __init__(self, session_id: str, *, hold_seconds: int = DEFAULT_HOLD_SECONDS) -> None:
        self.hold_seconds = hold_seconds
        self._automation_token = secrets.token_urlsafe(16)
        self._lease = SessionLease(
            session_id=session_id,
            holder=AUTOMATION,
            token=self._automation_token,
            since=datetime.now(timezone.utc),
        )
        self._handback = asyncio.Event()
        self._outcome: Handback | None = None
        self._operator_note: str | None = None

    # ------------------------------------------------------------- reading

    @property
    def lease(self) -> SessionLease:
        return self._lease

    @property
    def held_by_operator(self) -> bool:
        return self._lease.holder == OPERATOR

    @property
    def operator_note(self) -> str | None:
        return self._operator_note

    # ------------------------------------------- the single-writer invariant

    def assert_holder(self, who: str) -> None:
        """Called from `Surface.act()` and nowhere else.

        `who` is the actor name for automation, or the operator's bearer token.
        """
        if who == AUTOMATION:
            if self._lease.holder != AUTOMATION:
                raise LeaseViolation(
                    "automation tried to act while the operator holds the session lease"
                )
            return
        if who != self._lease.token:
            raise LeaseViolation("caller does not hold the session lease")

    # ------------------------------------------------------------ transfer

    def take(self, reason: str, *, hold_seconds: int | None = None) -> str:
        """Hand control to a person. Returns their bearer token."""
        if self._lease.holder == OPERATOR:
            raise LeaseViolation("the operator already holds this session")
        seconds = hold_seconds or self.hold_seconds
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc)
        self._lease = SessionLease(
            session_id=self._lease.session_id,
            holder=OPERATOR,
            token=token,
            since=now,
            reason=reason,
            expires_at=now + timedelta(seconds=seconds),
        )
        return token

    def release(self, token: str, note: str | None = None) -> None:
        """Hand control back to automation. Only the holder may."""
        self._require_operator(token)
        self._operator_note = note
        self._lease = SessionLease(
            session_id=self._lease.session_id,
            holder=AUTOMATION,
            token=self._automation_token,
            since=datetime.now(timezone.utc),
            reason=note,
        )
        self._finish(Handback.RESUMED)

    def abort(self, token: str, note: str | None = None) -> None:
        self._require_operator(token)
        self._operator_note = note
        self._finish(Handback.ABORTED)

    def expire(self) -> None:
        """Time ran out. The run ends; control is *not* returned."""
        self._finish(Handback.TIMED_OUT)

    def _require_operator(self, token: str) -> None:
        if self._lease.holder != OPERATOR:
            raise LeaseViolation("no operator holds this session")
        if token != self._lease.token:
            raise LeaseViolation("token does not match the current operator hold")

    def _finish(self, outcome: Handback) -> None:
        self._outcome = outcome
        self._handback.set()

    # -------------------------------------------------------------- waiting

    async def wait_for_handback(self, timeout_s: float) -> Handback:
        """Park the executor until a person resolves this, or time runs out.

        `timeout_s` covers the whole intervention — the time waiting for
        somebody to notice plus the time they spend working. An unattended
        request must not hang a run forever.
        """
        try:
            await asyncio.wait_for(self._handback.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            self.expire()
        return self._outcome or Handback.TIMED_OUT

    def reset_for_next_escalation(self) -> None:
        self._handback = asyncio.Event()
        self._outcome = None
