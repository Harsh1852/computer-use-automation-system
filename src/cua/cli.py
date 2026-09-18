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


async def _replay(args: argparse.Namespace) -> int:
    from .evidence import RunLog
    from .replay import ReplayExecutor
    from .schema import Capability

    path = find_artifact(args.artifact)
    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    params = json.loads(args.params) if args.params else {}

    run_dir = args.evidence or f"evidence/replay-{int(time.time())}"
    Path(run_dir).mkdir(parents=True, exist_ok=True)

    surface = registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        headless=args.headless,
        run_dir=run_dir,
    )
    await surface.start()
    log = RunLog(run_dir, "pending", also_stdout=args.trace)
    try:
        executor = ReplayExecutor(
            surface, run_dir=run_dir, log=log, verbose_capture=args.capture_steps
        )
        result = await executor.run(capability, params)
    finally:
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
    replay.set_defaults(func=_replay)

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
