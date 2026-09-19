"""Human-in-the-loop escalation and control transfer.

The seam: automation must be able to pause, cede control of the *same live
session*, and resume — and there must be one unambiguous answer to "who is
driving". That answer is the lease, and `Surface.act()` asks it before every
action, so a violation is an exception rather than a race.
"""

from .human_capture import HumanCapture, HumanStep
from .intervention import (
    DEFAULT_INTERVENTION_TIMEOUT_S,
    InterventionReason,
    InterventionRequest,
    InterventionStore,
    OperatorEscalator,
)
from .lease import (
    AUTOMATION,
    OPERATOR,
    Handback,
    LeaseManager,
    LeaseViolation,
    SessionLease,
)

__all__ = [
    "AUTOMATION",
    "DEFAULT_INTERVENTION_TIMEOUT_S",
    "Handback",
    "HumanCapture",
    "HumanStep",
    "InterventionReason",
    "InterventionRequest",
    "InterventionStore",
    "LeaseManager",
    "LeaseViolation",
    "OPERATOR",
    "OperatorEscalator",
    "SessionLease",
]
