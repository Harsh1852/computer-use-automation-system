"""The manifest that makes a run directory self-describing.

An evidence directory that says what happened but not *what was run* is
useless a week later. The manifest records which artifact and version, which
parameters (redacted by their declared sensitivity, never by guesswork), how
long it took, and the environment.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..schema import Capability, Sensitivity

REDACTED = "<redacted>"


def redact_params(
    capability: Capability, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Redact by declared sensitivity.

    Driven by the contract rather than by pattern-matching the values: the
    artifact already states which inputs are regulated, so the evidence layer
    does not have to guess. Parameters the contract does not declare are
    redacted too — an undeclared value is an unknown one.
    """
    declared = {p.name: p for p in capability.contract.inputs}
    out: dict[str, Any] = {}
    for name, value in params.items():
        param = declared.get(name)
        if param is None or param.sensitivity.restricted:
            out[name] = REDACTED
        else:
            out[name] = value
    return out


def redact_outputs(
    capability: Capability, outputs: Mapping[str, Any]
) -> dict[str, Any]:
    """Same rule for what came back.

    The caller receives the real value — it asked for it. The committed
    evidence does not: a balance is regulated financial data about a person,
    and a repository is not where it belongs.
    """
    declared = {f.name: f for f in capability.contract.outputs}
    return {
        name: REDACTED
        if (declared.get(name) is None or declared[name].sensitivity.restricted)
        else value
        for name, value in outputs.items()
    }


def write_manifest(
    run_dir: str | Path,
    *,
    capability: Capability,
    run_id: str,
    params: Mapping[str, Any],
    started_at: datetime,
    finished_at: datetime,
    status: str,
    step_timings: Mapping[str, int] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "capability": {
            "id": capability.id,
            "version": capability.version,
            "approval": capability.approval.state.value,
            "schema_version": capability.schema_version,
        },
        "target": {
            "vendor_product": capability.target.vendor_product,
            "product_version": capability.target.product_version,
            "tenant_id": capability.target.tenant_id,
            "surface_kind": capability.target.surface_kind.value,
            "entry_point": capability.target.entry_point,
        },
        "params": redact_params(capability, params),
        "timings": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "steps_ms": dict(step_timings or {}),
        },
        "environment": {
            "git_sha": os.environ.get("GIT_SHA"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "app_url": os.environ.get("APP_URL"),
            "model": os.environ.get("OPENAI_MODEL"),
        },
        **(dict(extra) if extra else {}),
    }
    path = run_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def sensitivity_note(capability: Capability) -> dict[str, str]:
    return {
        f.name: f.sensitivity.value for f in capability.contract.outputs
    } | {p.name: p.sensitivity.value for p in capability.contract.inputs}


__all__ = [
    "REDACTED",
    "redact_outputs",
    "redact_params",
    "sensitivity_note",
    "write_manifest",
    "Sensitivity",
]
