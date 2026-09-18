"""Surface abstraction: the only package permitted to know about a driver.

Importing `cua.surface.base` pulls in no driver. `cua.surface.registry` does,
because creating a web surface requires one — which is why callers ask the
registry for a surface rather than constructing one.
"""

from .base import (
    Gate,
    Handle,
    LadderOutcome,
    Lease,
    LeaseViolation,
    OpenGate,
    PolicyViolation,
    Surface,
    SurfaceError,
    UnheldLease,
    render_table,
    walk_ladder,
)

__all__ = [
    "Gate",
    "Handle",
    "LadderOutcome",
    "Lease",
    "LeaseViolation",
    "OpenGate",
    "PolicyViolation",
    "Surface",
    "SurfaceError",
    "UnheldLease",
    "render_table",
    "walk_ladder",
]
