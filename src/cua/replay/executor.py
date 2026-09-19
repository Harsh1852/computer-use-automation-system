"""Deterministic replay.

No model is consulted anywhere in this file. Given an artifact and a set of
parameters, the same inputs produce the same steps, and every decision the
executor makes is either declared in the artifact or is one of the fixed
rules below.

The loop, per step:

1. observe
2. run the standing detector set — before the action, not only after
3. resolve the target through the recorded ladder
4. act (the surface applies the lease check and the policy gate)
5. wait for the postcondition, re-running detectors on every observation
6. record evidence

Waiting is the subtle part. Step 5 is not "sleep, then check": it polls
observations until the postcondition holds, a detector fires, or the step's
own `timeout_ms` expires. That ordering is what keeps a business outcome from
being reported as a checkpoint failure — when the app answers "no such
member", the postcondition will never become true, and the run must return
the answer rather than time out complaining about it.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..evidence import EvidenceWriter, RunLog, utcnow, write_manifest
from ..schema import (
    Action,
    ActionType,
    BusinessOutcomeResult,
    Capability,
    DriftSignal,
    EscalationRecord,
    ExtractFrom,
    Failure,
    FailureKind,
    Observation,
    ParamType,
    RecoveryRecord,
    ReplayResult,
    Risk,
    Step,
    Success,
    Transform,
    ValueRef,
)
from ..surface.base import (
    Handle,
    LeaseViolation,
    PolicyViolation,
    Surface,
    SurfaceError,
)
from .conditions import ConditionEvaluator, Verdict, describe, interpolate
from .detectors import DetectorConfig, DetectorSet, Finding, FindingKind, ScanContext
from .recovery import RecoveryEngine
from .resolver import StepResolver

POLL_INTERVAL_S = 0.15
"""Gap between observations while waiting on a condition.

This is a polling cadence, not a synchronisation sleep: the executor never
waits *instead of* checking, and never waits a fixed amount and then assumes.
Every wait is bounded by the step's own recorded `timeout_ms`."""

RETRY_BACKOFF_S = (0.4, 1.2)
MAX_STEP_ATTEMPTS = 3  # the original plus two retries

MAX_ESCALATIONS = 2
"""How many times one run may ask a human for help.

Resume is bounded, never a loop. On hand-back the executor re-runs the guard
rather than blindly continuing, so if the operator did not actually fix the
condition the same finding fires again — and the second time it stops."""


class Escalator(Protocol):
    """Hands an unrecoverable-but-human-fixable condition to a person."""

    async def escalate(self, request: "EscalationContext") -> "EscalationDecision": ...


@dataclass
class EscalationContext:
    step: Step
    finding: Finding
    observation: Observation
    escalation_count: int


@dataclass
class EscalationDecision:
    resumed: bool
    record: EscalationRecord | None = None
    note: str = ""
    failure_kind: FailureKind | None = None
    """Set when the handoff itself failed — an expired hold is HUMAN_TIMEOUT,
    which is a different thing from the condition that caused the escalation."""


class NoEscalation:
    """Fails the run instead of escalating.

    A null object, not a stub: with no operator attached, the correct
    behaviour for an unrecoverable condition is to stop with a clear failure,
    never to guess. The escalation phase supplies a real implementation
    without the executor growing a new call site.
    """

    async def escalate(self, request: EscalationContext) -> EscalationDecision:
        return EscalationDecision(
            resumed=False, note="no operator attached; escalation unavailable"
        )


@dataclass
class RunState:
    run_id: str
    recoveries: list[RecoveryRecord] = field(default_factory=list)
    drift: list[DriftSignal] = field(default_factory=list)
    escalations: list[EscalationRecord] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    step_timings: dict[str, int] = field(default_factory=dict)
    steps_executed: int = 0
    authenticated: bool = False
    escalation_count: int = 0
    escalated_s: float = 0.0
    """Total wall time this run spent parked waiting for a person."""


class ReplayExecutor:
    def __init__(
        self,
        surface: Surface,
        *,
        run_dir: str,
        log: RunLog | None = None,
        evidence: EvidenceWriter | None = None,
        escalator: Escalator | None = None,
        detector_config: DetectorConfig | None = None,
        secrets: Mapping[str, str] | None = None,
        reauth_allowed: bool = False,
        fallback: Any | None = None,
        verbose_capture: bool = False,
    ) -> None:
        self.surface = surface
        self.run_dir = run_dir
        self.evidence = evidence or EvidenceWriter(run_dir, verbose=verbose_capture)
        self.escalator = escalator or NoEscalation()
        self.detector_config = detector_config or DetectorConfig()
        self.secrets = dict(secrets) if secrets is not None else dict(os.environ)
        self.reauth_allowed = reauth_allowed
        # None means no model anywhere in the replay path, which is the
        # default. An assisted fallback is opt-in per run.
        self.fallback = fallback
        self.resolver = StepResolver(surface)
        self.evaluator = ConditionEvaluator(resolve_target=self._resolve_for_condition)
        self._log = log

    # --------------------------------------------------------------- driving

    async def run(
        self, capability: Capability, params: Mapping[str, Any]
    ) -> ReplayResult:
        run_id = f"run-{uuid.uuid4().hex[:10]}"
        log = self._log or RunLog(self.run_dir, run_id)
        state = RunState(run_id=run_id)
        started_at = utcnow()
        started = time.perf_counter()

        log.bind(capability=capability.ref, tenant=capability.target.tenant_id)
        log.event(
            "run.start",
            steps=len(capability.steps),
            outcomes=[o.code for o in capability.outcomes],
            approval=capability.approval.state.value,
        )

        detectors = DetectorSet(capability.outcomes, self.evaluator, self.detector_config)
        recovery = RecoveryEngine(
            surface=self.surface,
            evaluator=self.evaluator,
            rules=capability.recoveries,
            reauth_allowed=self.reauth_allowed,
            rerun_steps=lambda ids: self._rerun(capability, ids, params, state, log),
        )

        violation = self._validate_contract(capability, params)
        if violation is not None:
            return await self._finish(
                self._fail(
                    capability, state, FailureKind.CONTRACT_VIOLATION, "-",
                    violation[0], violation[1],
                ),
                capability, state, params, started_at, started, log,
            )

        try:
            result = await self._run_steps(
                capability, params, state, detectors, recovery, log
            )
        except PolicyViolation as exc:
            result = self._fail(
                capability, state, FailureKind.POLICY_BLOCKED, state_step(state, capability),
                "an action permitted by policy", f"{exc} (rule: {exc.rule})",
            )
        except LeaseViolation as exc:
            result = self._fail(
                capability, state, FailureKind.HUMAN_TIMEOUT,
                state_step(state, capability), "the automation holds the lease", str(exc),
            )
        except SurfaceError as exc:
            result = self._fail(
                capability, state, FailureKind.SURFACE_ERROR,
                state_step(state, capability), "a working surface", str(exc),
            )

        return await self._finish(
            result, capability, state, params, started_at, started, log
        )

    async def _run_steps(
        self, capability, params, state, detectors, recovery, log
    ) -> ReplayResult:
        for step in capability.steps:
            outcome = await self._run_step(
                capability, step, params, state, detectors, recovery, log
            )
            if outcome is not None:
                return outcome
            state.steps_executed += 1

        observation = await self.surface.observe()
        verdict = await self.evaluator.check(
            capability.success_condition, observation, params=params
        )
        log.event(
            "run.success_condition",
            ok=verdict.ok,
            expected=verdict.expected,
            observed=verdict.observed,
        )
        if not verdict.ok:
            await self.evidence.step_capture(self.surface, "success-check", force=True)
            return self._fail(
                capability, state, FailureKind.CHECKPOINT_FAILED, capability.steps[-1].id,
                verdict.expected, verdict.observed,
            )

        return Success(
            capability_id=capability.id,
            capability_version=capability.version,
            run_id=state.run_id,
            evidence_ref=self.run_dir,
            outputs=state.outputs,
            steps_executed=state.steps_executed,
            recoveries=state.recoveries,
            drift=state.drift,
            escalations=state.escalations,
        )

    # ------------------------------------------------------------- one step

    async def _run_step(
        self, capability, step: Step, params, state, detectors, recovery, log
    ) -> ReplayResult | None:
        attempts = MAX_STEP_ATTEMPTS if self._retryable(step) else 1
        step_started = time.perf_counter()
        escalated_before_step = state.escalated_s

        for attempt in range(1, attempts + 1):
            observation = await self.surface.observe()
            ctx = ScanContext(
                params=params, step=step, authenticated=state.authenticated
            )

            guard = await self._guard(
                capability, step, observation, ctx, state, detectors, recovery, params, log
            )
            if guard is not None:
                return guard

            observation = await self.surface.observe()
            resolution = await self.resolver.resolve(step, observation)
            if resolution.drift is not None:
                state.drift.append(resolution.drift)
                log.event(
                    "step.drift",
                    step=step.id,
                    expected=resolution.drift.expected_strategy.value,
                    winning=resolution.drift.winning_strategy.value,
                    note=resolution.drift.note,
                )

            if not resolution.ok and step.action in _NEEDS_ELEMENT:
                if attempt < attempts:
                    await self._backoff(attempt, step, state, log, "target not yet present")
                    continue

                # The one place a model may touch the replay path, and only
                # after the entire recorded ladder has been exhausted.
                assisted = await self._assisted_resolve(step, observation, state, log)
                if assisted is not None:
                    resolution = assisted
                else:
                    await self.evidence.step_capture(
                        self.surface, f"{step.id}-notfound", force=True
                    )
                    self.evidence.observation_dump(observation, f"{step.id}-notfound")
                    return self._fail(
                        capability, state, FailureKind.TARGET_NOT_FOUND, step.id,
                        resolution.expected, resolution.observed,
                    )

            action = self._build_action(step, params)
            log.event(
                "step.act",
                step=step.id,
                intent=step.intent,
                action=step.action.value,
                risk=step.risk.value,
                frame="/".join(step.frame_path) or "(top)",
                target=(resolution.handle.description if resolution.handle else None),
                strategy=(resolution.handle.strategy.value if resolution.handle else None),
                value=self._loggable_value(step, action, params),
                attempt=attempt,
            )

            result = await self.surface.act(action, resolution.handle)
            if not result.ok:
                if attempt < attempts:
                    await self._backoff(attempt, step, state, log, result.detail or "action failed")
                    continue
                await self.evidence.step_capture(self.surface, f"{step.id}-actfail", force=True)
                return self._fail(
                    capability, state, await self._classify_action_failure(result),
                    step.id,
                    f"{step.action.value} to succeed", result.detail or "action failed",
                )

            if step.action is ActionType.READ:
                self._bind_output(capability, step, result.read_value, state, log)

            settled = await self._await_postcondition(
                capability, step, params, state, detectors, recovery, log
            )
            if isinstance(settled, (Success, BusinessOutcomeResult, Failure)):
                return settled
            if settled is True:
                # Exclude time parked waiting for a person: a five minute
                # hold is not a five minute page load, and reporting it as
                # one would make the slow-response signal meaningless.
                took_ms = int(
                    (time.perf_counter() - step_started
                     - (state.escalated_s - escalated_before_step)) * 1000
                )
                state.step_timings[step.id] = took_ms
                self._note_if_slow(step, took_ms, state, log)
                await self.evidence.step_capture(self.surface, f"{step.id}-{step.action.value}")
                return None

            if attempt < attempts:
                await self._backoff(attempt, step, state, log, "postcondition not met")
                continue

            observation = await self.surface.observe()
            verdict = await self.evaluator.check(
                step.postcondition, observation, params=params, step=step, secrets=self.secrets
            )
            await self.evidence.step_capture(self.surface, f"{step.id}-checkpoint", force=True)
            self.evidence.observation_dump(observation, f"{step.id}-checkpoint")
            return self._fail(
                capability, state, FailureKind.CHECKPOINT_FAILED, step.id,
                verdict.expected, verdict.observed,
            )

        return None  # pragma: no cover - loop always returns or continues

    # ------------------------------------------------------------- the guard

    async def _guard(
        self, capability, step, observation, ctx, state, detectors, recovery, params, log
    ) -> ReplayResult | None:
        """Standing detectors, resolved in precedence order.

        Returns a terminal result, or None when the run may proceed. Recovery
        loops here rather than in the caller so a dismissed modal is
        immediately re-checked: dismissing one thing can reveal another.
        """
        for _ in range(recovery.budget + 1):
            finding = await detectors.scan(observation, ctx)
            if finding is None:
                return None

            log.event(
                "guard.finding",
                step=step.id,
                detector=finding.detector,
                kind=finding.kind.value,
                message=finding.message,
                code=finding.code,
            )

            if finding.kind is FindingKind.BUSINESS_OUTCOME:
                await self.evidence.step_capture(self.surface, f"{step.id}-outcome", force=True)
                self.evidence.observation_dump(observation, f"{step.id}-outcome")
                return BusinessOutcomeResult(
                    capability_id=capability.id,
                    capability_version=capability.version,
                    run_id=state.run_id,
                    evidence_ref=self.run_dir,
                    code=finding.code or "UNKNOWN",
                    message=finding.message,
                    at_step=step.id,
                    terminal=finding.terminal,
                    steps_executed=state.steps_executed,
                    recoveries=state.recoveries,
                    drift=state.drift,
                )

            rule = await recovery.match(observation, params)
            if rule is not None:
                outcome = await recovery.apply(rule, step, observation, params)
                if outcome.applied and outcome.record is not None:
                    state.recoveries.append(outcome.record)
                    log.event(
                        "guard.recovered",
                        step=step.id,
                        rule=rule.id,
                        action=rule.do.value,
                        attempt=outcome.record.attempt,
                        note=outcome.record.note,
                    )
                    observation = await self.surface.observe()
                    continue
                log.event("guard.recovery_exhausted", step=step.id, detail=outcome.detail)

            if finding.kind is FindingKind.AUTH_WALL:
                if state.escalation_count >= MAX_ESCALATIONS:
                    return self._fail(
                        capability, state, FailureKind.UNRECOVERABLE_CONDITION, step.id,
                        finding.expected,
                        f"{finding.observed}; escalated {state.escalation_count} times "
                        "and the condition persisted",
                    )
                decision = await self._escalate(step, finding, observation, state, log)
                if decision.resumed:
                    # Deliberately not "continue from where we were": the loop
                    # re-observes and re-runs the guard, so if the operator did
                    # not actually fix it, the finding fires again and the
                    # bound above stops the second round.
                    observation = await self.surface.observe()
                    continue
                return self._fail(
                    capability, state,
                    decision.failure_kind or FailureKind.UNRECOVERABLE_CONDITION,
                    step.id, finding.expected, f"{finding.observed} ({decision.note})",
                )

            await self.evidence.step_capture(self.surface, f"{step.id}-{finding.kind.value}", force=True)
            self.evidence.observation_dump(observation, f"{step.id}-{finding.kind.value}")
            return self._fail(
                capability, state, FailureKind.UNRECOVERABLE_CONDITION, step.id,
                finding.expected, finding.observed,
            )

        return self._fail(
            capability, state, FailureKind.UNRECOVERABLE_CONDITION, step.id,
            "a recoverable condition to clear",
            "the same condition kept reappearing after recovery",
        )

    # ----------------------------------------------------------- the waiting

    async def _await_postcondition(
        self, capability, step, params, state, detectors, recovery, log
    ) -> bool | ReplayResult:
        """Poll until the checkpoint holds, a detector fires, or time runs out.

        Detectors are checked on every observation, ahead of the postcondition.
        That order is what makes "no such member" a returned answer instead of
        a checkpoint timeout.
        """
        started = time.perf_counter()
        escalated_at_entry = state.escalated_s
        timeout_s = step.timing.timeout_ms / 1000

        while True:
            # A step's timeout measures the *application*, not the operator.
            # Time parked waiting for a human is added back, or any hold
            # longer than the step timeout would guarantee a checkpoint
            # failure the moment control came back — punishing the run for
            # the very handoff that rescued it.
            deadline = started + timeout_s + (state.escalated_s - escalated_at_entry)
            observation = await self.surface.observe()
            ctx = ScanContext(params=params, step=step, authenticated=state.authenticated)

            guard = await self._guard(
                capability, step, observation, ctx, state, detectors, recovery, params, log
            )
            if guard is not None:
                return guard

            observation = await self.surface.observe()
            verdict = await self.evaluator.check(
                step.postcondition, observation, params=params, step=step, secrets=self.secrets
            )
            elapsed = time.perf_counter() - started

            if verdict.ok:
                # "We have been past sign-on" is derived from where the run
                # actually is, not from how a step was worded. Without it the
                # login page at step one is indistinguishable from a session
                # that just died.
                top = observation.frame_urls.get("", observation.url)
                if not any(
                    fnmatch.fnmatchcase(top, g)
                    for g in self.detector_config.login_url_globs
                ):
                    state.authenticated = True
                log.event(
                    "step.checkpoint",
                    step=step.id, ok=True, expected=verdict.expected,
                    elapsed_ms=int(elapsed * 1000),
                )
                return True

            if time.perf_counter() >= deadline:
                log.event(
                    "step.checkpoint",
                    step=step.id, ok=False, expected=verdict.expected,
                    observed=verdict.observed, elapsed_ms=int(elapsed * 1000),
                )
                return False

            await asyncio.sleep(POLL_INTERVAL_S)

    @staticmethod
    def _slow_threshold_ms(step: Step) -> int:
        """Three times the recorded p50, floored at a second.

        Derived from the artifact's own timing evidence rather than a global
        constant: a step recorded at 40ms and one recorded at 6s have very
        different ideas of "slow"."""
        return max(1_000, step.timing.observed_ms_p50 * 3)

    def _note_if_slow(self, step: Step, took_ms: int, state: RunState, log) -> None:
        """A step that took far longer than recorded absorbed something.

        Measured across the whole step, not just the wait: the transient
        delay usually lands inside the action itself, while a click is
        waiting for the page it triggered. Measuring only the checkpoint
        poll would report nothing and quietly hide the condition — which is
        the behaviour this record exists to prevent.
        """
        threshold = self._slow_threshold_ms(step)
        if took_ms <= threshold:
            return
        record = RecoveryRecord(
            at_step=step.id,
            condition="SlowResponse",
            action="extended_wait",
            duration_ms=took_ms,
            note=(
                f"step took {took_ms}ms against a recorded p50 of "
                f"{step.timing.observed_ms_p50}ms (threshold {threshold}ms)"
            ),
        )
        state.recoveries.append(record)
        log.event(
            "step.slow",
            step=step.id,
            elapsed_ms=took_ms,
            p50_ms=step.timing.observed_ms_p50,
            threshold_ms=threshold,
        )

    async def _rerun(self, capability, step_ids, params, state, log) -> bool:
        """Re-run a declared subset of steps, with no guards and no recursion.

        Used only by a `reauthenticate` recovery rule, which names its own
        steps. Deliberately lean: resolve, act, wait for the step's own
        postcondition. Re-entering the full loop here would mean a recovery
        that can itself escalate and recover, and bounding that honestly is
        harder than not allowing it.
        """
        for step_id in step_ids:
            step = capability.step(step_id)
            if step is None:
                log.event("reauth.unknown_step", step=step_id)
                return False

            observation = await self.surface.observe()
            resolution = await self.resolver.resolve(step, observation)
            if not resolution.ok and step.action in _NEEDS_ELEMENT:
                log.event("reauth.unresolved", step=step_id, observed=resolution.observed)
                return False

            result = await self.surface.act(
                self._build_action(step, params), resolution.handle
            )
            if not result.ok:
                log.event("reauth.action_failed", step=step_id, detail=result.detail)
                return False

            deadline = time.perf_counter() + step.timing.timeout_ms / 1000
            while True:
                observation = await self.surface.observe()
                verdict = await self.evaluator.check(
                    step.postcondition, observation, params=params,
                    step=step, secrets=self.secrets,
                )
                if verdict.ok:
                    break
                if time.perf_counter() >= deadline:
                    log.event("reauth.checkpoint_failed", step=step_id,
                              expected=verdict.expected, observed=verdict.observed)
                    return False
                await asyncio.sleep(POLL_INTERVAL_S)

        log.event("reauth.completed", steps=list(step_ids))
        return True

    async def _assisted_resolve(self, step, observation, state, log):
        """One bounded model call to re-identify a single element.

        Returns a resolution, or None to fail as normal. A success is still
        reported as drift: a locator the model kept alive should appear in
        telemetry rather than quietly work forever.
        """
        if self.fallback is None or not self.fallback.available:
            return None

        candidate = await self.fallback.reidentify(step, observation)
        if candidate is None:
            return None

        matches = await self.surface.find(candidate, step.frame_path)
        if len(matches) != 1:
            log.event(
                "fallback.unresolved",
                step=step.id,
                matched=len(matches),
                note="the re-identified locator was not unique",
            )
            return None

        state.drift.append(
            DriftSignal(
                at_step=step.id,
                expected_strategy=step.target.recorded.winning_strategy,
                winning_strategy=candidate.strategy,
                matches=1,
                note=(
                    "resolved by assisted fallback, not by the recorded ladder; "
                    "the artifact needs re-recording"
                ),
            )
        )
        log.event(
            "fallback.resolved",
            step=step.id,
            strategy=candidate.strategy.value,
            name=candidate.name,
        )
        from .resolver import Resolution

        return Resolution(handle=matches[0])

    async def _classify_action_failure(self, result) -> FailureKind:
        """An action that would not run is not automatically a broken surface.

        SURFACE_ERROR means the browser or transport is gone. A dropdown whose
        option does not exist, or a control that never became actionable, is a
        timeout against a perfectly healthy page — and telling an operator the
        surface crashed sends them to debug the wrong thing.
        """
        detail = (result.detail or "").lower()
        if "timeout" in detail:
            return FailureKind.TIMEOUT
        try:
            await self.surface.observe()
        except Exception:
            return FailureKind.SURFACE_ERROR
        return FailureKind.UNRECOVERABLE_CONDITION

    async def _backoff(self, attempt, step, state, log, reason: str) -> None:
        delay = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S)) - 1]
        state.recoveries.append(
            RecoveryRecord(
                at_step=step.id,
                condition="TransientFailure",
                action="retry_step",
                attempt=attempt,
                duration_ms=int(delay * 1000),
                note=reason,
            )
        )
        log.event("step.retry", step=step.id, attempt=attempt, reason=reason, delay_s=delay)
        await asyncio.sleep(delay)

    async def _escalate(self, step, finding, observation, state, log) -> EscalationDecision:
        parked_from = time.perf_counter()
        state.escalation_count += 1
        log.event(
            "escalation.raised",
            step=step.id, reason=finding.kind.value, count=state.escalation_count,
        )
        await self.evidence.step_capture(self.surface, f"{step.id}-escalation", force=True)
        decision = await self.escalator.escalate(
            EscalationContext(
                step=step, finding=finding, observation=observation,
                escalation_count=state.escalation_count,
            )
        )
        if decision.record is not None:
            state.escalations.append(decision.record)
        state.escalated_s += time.perf_counter() - parked_from
        log.event(
            "escalation.resolved",
            step=step.id,
            resumed=decision.resumed,
            note=decision.note,
            parked_s=round(state.escalated_s, 1),
        )
        return decision

    # -------------------------------------------------------------- binding

    def _validate_contract(
        self, capability: Capability, params: Mapping[str, Any]
    ) -> tuple[str, str] | None:
        for spec in capability.contract.inputs:
            if spec.name not in params:
                if spec.required:
                    return (f"required input {spec.name!r}", "not supplied")
                continue
            value = str(params[spec.name])
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                shown = "<redacted>" if spec.sensitivity.restricted else repr(value)
                return (f"{spec.name} matching {spec.pattern!r}", f"{spec.name}={shown}")
        declared = capability.contract.input_names
        if extra := set(params) - declared:
            return ("only declared inputs", f"unexpected: {sorted(extra)}")

        for ref_name in self._secret_refs(capability):
            if ref_name not in self.secrets:
                return (f"environment variable {ref_name}", "not set")
        return None

    @staticmethod
    def _secret_refs(capability: Capability) -> set[str]:
        return {
            s.value.secret_ref
            for s in capability.steps
            if s.value is not None and s.value.secret_ref is not None
        }

    def _resolve_value(self, ref: ValueRef, params: Mapping[str, Any]) -> str:
        if ref.secret_ref is not None:
            return self.secrets[ref.secret_ref]
        if ref.param is not None:
            return str(params[ref.param])
        return interpolate(ref.literal or "", params)

    def _build_action(self, step: Step, params: Mapping[str, Any]) -> Action:
        value = self._resolve_value(step.value, params) if step.value else None
        sensitive = bool(step.value and step.value.sensitivity.restricted)
        return Action(
            type=step.action,
            step_id=step.id,
            intent=step.intent,
            risk=step.risk,
            url=value if step.action is ActionType.NAVIGATE else None,
            text=value if step.action in (ActionType.TYPE, ActionType.SELECT) else None,
            option=value if step.action is ActionType.SELECT else None,
            extract=(step.output.extract if step.output else None),
            sensitive=sensitive,
            timeout_ms=step.timing.timeout_ms,
        )

    def _loggable_value(self, step, action, params) -> str | None:
        if step.value is None:
            return None
        if step.value.sensitivity.restricted:
            return "<redacted>"
        return action.url or action.text or action.option

    def _bind_output(self, capability, step, raw, state, log) -> None:
        binding = step.output
        value = extract_value(raw, binding.pattern, binding.transform)
        declared = next(
            (f for f in capability.contract.outputs if f.name == binding.name), None
        )
        coerced = coerce(value, declared.type if declared else ParamType.STRING)
        state.outputs[binding.name] = coerced
        if declared is not None and declared.sensitivity.restricted:
            # The screen is now displaying regulated data. Driven by the
            # contract rather than by scanning pixels for something that
            # looks like money.
            self.evidence.mark_restricted(
                f"output {binding.name!r} is {declared.sensitivity.value}"
            )
        log.event(
            "step.read",
            step=step.id,
            output=binding.name,
            sensitivity=(declared.sensitivity.value if declared else "unknown"),
            value=(
                "<redacted>"
                if declared is not None and declared.sensitivity.restricted
                else coerced
            ),
        )

    async def _resolve_for_condition(self, step: Step) -> Handle | None:
        resolution = await self.resolver.resolve(step, await self.surface.observe())
        return resolution.handle

    @staticmethod
    def _retryable(step: Step) -> bool:
        """Only safe, reversible steps are retried.

        Retrying a state-changing click is how one confirmation becomes two
        accounts. The bound is on the *class of action*, not on a guess about
        whether the last attempt took effect.
        """
        return step.risk is Risk.SAFE_REVERSIBLE

    # -------------------------------------------------------------- finishing

    def _fail(self, capability, state, kind, at_step, expected, observed) -> Failure:
        return Failure(
            capability_id=capability.id,
            capability_version=capability.version,
            run_id=state.run_id,
            evidence_ref=self.run_dir,
            kind=kind,
            at_step=at_step,
            expected=expected,
            observed=observed,
            steps_executed=state.steps_executed,
            recoveries=state.recoveries,
            drift=state.drift,
            escalations=state.escalations,
        )

    async def _finish(
        self, result, capability, state, params, started_at, started, log
    ) -> ReplayResult:
        result = result.model_copy(
            update={"duration_ms": int((time.perf_counter() - started) * 1000)}
        )
        self.evidence.write_result(result, capability)
        write_manifest(
            self.run_dir,
            capability=capability,
            run_id=state.run_id,
            params=params,
            started_at=started_at,
            finished_at=utcnow(),
            status=result.status,
            step_timings=state.step_timings,
            extra={
                "recoveries": len(state.recoveries),
                "drift_signals": len(state.drift),
                "escalations": len(state.escalations),
                "assisted_fallback": (
                    self.fallback.as_evidence() if self.fallback is not None else []
                ),
                "restricted_captures": {
                    "reason": self.evidence.restricted_reason,
                    "files": self.evidence.restricted_captures,
                    "committed": False,
                },
            },
        )
        log.event(
            "run.finish",
            status=result.status,
            steps_executed=state.steps_executed,
            duration_ms=result.duration_ms,
            recoveries=len(state.recoveries),
            drift=len(state.drift),
        )
        if self._log is None:
            log.close()
        return result


_NEEDS_ELEMENT = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.READ}

_MONEY = re.compile(r"-?[\d,]+(?:\.\d+)?")


def state_step(state: RunState, capability: Capability) -> str:
    index = min(state.steps_executed, len(capability.steps) - 1)
    return capability.steps[index].id


def extract_value(raw: str | None, pattern: str | None, transform: Transform) -> str:
    text = raw or ""
    if pattern:
        match = re.search(pattern, text)
        text = match.group(1) if match else ""
    if transform is Transform.STRIP:
        return text.strip()
    if transform is Transform.UPPER:
        return text.strip().upper()
    if transform is Transform.DIGITS:
        return re.sub(r"\D", "", text)
    if transform is Transform.MONEY:
        match = _MONEY.search(text)
        return match.group(0).replace(",", "") if match else ""
    return text


def coerce(value: str, param_type: ParamType) -> Any:
    if param_type is ParamType.INTEGER:
        return int(float(value)) if value else 0
    if param_type is ParamType.NUMBER:
        return float(value) if value else 0.0
    if param_type is ParamType.BOOLEAN:
        return value.strip().lower() in {"true", "yes", "1", "y"}
    return value


__all__ = [
    "EscalationContext",
    "EscalationDecision",
    "Escalator",
    "NoEscalation",
    "ReplayExecutor",
    "coerce",
    "extract_value",
]
