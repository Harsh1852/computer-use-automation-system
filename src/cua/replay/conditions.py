"""Evaluating the artifact's condition language against a live observation.

One evaluator serves three callers — step postconditions, business-outcome
detectors and recovery-rule triggers — so "how do I know I got there" is
defined once. Anything expressible as a checkpoint is therefore also
expressible as an outcome detector, which is why declaring "no such member"
costs nothing extra.

Every evaluation returns a `Verdict` carrying *expected* and *observed*
strings. A condition that only returns a boolean produces failures a human
cannot debug, and `Failure.expected` / `Failure.observed` are contractual.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..schema import (
    AllOf,
    AnyOf,
    Condition,
    HttpOk,
    NodeAbsent,
    NodePresent,
    Observation,
    Sensitivity,
    Step,
    TextAbsent,
    TextPresent,
    UrlMatches,
    ValueEquals,
    ValueRef,
)

PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    expected: str
    observed: str

    def __bool__(self) -> bool:
        return self.ok


def interpolate(template: str, params: Mapping[str, Any]) -> str:
    """Substitute ``{param}`` placeholders. Unknown names are left intact so
    the failure message shows the template rather than a crash."""
    return PLACEHOLDER.sub(
        lambda m: str(params.get(m.group(1), m.group(0))), template
    )


def _scope(observation: Observation, frame_path: list[str]):
    if not frame_path:
        return observation.nodes
    return [n for n in observation.nodes if n.frame_path == frame_path]


def _texts(observation: Observation, frame_path: list[str]) -> str:
    parts = [n.name or "" for n in _scope(observation, frame_path)]
    parts += [n.value or "" for n in _scope(observation, frame_path)]
    parts += [d.text for d in observation.dialogs]
    parts += [b.text for b in observation.banners]
    return " │ ".join(p for p in parts if p)


def _frame_url(observation: Observation, frame_path: list[str]) -> str:
    return observation.frame_urls.get("/".join(frame_path), observation.url)


def _where(frame_path: list[str]) -> str:
    return f" in frame {'/'.join(frame_path)}" if frame_path else ""


class ConditionEvaluator:
    """Evaluates conditions. Async because `value_equals` re-resolves a target.

    `value_equals` deliberately re-locates the step's element in the *current*
    observation rather than trusting the handle it typed into: the point of a
    postcondition is to check the application's state, not to replay our own
    belief about it.
    """

    def __init__(self, resolve_target=None) -> None:
        self._resolve_target = resolve_target

    async def check(
        self,
        condition: Condition,
        observation: Observation,
        *,
        params: Mapping[str, Any],
        step: Step | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> Verdict:
        kind = condition.kind

        if kind == "all_of":
            return await self._all_of(condition, observation, params, step, secrets)
        if kind == "any_of":
            return await self._any_of(condition, observation, params, step, secrets)
        if kind in ("text_present", "text_absent"):
            return self._text(condition, observation, params)
        if kind == "url_matches":
            return self._url(condition, observation, params)
        if kind in ("node_present", "node_absent"):
            return self._node(condition, observation)
        if kind == "http_ok":
            return self._http(condition, observation)
        if kind == "value_equals":
            return await self._value(condition, observation, params, step, secrets)
        raise ValueError(f"unhandled condition kind {kind!r}")  # pragma: no cover

    # ------------------------------------------------------------- combinators

    async def _all_of(self, cond: AllOf, obs, params, step, secrets) -> Verdict:
        for child in cond.conditions:
            verdict = await self.check(
                child, obs, params=params, step=step, secrets=secrets
            )
            if not verdict.ok:
                return Verdict(False, f"all of: {verdict.expected}", verdict.observed)
        return Verdict(True, "all conditions held", "all conditions held")

    async def _any_of(self, cond: AnyOf, obs, params, step, secrets) -> Verdict:
        observed = []
        for child in cond.conditions:
            verdict = await self.check(
                child, obs, params=params, step=step, secrets=secrets
            )
            if verdict.ok:
                return verdict
            observed.append(verdict.observed)
        return Verdict(False, "any of the declared conditions", "; ".join(observed))

    # ------------------------------------------------------------------ leaves

    def _text(self, cond: TextPresent | TextAbsent, obs, params) -> Verdict:
        wanted = interpolate(cond.text, params)
        haystack = _texts(obs, cond.frame_path)
        found = (
            wanted in haystack
            if cond.case_sensitive
            else wanted.casefold() in haystack.casefold()
        )
        want_present = cond.kind == "text_present"
        expected = f"text {'present' if want_present else 'absent'}: {wanted!r}{_where(cond.frame_path)}"
        if found == want_present:
            return Verdict(True, expected, f"{wanted!r} {'found' if found else 'absent'}")
        return Verdict(False, expected, f"{wanted!r} {'found' if found else 'not found'}")

    def _url(self, cond: UrlMatches, obs, params) -> Verdict:
        # fnmatchcase, not fnmatch: fnmatch normalises case through
        # os.path.normcase, so the same artifact would match differently on a
        # Windows runner than on Linux. A checkpoint may not depend on that.
        pattern = interpolate(cond.pattern, params)
        actual = _frame_url(obs, cond.frame_path)
        ok = fnmatch.fnmatchcase(actual, pattern)
        where = _where(cond.frame_path) or " in the top document"
        return Verdict(
            ok, f"url matches {pattern!r}{where}", f"url is {actual!r}"
        )

    def _node(self, cond: NodePresent | NodeAbsent, obs) -> Verdict:
        matches = [
            n
            for n in _scope(obs, cond.frame_path)
            if n.role == cond.role and (cond.name is None or n.name == cond.name)
        ]
        want_present = cond.kind == "node_present"
        label = f"{cond.role} {cond.name!r}" if cond.name else cond.role
        expected = f"{label} {'present' if want_present else 'absent'}{_where(cond.frame_path)}"
        return Verdict(
            bool(matches) == want_present, expected, f"{len(matches)} matching node(s)"
        )

    def _http(self, cond: HttpOk, obs) -> Verdict:
        status = obs.http_status
        ok = status is None or status <= cond.at_most
        return Verdict(ok, f"http status <= {cond.at_most}", f"status {status}")

    async def _value(self, cond: ValueEquals, obs, params, step, secrets) -> Verdict:
        wanted, redacted = self._expected_value(cond.expected, params, secrets)
        if step is None or step.target is None or self._resolve_target is None:
            return Verdict(False, f"value == {redacted}", "no target to read back")

        handle = await self._resolve_target(step)
        if handle is None or handle.ref is None:
            return Verdict(False, f"value == {redacted}", "target could not be re-resolved")

        node = obs.by_ref(handle.ref)
        actual = (node.value if node else None) or ""
        if wanted is None:
            return Verdict(False, f"value == {redacted}", "expected value unavailable")

        ok = actual == wanted
        # A secret's *actual* value must never reach a log line or a result,
        # so the observed side is redacted too when the comparison is secret.
        shown = "<redacted>" if redacted == "<redacted>" else repr(actual)
        return Verdict(ok, f"value == {redacted}", f"value is {shown}")

    @staticmethod
    def _expected_value(
        ref: ValueRef, params: Mapping[str, Any], secrets: Mapping[str, str] | None
    ) -> tuple[str | None, str]:
        if ref.secret_ref is not None:
            value = (secrets or {}).get(ref.secret_ref)
            return value, "<redacted>"
        if ref.param is not None:
            value = params.get(ref.param)
            shown = "<redacted>" if ref.sensitivity.restricted else repr(value)
            return (None if value is None else str(value)), shown
        literal = interpolate(ref.literal or "", params)
        return literal, repr(literal)


def describe(condition: Condition, params: Mapping[str, Any]) -> str:
    """One-line human rendering, for logs and intervention requests."""
    kind = condition.kind
    if kind in ("text_present", "text_absent"):
        return f"{kind} {interpolate(condition.text, params)!r}"
    if kind == "url_matches":
        return f"url_matches {interpolate(condition.pattern, params)!r}"
    if kind in ("node_present", "node_absent"):
        return f"{kind} {condition.role} {condition.name!r}"
    if kind == "http_ok":
        return f"http_ok <= {condition.at_most}"
    if kind == "value_equals":
        return "value_equals"
    if kind in ("all_of", "any_of"):
        return f"{kind}({', '.join(describe(c, params) for c in condition.conditions)})"
    return kind  # pragma: no cover


def sensitivity_of(ref: ValueRef) -> Sensitivity:
    return ref.sensitivity
