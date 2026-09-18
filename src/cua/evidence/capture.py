"""Richer capture: screenshots, observation dumps, and the result document.

Screenshots are taken at every step boundary when capture is verbose, and
always on failure — the brief asks for at least one richer signal on failure,
and a `TARGET_NOT_FOUND` with no picture of the screen is a bug report nobody
can action.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import Capability, Observation, ReplayResult
from ..surface.base import Surface, render_table
from .manifest import redact_outputs


class EvidenceWriter:
    def __init__(self, run_dir: str | Path, *, verbose: bool = False) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self._seq = 0

    async def step_capture(
        self, surface: Surface, label: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Screenshot plus the flat observation table, named in run order."""
        if not (self.verbose or force):
            return {}
        self._seq += 1
        name = f"{self._seq:02d}-{label}"
        try:
            snapshot = await surface.snapshot(name)
        except Exception as exc:  # pragma: no cover - capture must never break a run
            return {"capture_error": f"{type(exc).__name__}: {exc}"}
        return {
            "screenshot": snapshot.screenshot_ref,
            "a11y": snapshot.a11y_ref,
        }

    def observation_dump(self, observation: Observation, label: str) -> str:
        path = self.run_dir / "observations"
        path.mkdir(parents=True, exist_ok=True)
        file = path / f"{label}.txt"
        file.write_text(render_table(observation), encoding="utf-8")
        return str(file.relative_to(self.run_dir))

    def write_result(
        self, result: ReplayResult, capability: Capability
    ) -> Path:
        """`result.json`, with declared-sensitive outputs redacted.

        The caller gets the real values in the returned object; the committed
        evidence does not. Splitting those two is the whole point of tagging
        outputs with a sensitivity in the contract.
        """
        payload = json.loads(result.model_dump_json())
        if "outputs" in payload:
            payload["outputs"] = redact_outputs(capability, payload["outputs"])
        path = self.run_dir / "result.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path
