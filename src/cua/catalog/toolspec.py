"""Capabilities as things a model can call.

The whole point of the artifact having a *contract* rather than just a step
list is that this function is mechanical. Inputs, types, patterns,
descriptions and required-ness are already declared and already validated, so
turning a capability into an OpenAI tool definition is a projection, not an
interpretation — and a calling agent reads exactly what a human reviewer read.

Two things are deliberately included, because they are what a caller needs in
order to decide anything:

* the **outcomes**, in the description. A model that does not know
  `MEMBER_NOT_FOUND` is a possible answer will treat it as a failure and
  retry, which is precisely the conflation the result contract exists to
  prevent.
* the **approval state**. An unapproved capability is callable but the caller
  should know it is a draft, and that its irreversible steps will be refused.
"""

from __future__ import annotations

from typing import Any

from ..schema import Capability, ParamType

_JSON_TYPE = {
    ParamType.STRING: "string",
    ParamType.INTEGER: "integer",
    ParamType.NUMBER: "number",
    ParamType.BOOLEAN: "boolean",
}


def parameter_schema(capability: Capability) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for param in capability.contract.inputs:
        entry: dict[str, Any] = {
            "type": _JSON_TYPE[param.type],
            "description": param.description,
        }
        if param.pattern:
            entry["pattern"] = param.pattern
        # An example is only present on non-regulated parameters; the schema
        # forbids one on a pii or secret field.
        if param.example:
            entry["examples"] = [param.example]
        properties[param.name] = entry

    return {
        "type": "object",
        "properties": properties,
        "required": [p.name for p in capability.contract.inputs if p.required],
        "additionalProperties": False,
    }


def describe(capability: Capability) -> str:
    lines = [capability.description.strip()]

    if capability.contract.outputs:
        returns = ", ".join(
            f"{field.name} ({field.description.rstrip('.')})"
            for field in capability.contract.outputs
        )
        lines.append(f"Returns: {returns}.")

    if capability.outcomes:
        codes = ", ".join(f"{o.code} ({o.description.rstrip('.')})" for o in capability.outcomes)
        lines.append(
            "May also answer with one of these legitimate outcomes rather than "
            f"succeeding: {codes}. These are answers, not errors; do not retry them."
        )

    if capability.has_irreversible_step:
        lines.append(
            "Contains an irreversible step. It will be refused unless the "
            f"capability is approved (currently: {capability.approval.state.value})."
        )
    return " ".join(lines)


def tool_spec(capability: Capability) -> dict[str, Any]:
    """OpenAI tool-calling shape. Directly usable as a `tools` entry."""
    return {
        "type": "function",
        "function": {
            "name": capability.id,
            "description": describe(capability),
            "parameters": parameter_schema(capability),
        },
    }


def catalog_entry(capability: Capability) -> dict[str, Any]:
    """Tool spec plus the metadata a human or a router needs to choose."""
    return {
        "tool": tool_spec(capability),
        "id": capability.id,
        "version": capability.version,
        "title": capability.title,
        "approval": capability.approval.state.value,
        "stability": capability.approval.stability,
        "replays": capability.approval.replays,
        "vendor_product": capability.target.vendor_product,
        "product_version": capability.target.product_version,
        "tenant_id": capability.target.tenant_id,
        "variant_of": capability.target.variant_of,
        "surface_kind": capability.target.surface_kind.value,
        "steps": len(capability.steps),
        "irreversible_steps": [s.id for s in capability.irreversible_steps],
        "outcomes": [o.code for o in capability.outcomes],
        "outputs": [
            {"name": f.name, "type": f.type.value, "sensitivity": f.sensitivity.value}
            for f in capability.contract.outputs
        ],
    }
