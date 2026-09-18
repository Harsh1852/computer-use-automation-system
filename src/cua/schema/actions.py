"""The action vocabulary.

Deliberately tiny and surface-agnostic. Every one of these seven verbs is
expressible against a browser, a legacy frameset, or a Win32 window read
through UIA — which is the point. Nothing here knows what a DOM is.

This module has no imports from the rest of the package, so it can be the
shared vocabulary for the artifact schema, the surface protocol and the
policy gate without creating a cycle.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    READ = "read"
    WAIT = "wait"
    ASSERT = "assert"


class Risk(str, Enum):
    """How much damage getting this wrong does.

    The classification is per-action, assigned at record time, and is what the
    policy gate keys off. It is deliberately coarse: three buckets a human
    reviewer can apply consistently beat ten they cannot.
    """

    SAFE_REVERSIBLE = "safe_reversible"
    STATE_CHANGING = "state_changing"
    IRREVERSIBLE = "irreversible"


class Action(BaseModel):
    """A fully resolved action on its way to ``Surface.act()``.

    By the time an ``Action`` exists, parameters have been substituted and
    secrets dereferenced — so ``text`` may hold a password. It is marked
    ``repr=False`` so it cannot leak through an exception traceback or a
    careless f-string, and ``sensitive`` tells the log sink to redact it
    without the sink needing to understand the artifact schema.
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    step_id: str = "-"
    intent: str = ""
    risk: Risk = Risk.SAFE_REVERSIBLE

    url: str | None = None
    """Destination for ``navigate``."""

    text: str | None = Field(default=None, repr=False)
    """Text for ``type``. May be secret; never repr'd."""

    option: str | None = None
    """Option label or value for ``select``."""

    sensitive: bool = False
    """Set when ``text`` came from a secret or a PII-tagged parameter."""

    timeout_ms: int = Field(default=15_000, gt=0)


class ActionResult(BaseModel):
    """What the surface reports back. Never raises across the seam."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: ActionType
    step_id: str = "-"
    duration_ms: int = Field(default=0, ge=0)
    url_after: str | None = None
    read_value: str | None = Field(default=None, repr=False)
    detail: str | None = None
