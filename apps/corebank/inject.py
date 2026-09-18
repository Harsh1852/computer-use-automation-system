"""Fault injection for the CoreBank demo app.

The spec describes faults as a flag "on the session". They are process-global
here instead, because faults are injected out-of-band (``make inject``, curl,
a test fixture) by a client that does not share a cookie jar with the browser
the automation is driving — a session-scoped flag would be unreachable from
the place that needs to set it. See DECISIONS.md.

Each fault is armed with an ``once`` flag: armed-once faults are consumed by
the first request they apply to, sticky faults persist until ``/admin/reset``.
"""

from __future__ import annotations

import threading

# Faults handled generically by the request hook, in the order they are checked.
GENERIC_FAULTS = ["session_expired", "app_error", "slow", "interstitial"]

# Faults handled at a specific point in a specific view.
TARGETED_FAULTS = ["not_found", "validation", "permission_denied"]

SUPPORTED_FAULTS = GENERIC_FAULTS + TARGETED_FAULTS

SLOW_SECONDS = 6


class FaultStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # fault name -> once flag
        self._armed: dict[str, bool] = {}

    def arm(self, fault: str, once: bool = True) -> None:
        if fault not in SUPPORTED_FAULTS:
            raise ValueError(f"unsupported fault: {fault}")
        with self._lock:
            self._armed[fault] = once

    def consume(self, fault: str) -> bool:
        """Return True if ``fault`` is armed, disarming it when armed once."""
        with self._lock:
            if fault not in self._armed:
                return False
            if self._armed[fault]:
                del self._armed[fault]
            return True

    def armed(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._armed)

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()


FAULTS = FaultStore()
