"""One bounded, policy-checked model call, on one failure mode only.

This is the only place a model appears anywhere near the replay path, and
every constraint on it is deliberate:

* It runs **only on `TARGET_NOT_FOUND`**, after the entire recorded ladder has
  been exhausted. It is not consulted about what to do, only about *which
  element* the recorded locator was describing.
* It runs **at most once per run**. Not once per step — once. A drift absorber
  that can fire repeatedly is a model in the loop wearing a disguise.
* It may **only** return a node reference from the observation it was given.
  It cannot propose an action, skip a step, change a value, or reach for a
  selector: the return type makes those unexpressible.
* The action it enables still goes through the policy gate, because the gate
  is in `Surface.act()` and this does not bypass it.
* Every invocation is recorded as evidence, whether or not it helped.

Framed honestly: this is a drift absorber with a hard blast radius. It buys
one retry of one element identification, and the run stays deterministic in
every other respect — including the fact that a *successful* fallback still
reports drift, so a locator quietly kept alive by the model shows up in
telemetry instead of hiding.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..schema import (
    Observation,
    Step,
    TargetCandidate,
    TargetStrategy,
    UiNode,
)

MAX_INVOCATIONS_PER_RUN = 1
MAX_NODES_OFFERED = 60

SYSTEM_PROMPT = """\
You are re-identifying a single UI control for a deterministic replay whose \
recorded locator no longer matches. You are NOT deciding what to do - the \
action is already fixed, and so is the step it belongs to.

The recorded description will often not match any control exactly. That is \
the normal case and the reason you are being asked: the application has been \
re-labelled, or this tenant names the same control differently. Your job is \
to pick the control on this screen that serves the same purpose in the step \
described.

Reply with the ref of that one control, or -1 if genuinely none of them \
serves that purpose. Do not answer -1 merely because the name differs from \
the recorded one - a different name is the expected situation. Do answer -1 \
if two controls are equally plausible, or if the right one is clearly absent: \
a wrong answer makes an automation act on the wrong control, and refusing \
costs one clear failure message.\
"""


@dataclass
class FallbackInvocation:
    """Evidence for one call, recorded whether or not it worked."""

    at_step: str
    model: str
    wanted: str
    offered_nodes: int
    chosen_ref: int | None
    resolved: bool
    note: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "at_step": self.at_step,
            "model": self.model,
            "wanted": self.wanted,
            "offered_nodes": self.offered_nodes,
            "chosen_ref": self.chosen_ref,
            "resolved": self.resolved,
            "note": self.note,
        }


class AssistedFallback:
    """At most one model-assisted re-identification per run."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
        log: Any = None,
        max_invocations: int = MAX_INVOCATIONS_PER_RUN,
    ) -> None:
        self._client = client
        self.model = model or os.environ.get("OPENAI_MODEL") or ""
        self.log = log
        self.max_invocations = max_invocations
        self.invocations: list[FallbackInvocation] = []

    @property
    def available(self) -> bool:
        """False when unconfigured, so replay's default path has no model in it."""
        if len(self.invocations) >= self.max_invocations:
            return False
        if self._client is not None:
            return bool(self.model)
        return bool(self.model and os.environ.get("OPENAI_API_KEY"))

    @property
    def spent(self) -> bool:
        return len(self.invocations) >= self.max_invocations

    # ------------------------------------------------------------- the call

    async def reidentify(
        self, step: Step, observation: Observation
    ) -> TargetCandidate | None:
        """Ask once which node the recorded locator meant. Never twice."""
        if not self.available or step.target is None:
            return None

        wanted = _describe_wanted(step)
        offered = _candidates(step, observation)
        if not offered:
            self._record(step, wanted, 0, None, False, "no nodes of that role on screen")
            return None

        try:
            ref = await self._ask(wanted, offered)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            self._record(step, wanted, len(offered), None, False, detail)
            return None

        node = next((n for n in offered if n.ref == ref), None)
        if node is None:
            self._record(
                step, wanted, len(offered), ref, False,
                "declined or returned a ref that was not offered",
            )
            return None

        candidate = _candidate_for(node)
        if candidate is None:
            self._record(
                step, wanted, len(offered), ref, False,
                f"chose {node.role} with no stable name to locate it by",
            )
            return None

        self._record(
            step, wanted, len(offered), ref, True,
            f"re-identified as {node.role} {node.name!r}",
        )
        return candidate

    async def _ask(self, wanted: str, offered: list[UiNode]) -> int:
        client = self._client or self._default_client()
        rendered = "\n".join(
            f"  [{n.ref}] {n.role} {n.name!r}"
            + (f" value={n.value!r}" if n.value else "")
            + (f" frame={'/'.join(n.frame_path)}" if n.frame_path else "")
            for n in offered
        )
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"The recording was looking for:\n  {wanted}\n\n"
                        f"Controls currently on screen:\n{rendered}\n\n"
                        'Reply with JSON only: {"ref": <number>}'
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        return int(payload.get("ref", -1))

    def _default_client(self):
        from openai import AsyncOpenAI

        return AsyncOpenAI()

    def _record(self, step, wanted, offered, ref, resolved, note) -> None:
        invocation = FallbackInvocation(
            at_step=step.id,
            model=self.model,
            wanted=wanted,
            offered_nodes=offered,
            chosen_ref=ref,
            resolved=resolved,
            note=note,
        )
        self.invocations.append(invocation)
        if self.log is not None:
            self.log.event("fallback.invoked", **invocation.as_dict())

    def as_evidence(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in self.invocations]


# --------------------------------------------------------------- helpers


def _describe_wanted(step: Step) -> str:
    primary = step.target.primary
    bits = [f"a {primary.role or 'control'}"]
    if primary.name:
        bits.append(f"named {primary.name!r}")
    if primary.anchor:
        bits.append(f"beside the text {primary.anchor!r}")
    if primary.index is not None:
        bits.append(f"at position {primary.index}")
    bits.append(f"for the step: {step.intent}")
    return " ".join(bits)


def _candidates(step: Step, observation: Observation) -> list[UiNode]:
    """Only nodes in the right frame, and only of the recorded role.

    Narrowing before asking is part of the blast radius: the model is choosing
    among plausible controls, not browsing the page.
    """
    role = step.target.primary.role
    nodes = [
        n
        for n in observation.nodes
        if n.frame_path == step.frame_path
        and (role is None or n.role == role)
        and n.interactable
    ]
    return nodes[:MAX_NODES_OFFERED]


def _candidate_for(node: UiNode) -> TargetCandidate | None:
    """Build a locator from the chosen node — never a selector.

    The model's answer is laundered back into the same closed strategy set the
    recorder uses, so an assisted resolution produces the kind of locator a
    reviewer already knows how to read.
    """
    if node.name:
        return TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME,
            role=node.role,
            name=node.name,
            matches_at_record=1,
        )
    return None
