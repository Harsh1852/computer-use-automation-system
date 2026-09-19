"""The genuine model run. Skipped without `OPENAI_API_KEY`.

Marked `llm` and excluded from `make test`, because a test suite that
silently spends money on every run is a bad test suite. This is the one
thing in the project that cannot be faked, so it is also the one thing worth
paying for deliberately.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from cua.evidence import RunLog
from cua.schema import Capability, Success, SurfaceKind

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY"
    ),
    pytest.mark.skipif(
        not os.environ.get("OPENAI_MODEL"), reason="needs OPENAI_MODEL"
    ),
]

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")
GOAL = "Look up member 10001 and read their current savings balance."


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


async def test_a_real_model_run_produces_a_replayable_capability(tmp_path):
    from cua.discovery import DiscoveryAgent, Recorder, outcomes_for, recoveries_for
    from cua.replay import ReplayExecutor
    from cua.surface import registry

    admin("/admin/reset")
    run_dir = Path(tmp_path) / "discovery"
    surface = registry.create(
        SurfaceKind.LEGACY_WEB, entry_url=f"{APP}/login", run_dir=str(run_dir)
    )
    await surface.start()
    log = RunLog(run_dir, "llm")
    try:
        recorder = Recorder(
            surface,
            goal=GOAL,
            entry_url=f"{APP}/login",
            tenant_id="meridian",
            vendor_product="MERIDIAN CoreBank",
            product_version="7.4.11",
            surface_kind=SurfaceKind.LEGACY_WEB,
        )
        agent = DiscoveryAgent(
            surface, recorder, goal=GOAL, entry_url=f"{APP}/login",
            run_dir=run_dir, log=log,
        )
        result = await agent.run()
        assert result.ok, f"the agent did not finish: {result.status} {result.reason}"

        capability = recorder.build_capability(
            capability_id="llm_lookup_balance",
            proposal=result.proposal,
            run_id=result.transcript_sha256[:12],
            model=agent.model,
            transcript_sha256=result.transcript_sha256,
            known_outcomes=outcomes_for("MERIDIAN CoreBank", recorder._content_frame()),
            known_recoveries=recoveries_for("MERIDIAN CoreBank"),
        )
    finally:
        log.close()
        await surface.close()

    # The model must not have written a selector anywhere.
    serialised = capability.model_dump_json()
    assert os.environ.get("APP_PASSWORD", "demo1234") not in serialised
    assert capability.approval.state.value == "draft"
    assert capability.contract.outputs, "the run declared no outputs"

    # And the recording must replay, with the model out of the loop.
    admin("/admin/reset")
    replay_dir = str(Path(tmp_path) / "replay")
    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        run_dir=replay_dir,
    )
    await surface.start()
    log = RunLog(replay_dir, "replay")
    try:
        params = {i.name: i.example or "10001" for i in capability.contract.inputs}
        replayed = await ReplayExecutor(surface, run_dir=replay_dir, log=log).run(
            capability, params
        )
    finally:
        log.close()
        await surface.close()

    assert isinstance(replayed, Success), replayed
    assert replayed.outputs

    # Round-trips through JSON, because the artifact is stored as a file.
    assert Capability.model_validate_json(serialised) == capability
