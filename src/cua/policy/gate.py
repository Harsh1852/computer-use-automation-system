"""The single enforcement point.

`PolicyGate.check()` is called from exactly one place in the system:
`Surface.act()`. Nothing else enforces anything. That is the whole design —
a longer rule list spread over three call sites has three places to forget,
and the claim "the automation cannot reach /admin" is only as good as the
number of places it has to be true.

Two things the gate deliberately does *not* do:

* It does not trust the artifact's own risk label. An artifact is data; a
  recording that under-declared a step should still be caught. The gate
  re-derives risk from the policy's route and label rules and takes the
  higher of the two.
* It does not know about modes beyond discovery and replay. The asymmetry
  between them is expressed in `policy.yaml`, not in branching here.
"""

from __future__ import annotations

import fnmatch
from enum import Enum
from typing import Any, Mapping

from ..schema import Action, ActionType, Risk
from ..surface.base import PolicyViolation
from .config import Policy, load_policy

_RISK_ORDER = {Risk.SAFE_REVERSIBLE: 0, Risk.STATE_CHANGING: 1, Risk.IRREVERSIBLE: 2}


class Mode(str, Enum):
    DISCOVERY = "discovery"
    REPLAY = "replay"


class PolicyGate:
    """Decides whether one action may run, and says which rule refused it."""

    def __init__(
        self,
        policy: Policy | None = None,
        *,
        mode: Mode = Mode.REPLAY,
        artifact_approved: bool = False,
    ) -> None:
        self.policy = policy or load_policy()
        self.mode = mode
        self.artifact_approved = artifact_approved

    # ------------------------------------------------------------ the check

    def check(self, action: Action, context: Mapping[str, Any]) -> None:
        self._check_action_type(action)
        self._check_urls(action, context)
        self._check_risk(action, context)

    def _check_action_type(self, action: Action) -> None:
        permitted = self.policy.allowlist.action_types
        if permitted and action.type not in permitted:
            raise PolicyViolation(
                f"action type {action.type.value!r} is not permitted",
                rule="allowlist.action_types",
            )

    def _check_urls(self, action: Action, context: Mapping[str, Any]) -> None:
        """Destination for a navigate; current location for everything else.

        Checking the current location matters: if the application redirects
        somewhere unexpected, every subsequent click is happening on a page
        the allowlist never sanctioned, and the run should stop rather than
        keep typing into it.
        """
        if action.type is ActionType.NAVIGATE and action.url:
            self._assert_permitted(action.url, "navigate to")
            return

        current = str(context.get("url") or "")
        if current and not current.startswith("about:"):
            self._assert_permitted(current, "act on")

    def _assert_permitted(self, url: str, verb: str) -> None:
        allowlist = self.policy.allowlist
        # Denials are checked first and independently: a broader allow
        # pattern must not be able to re-open something explicitly closed.
        for pattern in allowlist.denied_patterns:
            if _matches(url, pattern):
                raise PolicyViolation(
                    f"refusing to {verb} {url}: matches denied pattern {pattern!r}",
                    rule="allowlist.denied_patterns",
                )
        if allowlist.url_patterns and not any(
            _matches(url, p) for p in allowlist.url_patterns
        ):
            raise PolicyViolation(
                f"refusing to {verb} {url}: outside the allowlist",
                rule="allowlist.url_patterns",
            )

    def _check_risk(self, action: Action, context: Mapping[str, Any]) -> None:
        risk = self.effective_risk(action, context)
        rules = getattr(self.policy.enforcement, self.mode.value)
        disposition = {
            Risk.IRREVERSIBLE: rules.irreversible,
            Risk.STATE_CHANGING: rules.state_changing,
            Risk.SAFE_REVERSIBLE: rules.safe_reversible,
        }[risk]

        if disposition == "allow":
            return
        if disposition == "block":
            raise PolicyViolation(
                f"{self.mode.value} may not perform {risk.value} actions "
                f"({action.intent or action.type.value}); it must stop and escalate "
                "for approval",
                rule=f"enforcement.{self.mode.value}.{risk.value}",
            )
        if disposition == "require_approved_artifact" and not self.artifact_approved:
            raise PolicyViolation(
                f"{risk.value} step requires an approved capability; this artifact "
                "is still a draft",
                rule=f"enforcement.{self.mode.value}.{risk.value}",
            )

    # -------------------------------------------------------- risk grading

    def effective_risk(self, action: Action, context: Mapping[str, Any]) -> Risk:
        """The higher of what the artifact declared and what policy derives."""
        derived = self.derived_risk(action, context)
        return max((action.risk, derived), key=lambda r: _RISK_ORDER[r])

    def derived_risk(self, action: Action, context: Mapping[str, Any]) -> Risk:
        rules = self.policy.risk_rules
        label = str(context.get("label") or "").strip().upper()
        if label and label in rules.labels:
            return Risk.IRREVERSIBLE

        candidates = [str(context.get("url") or "")]
        if action.url:
            candidates.append(action.url)
        for url in candidates:
            if url and any(_matches(url, r) for r in rules.irreversible_routes):
                return Risk.IRREVERSIBLE
        return Risk.SAFE_REVERSIBLE

    # ------------------------------------------------------------- queries

    @property
    def reauth_allowed(self) -> bool:
        return self.policy.enforcement.reauth_allowed

    def describe(self) -> str:
        return (
            f"mode={self.mode.value} approved={self.artifact_approved} "
            f"allow={len(self.policy.allowlist.url_patterns)} "
            f"deny={len(self.policy.allowlist.denied_patterns)}"
        )


def _matches(url: str, pattern: str) -> bool:
    """Glob match, with `**` meaning "any number of path segments".

    `fnmatch` treats `*` as matching separators too, which would make
    `http://app:5000/members/**` accidentally match a different host. The
    pattern is anchored and `**` expanded explicitly so a rule means what it
    looks like it means.
    """
    if fnmatch.fnmatchcase(url, pattern):
        return True
    if "**" in pattern:
        head, _, tail = pattern.partition("**")
        return url.startswith(head.rstrip("*")) and (
            not tail or fnmatch.fnmatchcase(url, f"*{tail}")
        )
    return False
