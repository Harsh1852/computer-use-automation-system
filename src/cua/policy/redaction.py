"""Redaction at the sink.

A `Redactor` is a structlog processor sitting at the front of the chain, so
every log line passes through it whether or not the caller remembered. That
placement is the point: redaction that depends on each call site doing the
right thing is redaction that fails the first time somebody adds a log line
in a hurry.

Two mechanisms, because they fail differently:

* **Secret values** are scrubbed by exact substring. This catches a password
  wherever it appears, in any shape, including inside a URL or an exception
  message.
* **Patterns** catch regulated *shapes* — an SSN, a card, a long account
  number — that nobody registered as a secret because nobody knew they were
  about to be logged.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

SECRET_MASK = "<redacted:secret>"

MAX_DEPTH = 6
"""Recursion guard. A log payload deep enough to exceed this is a bug in the
caller, and silently walking it forever would be a worse one."""


class Redactor:
    """Scrubs secrets and regulated shapes out of anything on its way to disk."""

    def __init__(
        self,
        patterns: Mapping[str, str] | None = None,
        secret_values: Iterable[str] = (),
    ) -> None:
        self._patterns = {
            name: re.compile(expr) for name, expr in (patterns or {}).items()
        }
        # Longest first: scrubbing a short secret that is a prefix of a longer
        # one would leave the tail of the longer one in the log.
        self._secrets = sorted(
            {s for s in secret_values if s and len(s) >= 4}, key=len, reverse=True
        )

    # ------------------------------------------------------- the processor

    def __call__(self, _logger: Any, _method: str, event_dict: dict) -> dict:
        return {key: self.scrub(value) for key, value in event_dict.items()}

    def scrub(self, value: Any, _depth: int = 0) -> Any:
        if _depth > MAX_DEPTH:
            return value
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {k: self.scrub(v, _depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            scrubbed = [self.scrub(v, _depth + 1) for v in value]
            return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
        return value

    def scrub_text(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, SECRET_MASK)
        for name, pattern in self._patterns.items():
            text = pattern.sub(f"<redacted:{name}>", text)
        return text

    # ---------------------------------------------------------- inspection

    def finds_regulated_data(self, text: str) -> str | None:
        """Which pattern, if any, this text trips. Used to flag screenshots."""
        for name, pattern in self._patterns.items():
            if pattern.search(text):
                return name
        return None

    @property
    def secret_count(self) -> int:
        return len(self._secrets)


def default_redactor(environ: Mapping[str, str] | None = None) -> Redactor:
    """Built from `policy.yaml` plus the environment.

    Falls back to a redactor that still scrubs nothing-but-secrets if the
    policy file is missing, rather than to no redaction at all: an absent
    policy is a configuration error, not permission to log freely.
    """
    try:
        from .config import load_policy

        policy = load_policy()
        return Redactor(
            policy.redaction.patterns,
            policy.redaction.secret_values(dict(environ) if environ else None),
        )
    except Exception:
        import os

        source = dict(environ) if environ is not None else dict(os.environ)
        return Redactor(
            {},
            [source.get(n, "") for n in ("APP_PASSWORD", "APP_USER", "OPENAI_API_KEY")],
        )


def scrub_evidence(text: str) -> str:
    """Scrub anything on its way to a file in an evidence directory.

    The log sink was not the whole sink. An observation dump prints each
    node's *value*, so the accessibility snapshot taken right after a
    credential is typed contains that credential - and it reached disk
    without passing the log processor at all. Every evidence write path now
    goes through here, which is the only version of "redaction at the sink"
    that is actually true.
    """
    return default_redactor().scrub_text(text)
