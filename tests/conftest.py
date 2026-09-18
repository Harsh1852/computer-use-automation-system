from __future__ import annotations

import pytest

from cua.schema import ActionType, Step, TextPresent, Timing


@pytest.fixture
def step_factory():
    """Build a valid Step without restating the required fields every time."""

    def make(step_id: str = "s1", **kw) -> Step:
        fields = dict(
            id=step_id,
            intent="do the thing",
            # `assert` is the only verb that needs neither a target nor a
            # value, so it is the honest default for a step stub.
            action=ActionType.ASSERT,
            postcondition=TextPresent(text="CURRENT BALANCE"),
            timing=Timing(observed_ms_p50=10, timeout_ms=5000),
        )
        fields.update(kw)
        return Step(**fields)

    return make
