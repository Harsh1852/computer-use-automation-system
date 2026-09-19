"""Command line entry point.

Deliberately thin: it wires environment to objects and prints. No automation
logic lives here, and nothing here imports a driver — the surface registry
does that, which is how the seam stays checkable with a grep.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from .schema import SurfaceKind
from .surface import registry, render_table


def _app_url() -> str:
    return os.environ.get("APP_URL", "http://app:5000").rstrip("/")


async def sign_on(surface, base: str, tenant_prefix: str = "") -> None:
    """Get past the sign-on screen so the frameset is reachable.

    Hand-driven here rather than replayed, because replay is the next phase.
    It uses the same public surface API a capability would.
    """
    from .schema import Action, ActionType, TargetCandidate, TargetStrategy

    async def one(candidate: TargetCandidate, frame_path: list[str]):
        matches = await surface.find(candidate, frame_path)
        if len(matches) != 1:
            raise SystemExit(
                f"sign-on: expected 1 match for {candidate.strategy.value} "
                f"{candidate.anchor or candidate.name!r}, got {len(matches)}"
            )
        return matches[0]

    await surface.act(
        Action(type=ActionType.NAVIGATE, url=f"{base}{tenant_prefix}/login"), None
    )
    await surface.observe()

    user = await one(
        TargetCandidate(
            strategy=TargetStrategy.NEAR_TEXT, role="textbox", anchor="USER ID", index=0
        ),
        [],
    )
    await surface.act(
        Action(type=ActionType.TYPE, text=os.environ.get("APP_USER", "svc_agent")), user
    )

    await surface.observe()
    password = await one(
        TargetCandidate(
            strategy=TargetStrategy.NEAR_TEXT, role="textbox", anchor="PASSWORD", index=0
        ),
        [],
    )
    await surface.act(
        Action(
            type=ActionType.TYPE,
            text=os.environ.get("APP_PASSWORD", "demo1234"),
            sensitive=True,
        ),
        password,
    )

    await surface.observe()
    button = await one(
        TargetCandidate(
            strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SIGN ON"
        ),
        [],
    )
    await surface.act(Action(type=ActionType.CLICK), button)


async def _observe(args: argparse.Namespace) -> int:
    base = _app_url()
    surface = registry.create(
        SurfaceKind(args.surface_kind),
        entry_url=f"{base}{args.tenant_prefix}/app",
        headless=args.headless,
        run_dir=args.run_dir,
    )
    await surface.start()
    try:
        if not args.no_login:
            await sign_on(surface, base, args.tenant_prefix)

        from .schema import Action, ActionType

        target = f"{base}{args.tenant_prefix}{args.path}"
        result = await surface.act(Action(type=ActionType.NAVIGATE, url=target), None)
        if not result.ok:
            print(f"navigate to {target} failed: {result.detail}", file=sys.stderr)
            return 1

        observation = await surface.observe()
        print(render_table(observation))

        frames = sorted({"/".join(n.frame_path) or "(top)" for n in observation.nodes})
        print(f"\nframes observed: {', '.join(frames)}")
        print(f"observation digest: {observation.digest()[:16]}")

        if args.run_dir:
            snapshot = await surface.snapshot("observe")
            print(f"snapshot: {snapshot.screenshot_ref} {snapshot.a11y_ref}")
    finally:
        await surface.close()
    return 0


def find_artifact(name: str) -> Path:
    """Accept a path, or a capability id resolved to its highest version."""
    direct = Path(name)
    if direct.is_file():
        return direct
    matches = sorted(Path("artifacts").glob(f"{name}.v*.json"))
    if not matches:
        raise SystemExit(f"no artifact named {name!r} under artifacts/")
    return matches[-1]


def _build_escalation(args, capability, params) -> dict:
    from .escalation import InterventionStore, LeaseManager

    return {
        "lease": LeaseManager(
            args.session or f"{capability.id}-{int(time.time())}",
            hold_seconds=args.hold_seconds,
        ),
        "store": InterventionStore(),
        "human": None,
    }


def _human_capture(log, lease):
    from .escalation import HumanCapture

    return HumanCapture(log, is_active=lambda: lease.held_by_operator)


def _operator_escalator(escalation, surface, capability, params, log):
    from .escalation import OperatorEscalator

    return OperatorEscalator(
        lease=escalation["lease"],
        store=escalation["store"],
        surface=surface,
        capability=capability,
        params=params,
        log=log,
        human=escalation["human"],
        goal=capability.description,
    )


async def _serve_console(escalation, port: int):
    """Run the console in-process, for the lifetime of this run.

    Single process by choice: the console has to see the live lease and the
    live intervention, and sharing those across processes would mean building
    the queue the brief explicitly says not to build.
    """
    import uvicorn

    from .escalation.operator_app import create_operator_app

    app = create_operator_app(
        lease=escalation["lease"],
        store=escalation["store"],
        human=escalation["human"],
    )
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.get_running_loop().create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    return server


async def _replay(args: argparse.Namespace) -> int:
    from .evidence import RunLog
    from .replay import ReplayExecutor
    from .schema import Capability

    path = find_artifact(args.artifact)
    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    params = json.loads(args.params) if args.params else {}

    run_dir = args.evidence or f"evidence/replay-{int(time.time())}"
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    from .policy import Mode, PolicyGate
    from .schema import ApprovalState

    gate = PolicyGate(
        mode=Mode.REPLAY,
        artifact_approved=capability.approval.state is ApprovalState.APPROVED,
    )
    escalation = _build_escalation(args, capability, params) if args.escalate else None
    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        headless=args.headless,
        run_dir=run_dir,
        gate=gate,
        lease=escalation["lease"] if escalation else None,
    )
    await surface.start()
    log = RunLog(run_dir, "pending", also_stdout=args.trace)
    console = None
    try:
        escalator = None
        if escalation is not None:
            escalation["human"] = _human_capture(log, escalation["lease"])
            escalator = _operator_escalator(escalation, surface, capability, params, log)
            console = await _serve_console(escalation, args.console_port)
            session_id = escalation["lease"].lease.session_id
            print()
            print(f"operator console  http://localhost:{args.console_port}")
            print(f"session           {session_id}")
            print()
        executor = ReplayExecutor(
            surface,
            run_dir=run_dir,
            log=log,
            escalator=escalator,
            reauth_allowed=gate.reauth_allowed,
            verbose_capture=args.capture_steps,
        )
        result = await executor.run(capability, params)
        if escalation is not None and escalation["human"].steps:
            (Path(run_dir) / "human_steps.json").write_text(
                json.dumps(escalation["human"].as_timeline(), indent=2), encoding="utf-8"
            )
    finally:
        if console is not None:
            await console.shutdown()
        log.close()
        await surface.close()

    print()
    print(json.dumps(json.loads(result.model_dump_json()), indent=2))
    print()
    print(f"status   {result.status.upper()}")
    print(f"evidence {run_dir}/")
    # A business outcome is an answer, not an error: exit 0. Only a failure
    # is a non-zero exit, so a caller's shell semantics match the contract.
    return 1 if result.status == "failure" else 0


async def _discover(args: argparse.Namespace) -> int:
    from .discovery import DiscoveryAgent, Recorder, outcomes_for, recoveries_for
    from .evidence import RunLog
    from .schema import Capability

    base = _app_url()
    entry_url = args.entry or f"{base}{args.tenant_prefix}/login"
    run_dir = Path(args.evidence or f"evidence/discovery-{args.id}")
    run_dir.mkdir(parents=True, exist_ok=True)

    from .policy import Mode, PolicyGate

    surface = registry.create(
        SurfaceKind(args.surface_kind),
        entry_url=entry_url,
        headless=args.headless,
        run_dir=str(run_dir),
        gate=PolicyGate(mode=Mode.DISCOVERY),
    )
    await surface.start()
    log = RunLog(run_dir, f"discovery-{args.id}", also_stdout=not args.quiet)
    try:
        recorder = Recorder(
            surface,
            goal=args.goal,
            entry_url=entry_url,
            tenant_id=args.tenant,
            vendor_product=args.product,
            product_version=args.product_version,
            surface_kind=SurfaceKind(args.surface_kind),
        )
        agent = DiscoveryAgent(
            surface,
            recorder,
            goal=args.goal,
            entry_url=entry_url,
            run_dir=run_dir,
            log=log,
            supervised=args.supervised,
        )
        result = await agent.run()

        (run_dir / "steps.jsonl").write_text(
            "\n".join(
                json.dumps(
                    {
                        "id": rs.step.id,
                        "intent": rs.step.intent,
                        "action": rs.step.action.value,
                        "frame_path": rs.step.frame_path,
                        "risk": rs.step.risk.value,
                        "ladder": rs.ladder_counts,
                        "winning_strategy": (
                            rs.step.target.recorded.winning_strategy.value
                            if rs.step.target
                            else None
                        ),
                        "observed_ms": rs.step.timing.observed_ms_p50,
                    }
                )
                for rs in recorder.steps
            )
            + "\n",
            encoding="utf-8",
        )

        print()
        print(f"discovery {result.status}: {result.reason}")
        print(f"tool calls {result.tool_calls}  duration {result.duration_s:.0f}s  "
              f"recorded steps {len(recorder.steps)}")
        if recorder.skipped:
            print("skipped:")
            for note in recorder.skipped:
                print(f"  - {note}")

        if not result.ok:
            print(f"\nno artifact emitted; evidence in {run_dir}/")
            return 1

        capability = recorder.build_capability(
            capability_id=args.id,
            proposal=result.proposal,
            run_id=f"discovery-{args.id}",
            model=agent.model,
            transcript_sha256=result.transcript_sha256,
            known_outcomes=outcomes_for(args.product, recorder._content_frame()),
            known_recoveries=recoveries_for(args.product),
            title=args.title,
        )
        emitted = json.loads(capability.model_dump_json(exclude_none=False))
        (run_dir / "artifact.json").write_text(
            json.dumps(emitted, indent=2), encoding="utf-8"
        )
        target = Path("artifacts") / f"{args.id}.v1.json"
        if args.write_artifact:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(emitted, indent=2), encoding="utf-8")

        # Re-parse what was written: an artifact that does not load is not an
        # artifact, and finding that out now is cheaper than at replay.
        Capability.model_validate_json((run_dir / "artifact.json").read_text(encoding="utf-8"))

        print()
        print(f"artifact   {run_dir / 'artifact.json'}")
        if args.write_artifact:
            print(f"installed  {target}")
        print(f"inputs     {[i.name for i in capability.contract.inputs]}")
        print(f"outputs    {[o.name for o in capability.contract.outputs]}")
        print(f"steps      {len(capability.steps)}  "
              f"irreversible {len(capability.irreversible_steps)}")
        print(f"approval   {capability.approval.state.value}")
        return 0
    finally:
        log.close()
        await surface.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser(
        "observe", help="print a flat table of the current observation"
    )
    observe.add_argument("--path", default="/app", help="path under the tenant prefix")
    observe.add_argument("--tenant-prefix", default="", help="e.g. /t/summit-cu")
    observe.add_argument("--surface-kind", default=SurfaceKind.LEGACY_WEB.value)
    observe.add_argument("--headless", action="store_true")
    observe.add_argument("--no-login", action="store_true")
    observe.add_argument("--run-dir", default=None, help="write evidence here")
    observe.set_defaults(func=_observe)

    replay = sub.add_parser("replay", help="replay a capability artifact")
    replay.add_argument("--artifact", required=True, help="capability id or path")
    replay.add_argument("--params", default="{}", help="JSON object of input params")
    replay.add_argument("--evidence", default=None, help="evidence directory")
    replay.add_argument("--headless", action="store_true")
    replay.add_argument("--trace", action="store_true", help="echo the run log")
    replay.add_argument(
        "--capture-steps", action="store_true", help="screenshot every step"
    )
    replay.add_argument(
        "--escalate",
        action="store_true",
        help="run under a session lease and serve the operator console",
    )
    replay.add_argument("--console-port", type=int, default=8080)
    replay.add_argument("--session", default=None, help="session id for the lease")
    replay.add_argument(
        "--hold-seconds", type=int, default=600, help="how long an operator hold lasts"
    )
    replay.set_defaults(func=_replay)

    discover = sub.add_parser("discover", help="run the LLM agent against a goal")
    discover.add_argument("--goal", required=True)
    discover.add_argument("--id", required=True, help="capability id, snake_case")
    discover.add_argument("--title", default=None)
    discover.add_argument("--entry", default=None, help="entry point URL")
    discover.add_argument("--tenant-prefix", default="")
    discover.add_argument("--tenant", default="meridian")
    discover.add_argument("--product", default="MERIDIAN CoreBank")
    discover.add_argument("--product-version", default="7.4.11")
    discover.add_argument("--surface-kind", default=SurfaceKind.LEGACY_WEB.value)
    discover.add_argument("--evidence", default=None)
    discover.add_argument("--headless", action="store_true")
    discover.add_argument("--quiet", action="store_true")
    discover.add_argument(
        "--supervised",
        action="store_true",
        help="a human is watching: allow recording an irreversible final step",
    )
    discover.add_argument(
        "--write-artifact", action="store_true", help="also install into artifacts/"
    )
    discover.set_defaults(func=_discover)

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
