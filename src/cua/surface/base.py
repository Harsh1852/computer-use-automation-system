"""The surface seam.

This is the load-bearing boundary of the whole system. Everything above it —
the resolver, the detectors, the executor, the discovery agent's prompt — is
written against `Observation`, `Action` and `Handle`, and never learns what is
underneath. Everything below it is free to be a browser, a frameset, or a
Win32 window read through UIA.

Concretely, a new surface implements six things: `observe`, `find`, `act`,
`snapshot`, `pause`, `resume`, plus a lifecycle. It does **not** implement the
locator ladder. `walk_ladder` below is shared policy — "try the primary, then
each fallback in recorded order, and treat an ambiguous match as a miss" is a
rule about robustness, not about browsers, so every surface must obey the
identical version of it rather than reimplementing it slightly differently.

That split is the answer to the heterogeneity question: a `DesktopSurface`
supplies perception and actuation; determinism is inherited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from ..schema import (
    Action,
    ActionResult,
    Observation,
    Snapshot,
    SurfaceKind,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
)


class SurfaceError(RuntimeError):
    """The surface itself broke: browser gone, frame detached, transport dead."""


class PolicyViolation(RuntimeError):
    """The gate refused an action. Carries the rule that refused it."""

    def __init__(self, message: str, rule: str = "unspecified") -> None:
        super().__init__(message)
        self.rule = rule


class LeaseViolation(RuntimeError):
    """Something tried to act while it did not hold the session lease."""


@dataclass(frozen=True, kw_only=True)
class Handle:
    """An opaque, surface-owned reference to exactly one resolved element.

    Deliberately carries no way to *do* anything. Callers pass a handle back to
    the surface that produced it; if a handle exposed behaviour, the seam would
    leak and every caller would start depending on the underlying driver.

    The metadata it does carry exists so the layer above can report how the
    element was found — which rung won, and how many things it matched —
    without being able to touch the element itself.
    """

    strategy: TargetStrategy
    matched: int
    description: str
    frame_path: tuple[str, ...] = ()
    ref: int | None = None


@dataclass
class LadderOutcome:
    """The result of walking a locator ladder, including the near misses.

    Kept separate from `Handle` because a failed resolution still has to be
    reportable: `Failure(TARGET_NOT_FOUND)` is specified to carry the observed
    nodes of the same role, and this is where that evidence comes from.
    """

    handle: Handle | None = None
    winning_index: int | None = None
    attempts: list[tuple[TargetCandidate, int]] = field(default_factory=list)
    """Each rung tried, with how many nodes it matched."""

    @property
    def drifted(self) -> bool:
        """True when a fallback won and the primary did not."""
        return self.winning_index is not None and self.winning_index > 0

    @property
    def ambiguous(self) -> bool:
        return any(count > 1 for _, count in self.attempts)

    def describe_attempts(self) -> str:
        return "; ".join(
            f"{c.strategy.value}->{n}" for c, n in self.attempts
        ) or "no candidates"


# --------------------------------------------------------------------------
# Enforcement seams
#
# `act()` is the single chokepoint: it is the only place a policy decision or
# a lease check happens. Both are Protocols with permissive defaults here, so
# the call sites are real from this phase onward and the policy and escalation
# phases supply implementations rather than inserting new call sites.
# --------------------------------------------------------------------------


@runtime_checkable
class Gate(Protocol):
    """Decides whether an action may run. Raises `PolicyViolation` if not."""

    def check(self, action: Action, context: Mapping[str, Any]) -> None: ...


@runtime_checkable
class Lease(Protocol):
    """Single-writer guard. Raises `LeaseViolation` if the caller is not holder."""

    def assert_holder(self, who: str) -> None: ...


class OpenGate:
    """Allows everything.

    Not a stub for a missing mechanism — it is the null object for the gate
    seam, used by scripts that run outside a policy context (the observation
    demo, unit tests). Production paths are constructed with the real gate.
    """

    def check(self, action: Action, context: Mapping[str, Any]) -> None:
        return None


class UnheldLease:
    """Grants to anyone. Used before a session is placed under a lease."""

    def assert_holder(self, who: str) -> None:
        return None


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


@runtime_checkable
class Surface(Protocol):
    """What every surface must provide. No driver type may appear in here."""

    kind: SurfaceKind

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def observe(self) -> Observation: ...

    async def find(
        self, candidate: TargetCandidate, frame_path: list[str]
    ) -> list[Handle]:
        """Every element matching one rung. Returning more than one is allowed
        and meaningful — the ambiguity rule is applied above, not here."""
        ...

    async def resolve(self, spec: TargetSpec, frame_path: list[str]) -> Handle | None: ...

    async def act(self, action: Action, handle: Handle | None) -> ActionResult: ...

    async def snapshot(self, label: str = "snapshot") -> Snapshot: ...

    async def pause(self) -> None: ...
    async def resume(self) -> None: ...


# --------------------------------------------------------------------------
# Shared determinism policy
# --------------------------------------------------------------------------


async def walk_ladder(
    surface: Surface, spec: TargetSpec, frame_path: list[str]
) -> LadderOutcome:
    """Try each rung in recorded order and return the first unambiguous match.

    Two rules, both deliberate:

    * **Ambiguity is a miss.** A rung that matches more than one node is
      recorded and skipped, never resolved by taking the first. Silently
      picking one is how a replay clicks the wrong row for six months before
      anyone notices.
    * **Every attempt is kept.** Whether resolution succeeded or not, the
      caller gets the per-rung match counts, so a drift signal or a debuggable
      failure can be built from the same data.
    """
    outcome = LadderOutcome()
    for index, candidate in enumerate(spec.ladder):
        matches = await surface.find(candidate, frame_path)
        outcome.attempts.append((candidate, len(matches)))
        if len(matches) == 1:
            outcome.handle = matches[0]
            outcome.winning_index = index
            return outcome
    return outcome


def render_table(observation: Observation) -> str:
    """Flat, greppable rendering of an observation.

    Lives on the driver-free side of the seam because it is a pure function of
    `Observation` — a desktop surface's observation renders through the same
    code. Used by the CLI, by evidence capture, and (rendered more selectively)
    as what the discovery model reads.
    """
    header = f"{'REF':>4}  {'FRAME':<14} {'ROLE':<14} {'NAME':<40} {'VALUE':<14} SRC"
    lines = [
        f"url    {observation.url}",
        f"title  {observation.title}",
        f"status {observation.http_status}",
        f"nodes  {len(observation.nodes)}   dialogs {len(observation.dialogs)}"
        f"   banners {len(observation.banners)}",
        "",
        header,
        "-" * len(header),
    ]
    for node in observation.nodes:
        frame = "/".join(node.frame_path) or "(top)"
        name = (node.name or "")[:40]
        value = (node.value or "")[:14]
        lines.append(
            f"{node.ref:>4}  {frame:<14} {node.role:<14} {name:<40} {value:<14}"
            f" {node.name_source.value}"
        )
    for dialog in observation.dialogs:
        lines.append(f"\nDIALOG {dialog.title!r} dismiss_ref={dialog.dismiss_ref}")
        lines.append(f"       {dialog.text[:160]}")
    for banner in observation.banners:
        lines.append(f"\nBANNER [{banner.region}] {banner.text[:160]}")
    return "\n".join(lines)
