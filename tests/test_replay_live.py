"""The four replay paths, end to end against the running application.

This is the acceptance evidence for deterministic replay: one artifact, no
model in the loop, and four different classes of answer depending only on
what the application did.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from cua.evidence import RunLog
from cua.replay import ReplayExecutor
from cua.schema import BusinessOutcomeResult, Capability, Failure, FailureKind, Success

pytestmark = pytest.mark.integration

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")
ARTIFACT = Path("artifacts/lookup_member_balance.v1.json")


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


@pytest.fixture
def capability() -> Capability:
    return Capability.model_validate_json(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture
async def replay(tmp_path, capability):
    pytest.importorskip("playwright")
    from cua.surface import registry

    admin("/admin/reset")

    async def run(params: dict, *, fault: str | None = None):
        if fault:
            admin("/admin/inject", {"fault": fault, "once": True})
        run_dir = str(tmp_path / (fault or "clean"))
        surface = registry.create(
            capability.target.surface_kind,
            entry_url=capability.target.entry_point,
            run_dir=run_dir,
        )
        await surface.start()
        log = RunLog(run_dir, "test")
        try:
            executor = ReplayExecutor(surface, run_dir=run_dir, log=log)
            return await executor.run(capability, params), Path(run_dir)
        finally:
            log.close()
            await surface.close()

    yield run
    admin("/admin/reset")


# ------------------------------------------------------------------- paths


async def test_success_returns_the_declared_output(replay):
    result, run_dir = await replay({"member_id": "10001"})

    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "1234.56"}
    assert result.steps_executed == 7
    assert result.drift == []
    assert (run_dir / "run.jsonl").exists()
    assert (run_dir / "result.json").exists()
    assert (run_dir / "manifest.json").exists()


async def test_no_such_member_is_an_answer_not_a_crash(replay):
    result, _ = await replay({"member_id": "99999"})

    assert isinstance(result, BusinessOutcomeResult), result
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.at_step == "s6"
    assert result.terminal is True


async def test_a_403_declared_as_an_outcome_is_reported_as_one(replay):
    """Member 10003 is restricted. The page is an HTTP 403 *and* a declared
    business outcome; precedence decides which the caller is told about."""
    result, _ = await replay({"member_id": "10003"})

    assert isinstance(result, BusinessOutcomeResult), result
    assert result.code == "PERMISSION_DENIED"


async def test_a_transient_delay_is_absorbed_and_reported(replay):
    result, _ = await replay({"member_id": "10001"}, fault="slow")

    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "1234.56"}
    slow = [r for r in result.recoveries if r.condition == "SlowResponse"]
    assert slow, "the run absorbed six seconds and said nothing"
    assert slow[0].duration_ms >= 5_000


async def test_a_known_modal_is_dismissed_by_its_declared_rule(replay):
    result, _ = await replay({"member_id": "10001"}, fault="interstitial")

    assert isinstance(result, Success), result
    dismissals = [r for r in result.recoveries if r.rule_id == "dismiss_maintenance_notice"]
    assert dismissals and dismissals[0].action == "dismiss"


async def test_an_app_error_is_a_debuggable_hard_failure(replay):
    result, run_dir = await replay({"member_id": "10001"}, fault="app_error")

    assert isinstance(result, Failure), result
    assert result.kind is FailureKind.UNRECOVERABLE_CONDITION
    assert result.at_step == "s4"
    assert "500" in result.observed
    assert "contentFrame" in result.observed, "must name the frame that errored"
    assert list(run_dir.glob("screenshots/*.png")), "a failure needs a richer signal"


async def test_bad_input_fails_before_the_browser_does_anything(replay):
    result, _ = await replay({"member_id": "not-a-member"})

    assert isinstance(result, Failure), result
    assert result.kind is FailureKind.CONTRACT_VIOLATION
    assert result.steps_executed == 0
    assert "^[0-9]{5}$" in result.expected


# ---------------------------------------------------------------- evidence


async def test_evidence_redacts_by_declared_sensitivity(replay):
    result, run_dir = await replay({"member_id": "10001"})

    # The caller asked for the balance and gets it.
    assert result.outputs["savings_balance"] == "1234.56"

    # The committed evidence does not carry regulated data.
    written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert written["outputs"]["savings_balance"] == "<redacted>"

    log = (run_dir / "run.jsonl").read_text(encoding="utf-8")
    assert "1234.56" not in log
    assert os.environ.get("APP_PASSWORD", "demo1234") not in log


async def test_the_manifest_makes_the_run_self_describing(replay, capability):
    _, run_dir = await replay({"member_id": "10001"})
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["capability"]["id"] == capability.id
    assert manifest["capability"]["version"] == capability.version
    assert manifest["status"] == "success"
    assert manifest["params"] == {"member_id": "10001"}
    assert set(manifest["timings"]["steps_ms"]) == {f"s{i}" for i in range(1, 8)}


async def test_replay_is_deterministic_across_runs(replay):
    """Same artifact, same inputs, same steps, same outputs."""
    first, _ = await replay({"member_id": "10001"})
    second, _ = await replay({"member_id": "10001"})

    assert isinstance(first, Success) and isinstance(second, Success)
    assert first.outputs == second.outputs
    assert first.steps_executed == second.steps_executed
    assert [d.at_step for d in first.drift] == [d.at_step for d in second.drift]
