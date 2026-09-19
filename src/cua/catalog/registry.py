"""What capabilities exist, and which variant a given tenant gets.

The registry is the one place that answers "give me `lookup_member_balance`
for `summit-cu`". A caller asks for a capability by name and a tenant; it
never learns whether it got the base recording, the base plus an overlay, or
a fork — which is what makes adding a tenant a data change.
"""

from __future__ import annotations

from pathlib import Path

from ..schema import Capability
from .overlay import Overlay, apply_overlay

ARTIFACT_DIR = Path("artifacts")


class CapabilityRegistry:
    def __init__(
        self,
        artifact_dir: str | Path = ARTIFACT_DIR,
        overlay_dir: str | Path | None = None,
    ) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.overlay_dir = Path(overlay_dir) if overlay_dir else self.artifact_dir / "overlays"

    # ------------------------------------------------------------- loading

    def paths(self) -> list[Path]:
        return sorted(p for p in self.artifact_dir.glob("*.json") if p.is_file())

    def all(self) -> list[Capability]:
        found: dict[str, Capability] = {}
        for path in self.paths():
            try:
                capability = Capability.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except Exception:
                # A malformed artifact must not take the catalog down with it;
                # the other capabilities are still callable.
                continue
            existing = found.get(capability.id)
            if existing is None or _newer(capability.version, existing.version):
                found[capability.id] = capability
        return sorted(found.values(), key=lambda c: c.id)

    def get(self, capability_id: str, tenant_id: str | None = None) -> Capability | None:
        base = next((c for c in self.all() if c.id == capability_id), None)
        if base is None:
            return None
        if tenant_id is None or tenant_id == base.target.tenant_id:
            return base
        overlay = self.overlay_for(capability_id, tenant_id)
        if overlay is None:
            # Deliberately *not* falling back to the base recording. Running
            # one tenant's locators against another is how automation types
            # into the wrong field and calls it success.
            return None
        return apply_overlay(base, overlay)

    # ------------------------------------------------------------ overlays

    def overlays(self) -> list[Overlay]:
        if not self.overlay_dir.is_dir():
            return []
        found = []
        for path in sorted(self.overlay_dir.glob("*.json")):
            try:
                found.append(Overlay.load(path))
            except Exception:
                continue
        return found

    def overlay_for(self, capability_id: str, tenant_id: str) -> Overlay | None:
        return next(
            (
                o
                for o in self.overlays()
                if o.capability_id == capability_id and o.tenant_id == tenant_id
            ),
            None,
        )

    def tenants_for(self, capability_id: str) -> list[str]:
        base = next((c for c in self.all() if c.id == capability_id), None)
        tenants = [base.target.tenant_id] if base else []
        tenants += [o.tenant_id for o in self.overlays() if o.capability_id == capability_id]
        return sorted(set(tenants))

    # -------------------------------------------------------------- saving

    def save(self, capability: Capability) -> Path:
        """Write a capability back, preserving the `<id>.v<major>.json` name."""
        major = capability.version.split(".", 1)[0]
        path = self.artifact_dir / f"{capability.id}.v{major}.json"
        import json

        path.write_text(
            json.dumps(json.loads(capability.model_dump_json()), indent=2),
            encoding="utf-8",
        )
        return path


def _newer(a: str, b: str) -> bool:
    return tuple(int(p) for p in a.split(".")) > tuple(int(p) for p in b.split("."))
