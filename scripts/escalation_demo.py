"""The escalation demo, end to end, with a scripted operator.

Reproducible version of the acceptance walkthrough:

    1. replay `lookup_member_balance`
    2. the session dies mid-run (`session_expired` injected before s4)
    3. the executor detects the auth wall, pauses and raises an intervention
    4. an operator takes the lease, signs back in on the *same live browser*,
       and releases
    5. the executor resumes and returns Success
    6. `evidence/replay-escalation/` shows one interleaved timeline

The operator here is a script so the demo runs unattended. A person does the
identical thing through the console at http://localhost:8080 — the console's
buttons post to the same lease this script calls. Run with `--manual` to stop
after the intervention is raised and hand it to a human instead.

    make escalation-demo
    make escalation-demo MANUAL=1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

from cua.escalation import (
    HumanCapture,
    InterventionStore,
    LeaseManager,
    LeaseViolation,
    OperatorEscalator,
)
from cua.evidence import RunLog
from cua.replay import ReplayExecutor
from cua.schema import Action, ActionType, Capability, TargetCandidate, TargetStrategy
from cua.surface import registry

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")
ARTIFACT = Path("artifacts/lookup_member_balance.v1.json")
SESSION = "demo-session"


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


async def operator_signs_back_in(surface, token: str) -> None:
    """What the person does through noVNC, expressed through the lease.

    In a manual run this never executes: the human drives the browser
    directly and this API is not involved, which is the entire point of
    handing them the live session rather than a screenshot.
    """

    async def one(candidate):
        matches = await surface.find(candidate, [])
        if len(matches) != 1:
            raise RuntimeError(f"operator could not find {candidate.anchor or candidate.name}")
        return matches[0]

    await surface.act_as_operator(
        token, Action(type=ActionType.NAVIGATE, url=f"{APP}/login"), None
    )
    await surface.observe()
    await surface.act_as_operator(
        token,
        Action(type=ActionType.TYPE, text=os.environ.get("APP_USER", "svc_agent")),
        await one(TargetCandidate(strategy=TargetStrategy.NEAR_TEXT, role="textbox",
                                  anchor="USER ID", index=0)),
    )
    await surface.observe()
    await surface.act_as_operator(
        token,
        Action(type=ActionType.TYPE,
               text=os.environ.get("APP_PASSWORD", "demo1234"), sensitive=True),
        await one(TargetCandidate(strategy=TargetStrategy.NEAR_TEXT, role="textbox",
                                  anchor="PASSWORD", index=0)),
    )
    await surface.observe()
    await surface.act_as_operator(
        token,
        Action(type=ActionType.CLICK),
        await one(TargetCandidate(strategy=TargetStrategy.A11Y_ROLE_NAME,
                                  role="button", name="SIGN ON")),
    )


async def main(manual: bool, run_dir: str) -> int:
    capability = Capability.model_validate_json(ARTIFACT.read_text(encoding="utf-8"))
    params = {"member_id": "10001"}

    admin("/admin/reset")
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    lease = LeaseManager(SESSION, hold_seconds=1800)
    store = InterventionStore()
    log = RunLog(run_dir, "escalation-demo", also_stdout=True)
    human = HumanCapture(log, is_active=lambda: lease.held_by_operator)

    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        run_dir=run_dir,
        lease=lease,
    )
    await surface.start()

    from cua.cli import _serve_console

    console = await _serve_console(
        {"lease": lease, "store": store, "human": human}, 8080
    )
    print(f"\noperator console  http://localhost:8080/session/{SESSION}\n")

    executor = ReplayExecutor(
        surface,
        run_dir=run_dir,
        log=log,
        escalator=OperatorEscalator(
            lease=lease, store=store, surface=surface, capability=capability,
            params=params, log=log, human=human,
            timeout_s=3600 if manual else 120,
        ),
    )

    # Arm the fault so the session dies as the console loads, at s4.
    admin("/admin/inject", {"fault": "session_expired", "once": True})
    run = asyncio.get_running_loop().create_task(executor.run(capability, params))

    while store.by_session(SESSION) is None and not run.done():
        await asyncio.sleep(0.2)

    if manual:
        print("intervention raised. Take control in the console, sign back in")
        print("through noVNC, then press Release. Waiting...\n")
    else:
        request = store.by_session(SESSION)
        print(f"\nintervention {request.id}: {request.observed}")
        token = lease.take("scripted operator")
        try:
            await surface.act(Action(type=ActionType.NAVIGATE, url=APP), None)
            raise SystemExit("automation acted while the operator held the lease")
        except LeaseViolation:
            print("automation is correctly locked out while the operator holds it")
        await operator_signs_back_in(surface, token)
        lease.release(token, "signed back in after the session timed out")
        print("operator released the session\n")

    result = await run
    (Path(run_dir) / "human_steps.json").write_text(
        json.dumps(human.as_timeline(), indent=2), encoding="utf-8"
    )
    await console.shutdown()
    log.close()
    await surface.close()

    print()
    print(json.dumps(json.loads(result.model_dump_json()), indent=2))
    print(f"\nstatus   {result.status.upper()}")
    print(f"evidence {run_dir}/")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true", help="wait for a real person")
    parser.add_argument("--evidence", default="evidence/replay-escalation")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.manual, args.evidence)))
