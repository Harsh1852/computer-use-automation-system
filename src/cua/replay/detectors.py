"""The standing detector set.

Detection is not a per-step check bolted onto the steps that happened to need
it. It is a fixed set of detectors run against *every* observation, before and
after every action. That matters because the conditions worth detecting —
a session dying, a maintenance modal, a 500 — do not announce themselves at
the step whose author anticipated them.

Precedence is the load-bearing part. `DeclaredOutcomeDetector` runs first, so
a state the artifact declares as a legitimate business answer is reported as
one even when it also happens to be an HTTP 403 with an angry red banner.
Conflating those two is the single most common design mistake in this problem,
and ordering is how the code refuses to make it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from ..schema import BusinessOutcome, Observation, Step
from .conditions import ConditionEvaluator


class FindingKind(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"
    AUTH_WALL = "auth_wall"
    MODAL = "modal"
    HTTP_ERROR = "http_error"
    ERROR_BANNER = "error_banner"


@dataclass(frozen=True)
class Finding:
    detector: str
    kind: FindingKind
    message: str
    expected: str = ""
    observed: str = ""
    code: str | None = None
    terminal: bool = True


@dataclass
class DetectorConfig:
    """Text the detectors key off.

    Defaults describe this vendor product. In a real deployment these would be
    per-product configuration rather than constants — the point of keeping them
    in one object is that adding a product means adding data, not a detector.
    """

    login_url_globs: tuple[str, ...] = ("*/login*",)
    session_expired_text: tuple[str, ...] = ("YOUR SESSION HAS TIMED OUT",)
    error_patterns: tuple[str, ...] = (
        r"\bMUST BE AT LEAST\b",
        r"\bIS REQUIRED\b",
        r"\bSIGN-ON FAILED\b",
        r"\bServer Error\b",
    )


class Detector(Protocol):
    name: str

    async def scan(self, observation: Observation, ctx: "ScanContext") -> Finding | None: ...


@dataclass
class ScanContext:
    """What the detectors need to know about where the run currently is."""

    params: Mapping[str, Any] = field(default_factory=dict)
    step: Step | None = None
    authenticated: bool = False
    """Set once the run has been past the sign-on screen. Without it, the
    login page at step one looks identical to a session that just died."""


class DeclaredOutcomeDetector:
    """Matches the artifact's own `outcomes`. Runs first, always."""

    name = "DeclaredOutcomeDetector"

    def __init__(
        self, outcomes: Sequence[BusinessOutcome], evaluator: ConditionEvaluator
    ) -> None:
        self._outcomes = list(outcomes)
        self._evaluator = evaluator

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        for outcome in self._outcomes:
            verdict = await self._evaluator.check(
                outcome.detect, observation, params=ctx.params
            )
            if verdict.ok:
                return Finding(
                    detector=self.name,
                    kind=FindingKind.BUSINESS_OUTCOME,
                    message=outcome.description,
                    code=outcome.code,
                    expected="a declared business outcome",
                    observed=verdict.observed,
                    terminal=outcome.terminal,
                )
        return None


class AuthWallDetector:
    """The sign-on page reappeared mid-flow, i.e. the session died."""

    name = "AuthWallDetector"

    def __init__(self, config: DetectorConfig) -> None:
        self._config = config

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        text = " ".join(n.name or "" for n in observation.nodes)
        for marker in self._config.session_expired_text:
            if marker in text:
                return Finding(
                    detector=self.name,
                    kind=FindingKind.AUTH_WALL,
                    message=marker,
                    expected="an authenticated session",
                    observed=f"{marker!r} on screen",
                )

        if not ctx.authenticated:
            return None  # the login page is where a run legitimately starts

        import fnmatch

        for path, url in observation.frame_urls.items():
            if any(fnmatch.fnmatch(url, g) for g in self._config.login_url_globs):
                where = path or "top document"
                return Finding(
                    detector=self.name,
                    kind=FindingKind.AUTH_WALL,
                    message="sign-on page reappeared mid-flow",
                    expected="an authenticated session",
                    observed=f"{where} is at {url}",
                )
        return None


class ModalDetector:
    """An unexpected dialog or overlay is sitting on top of the content."""

    name = "ModalDetector"

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        if not observation.dialogs:
            return None
        dialog = observation.dialogs[0]
        return Finding(
            detector=self.name,
            kind=FindingKind.MODAL,
            message=(dialog.title or dialog.text)[:160],
            expected="no modal obscuring the screen",
            observed=f"dialog: {dialog.text[:160]!r}",
        )


class HttpErrorDetector:
    """A 4xx or 5xx response. Only reached if no declared outcome claimed it."""

    name = "HttpErrorDetector"

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        status = observation.http_status
        if status is None or status < 400:
            return None
        # Name the document that actually errored. In a frameset the shell
        # returns 200 while the content frame returns 500, and "status 500 at
        # <shell url>" sends whoever is debugging to the wrong page.
        culprits = [
            f"{path or 'top document'} at {observation.frame_urls.get(path, '?')}"
            for path, code in observation.frame_statuses.items()
            if code >= 400
        ]
        where = "; ".join(culprits) or observation.url
        return Finding(
            detector=self.name,
            kind=FindingKind.HTTP_ERROR,
            message=f"HTTP {status}",
            expected="http status <= 399",
            observed=f"status {status} in {where}",
        )


class ErrorBannerDetector:
    """Configured text patterns anywhere in the observed text."""

    name = "ErrorBannerDetector"

    def __init__(self, config: DetectorConfig) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in config.error_patterns]

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        haystack = " │ ".join(
            [n.name or "" for n in observation.nodes]
            + [b.text for b in observation.banners]
        )
        for pattern in self._patterns:
            if match := pattern.search(haystack):
                window = haystack[max(0, match.start() - 60) : match.end() + 60]
                return Finding(
                    detector=self.name,
                    kind=FindingKind.ERROR_BANNER,
                    message=match.group(0),
                    expected="no error banner",
                    observed=window.strip(),
                )
        return None


class DetectorSet:
    """Runs every detector in precedence order and returns the first finding.

    Precedence, highest first:

    1. a declared business outcome — the artifact says this is an answer
    2. an auth wall — the session died, which is human-fixable
    3. a modal — possibly recoverable by a declared rule
    4. an HTTP error — hard failure
    5. an error banner — hard failure
    """

    def __init__(
        self,
        outcomes: Sequence[BusinessOutcome],
        evaluator: ConditionEvaluator,
        config: DetectorConfig | None = None,
    ) -> None:
        config = config or DetectorConfig()
        self.detectors: list[Detector] = [
            DeclaredOutcomeDetector(outcomes, evaluator),
            AuthWallDetector(config),
            ModalDetector(),
            HttpErrorDetector(),
            ErrorBannerDetector(config),
        ]

    async def scan(self, observation: Observation, ctx: ScanContext) -> Finding | None:
        for detector in self.detectors:
            if finding := await detector.scan(observation, ctx):
                return finding
        return None

    async def scan_all(
        self, observation: Observation, ctx: ScanContext
    ) -> list[Finding]:
        found = []
        for detector in self.detectors:
            if finding := await detector.scan(observation, ctx):
                found.append(finding)
        return found
