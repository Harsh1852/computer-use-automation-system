"""The capability API: what an AI agent talks to.

This closes the through-line. The model discovered the flow once; the artifact
became a reviewable capability; and this is the interface through which an
agent invokes it in production — by name, with typed arguments, getting back
the same three-arm result contract the executor produces.

`GET /capabilities` returns OpenAI tool definitions, so a calling agent can
paste the response straight into its `tools` array. `POST /invoke` replays
deterministically: no model is consulted anywhere behind this endpoint.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..schema import ApprovalState, Capability
from .registry import CapabilityRegistry
from .toolspec import catalog_entry, tool_spec

EVIDENCE_ROOT = Path("evidence")


class InvokeRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None
    evidence_dir: str | None = None


def create_catalog_app(
    registry: CapabilityRegistry | None = None,
    *,
    runner: Any = None,
) -> FastAPI:
    """`runner` is injected so the API can be tested without a browser."""
    registry = registry or CapabilityRegistry()
    app = FastAPI(title="CUA capability catalog", docs_url="/docs", redoc_url=None)

    @app.get("/capabilities")
    def list_capabilities(tenant_id: str | None = None) -> JSONResponse:
        entries = []
        for capability in registry.all():
            resolved = (
                registry.get(capability.id, tenant_id) if tenant_id else capability
            )
            if resolved is None:
                continue  # this tenant has no variant, and guessing is worse
            entry = catalog_entry(resolved)
            entry["tenants"] = registry.tenants_for(capability.id)
            entries.append(entry)
        return JSONResponse(entries)

    @app.get("/tools")
    def list_tools(tenant_id: str | None = None) -> JSONResponse:
        """Just the tool array, ready to hand to a model."""
        specs = []
        for capability in registry.all():
            resolved = registry.get(capability.id, tenant_id) if tenant_id else capability
            if resolved is not None:
                specs.append(tool_spec(resolved))
        return JSONResponse(specs)

    @app.get("/capabilities/{capability_id}")
    def get_capability(capability_id: str, tenant_id: str | None = None) -> JSONResponse:
        capability = registry.get(capability_id, tenant_id)
        if capability is None:
            raise HTTPException(404, _not_found(registry, capability_id, tenant_id))
        return JSONResponse(catalog_entry(capability))

    @app.get("/capabilities/{capability_id}/artifact")
    def get_artifact(capability_id: str, tenant_id: str | None = None) -> JSONResponse:
        capability = registry.get(capability_id, tenant_id)
        if capability is None:
            raise HTTPException(404, _not_found(registry, capability_id, tenant_id))
        import json

        return JSONResponse(json.loads(capability.model_dump_json()))

    @app.post("/capabilities/{capability_id}/invoke")
    async def invoke(capability_id: str, request: InvokeRequest) -> JSONResponse:
        capability = registry.get(capability_id, request.tenant_id)
        if capability is None:
            raise HTTPException(404, _not_found(registry, capability_id, request.tenant_id))
        if runner is None:
            raise HTTPException(503, "no replay runner is attached to this catalog")

        run_dir = request.evidence_dir or str(
            EVIDENCE_ROOT / f"invoke-{capability_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        )
        result = await runner(capability, request.params, run_dir)
        import json

        # Every arm is a 200: a business outcome is an answer, and making the
        # caller distinguish "no such member" from a transport failure by HTTP
        # status would push the conflation this contract exists to prevent
        # back onto them.
        return JSONResponse(json.loads(result.model_dump_json()))

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "capabilities": [c.id for c in registry.all()],
                "approved": [
                    c.id
                    for c in registry.all()
                    if c.approval.state is ApprovalState.APPROVED
                ],
            }
        )

    return app


def _not_found(
    registry: CapabilityRegistry, capability_id: str, tenant_id: str | None
) -> str:
    known = [c.id for c in registry.all()]
    if capability_id not in known:
        return f"no capability {capability_id!r}. Known: {known}"
    return (
        f"{capability_id!r} has no variant for tenant {tenant_id!r}. "
        f"Available tenants: {registry.tenants_for(capability_id)}. "
        "Running another tenant's locators would be worse than refusing."
    )


async def replay_runner(
    capability: Capability,
    params: dict,
    run_dir: str,
    *,
    approved: bool | None = None,
):
    """Default runner: a real deterministic replay, one browser per call.

    `approved` overrides what the gate is told about the artifact's approval
    state. Only the stability command passes it, and only when a human has
    explicitly asked for a supervised verification run - see the note there
    about why an irreversible capability cannot bootstrap its own approval.
    """
    from ..evidence import RunLog
    from ..policy import Mode, PolicyGate
    from ..replay import ReplayExecutor
    from ..surface import registry as surface_registry

    gate = PolicyGate(
        mode=Mode.REPLAY,
        artifact_approved=(
            approved
            if approved is not None
            else capability.approval.state is ApprovalState.APPROVED
        ),
    )
    surface = surface_registry.create(
        capability.target.surface_kind,
        entry_url=capability.target.entry_point,
        run_dir=run_dir,
        gate=gate,
    )
    await surface.start()
    log = RunLog(run_dir, f"invoke-{capability.id}")
    try:
        executor = ReplayExecutor(
            surface, run_dir=run_dir, log=log, reauth_allowed=gate.reauth_allowed
        )
        return await executor.run(capability, params)
    finally:
        log.close()
        await surface.close()
