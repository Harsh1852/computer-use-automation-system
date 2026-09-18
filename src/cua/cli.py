"""Command line entry point.

Deliberately thin: it wires environment to objects and prints. No automation
logic lives here, and nothing here imports a driver — the surface registry
does that, which is how the seam stays checkable with a grep.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

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

    args = parser.parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
