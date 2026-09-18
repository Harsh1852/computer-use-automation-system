"""Bounded, declared responses to conditions the run can handle itself.

Recovery is **data, not code**. Every response the executor is allowed to make
is declared in the artifact as a `RecoveryRule` a reviewer approved. If the
executor could invent a response, replay would stop being deterministic — and
"it recovered" would stop being a claim anyone could check.

Three bounds, all enforced here rather than trusted:

* each rule fires at most `max_attempts` times per step (schema caps it at 2),
* the whole run has a global recovery budget, so a page that keeps producing
  the same condition fails instead of looping,
* anything without a matching rule is *not* recovered — it escalates or fails.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..schema import (
    Action,
    ActionType,
    Observation,
    RecoveryAction,
    RecoveryRecord,
    RecoveryRule,
    Step,
)
from ..surface.base import Surface, walk_ladder
from .conditions import ConditionEvaluator, describe

GLOBAL_RECOVERY_BUDGET = 6


@dataclass
class RecoveryOutcome:
    applied: bool
    record: RecoveryRecord | None = None
    exhausted: bool = False
    detail: str = ""


@dataclass
class RecoveryEngine:
    surface: Surface
    evaluator: ConditionEvaluator
    rules: Sequence[RecoveryRule] = field(default_factory=list)
    budget: int = GLOBAL_RECOVERY_BUDGET

    _used: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    _spent: int = field(default=0, init=False)

    async def match(
        self, observation: Observation, params: Mapping[str, Any]
    ) -> RecoveryRule | None:
        for rule in self.rules:
            verdict = await self.evaluator.check(rule.when, observation, params=params)
            if verdict.ok:
                return rule
        return None

    def remaining(self, rule: RecoveryRule, step: Step) -> int:
        return rule.max_attempts - self._used.get((rule.id, step.id), 0)

    async def apply(
        self,
        rule: RecoveryRule,
        step: Step,
        observation: Observation,
        params: Mapping[str, Any],
    ) -> RecoveryOutcome:
        key = (rule.id, step.id)
        used = self._used.get(key, 0)
        if used >= rule.max_attempts:
            return RecoveryOutcome(
                applied=False,
                exhausted=True,
                detail=f"rule {rule.id!r} already used {used}/{rule.max_attempts} at {step.id}",
            )
        if self._spent >= self.budget:
            return RecoveryOutcome(
                applied=False,
                exhausted=True,
                detail=f"run recovery budget of {self.budget} exhausted",
            )

        started = time.perf_counter()
        detail = await self._perform(rule, observation)
        self._used[key] = used + 1
        self._spent += 1

        return RecoveryOutcome(
            applied=True,
            record=RecoveryRecord(
                at_step=step.id,
                rule_id=rule.id,
                condition=describe(rule.when, params),
                action=rule.do.value,
                attempt=used + 1,
                duration_ms=int((time.perf_counter() - started) * 1000),
                note=detail,
            ),
        )

    async def _perform(self, rule: RecoveryRule, observation: Observation) -> str:
        if rule.do is RecoveryAction.DISMISS:
            return await self._dismiss(rule, observation)
        if rule.do is RecoveryAction.RELOAD:
            await self.surface.act(
                Action(type=ActionType.NAVIGATE, url=observation.url, intent="reload"),
                None,
            )
            return f"reloaded {observation.url}"
        if rule.do in (RecoveryAction.RETRY_STEP, RecoveryAction.RE_RESOLVE):
            # Both are handled by the executor simply going round again; the
            # rule exists so the retry is declared and counted rather than
            # implicit.
            return rule.do.value
        raise ValueError(f"unhandled recovery action {rule.do}")  # pragma: no cover

    async def _dismiss(self, rule: RecoveryRule, observation: Observation) -> str:
        frame_path = list(observation.dialogs[0].frame_path) if observation.dialogs else []
        outcome = await walk_ladder(self.surface, rule.target, frame_path)
        if outcome.handle is None:
            # Fall back to the dismiss control the surface spotted structurally.
            for dialog in observation.dialogs:
                if dialog.dismiss_ref is None:
                    continue
                node = observation.by_ref(dialog.dismiss_ref)
                if node is None:
                    continue
                from ..schema import TargetCandidate, TargetStrategy

                found = await self.surface.find(
                    TargetCandidate(
                        strategy=TargetStrategy.A11Y_ROLE_NAME,
                        role=node.role,
                        name=node.name,
                    ),
                    list(node.frame_path),
                )
                if len(found) == 1:
                    await self.surface.act(Action(type=ActionType.CLICK), found[0])
                    return f"dismissed via observed control {node.name!r}"
            return "no dismiss control resolved"

        await self.surface.act(
            Action(type=ActionType.CLICK, intent=f"recovery:{rule.id}"), outcome.handle
        )
        return f"dismissed via {outcome.handle.description}"
