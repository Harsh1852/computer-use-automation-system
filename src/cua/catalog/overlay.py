"""Reusing one recording across tenants on the same vendor product.

Hundreds of institutions run the same twenty applications, configured and
branded differently. Re-recording every capability per tenant is the failure
mode this exists to avoid, and forking per tenant is only a slightly slower
version of the same failure.

An overlay may change **how a control is named and where it lives**:

* frame names (`contentFrame` → `main`)
* near-text anchors (`MEMBER NUMBER` → `ACCOUNT NUMBER`)
* accessible names, URL prefixes, entry point
* the value bound to a step
* *additional* input steps, for a field this tenant requires and the base
  does not

An overlay may **not** change the flow: it cannot remove a step, reorder
steps, change an action verb, or raise a step's risk. A tenant that needs any
of those is not a variant, it is a different capability, and it must fork with
an explicit `variant_of` link so the divergence is visible rather than hidden
inside an override file.

That line is enforced below, not merely described. The distinction is the
whole point: an override that could quietly turn a read into a confirmation
would make "the same capability, adapted" a claim nobody could trust.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schema import (
    Capability,
    Condition,
    Risk,
    Step,
    TargetCandidate,
    TargetSpec,
    ValueRef,
)

OVERLAY_DIR = Path("artifacts/overlays")


class OverlayError(ValueError):
    """An overlay tried to do something only a fork may do."""


class InsertedStep(BaseModel):
    """An extra field this tenant requires, and where it goes."""

    model_config = ConfigDict(extra="forbid")

    after: str = Field(pattern=r"^s\d+$")
    step: Step

    @model_validator(mode="after")
    def _only_additional_input(self) -> "InsertedStep":
        if self.step.action.value not in ("type", "select"):
            raise OverlayError(
                f"an overlay may only insert a type or select step, not "
                f"{self.step.action.value}; changing the flow requires a fork"
            )
        if self.step.risk is not Risk.SAFE_REVERSIBLE:
            raise OverlayError(
                "an inserted step must be safe_reversible; an overlay may not "
                "introduce a state-changing action"
            )
        return self


class Overlay(BaseModel):
    """Per-tenant specialisation of one capability."""

    model_config = ConfigDict(extra="forbid")

    overlay_version: Literal["1.0"] = "1.0"
    capability_id: str
    tenant_id: str
    description: str = ""

    entry_point: str | None = None
    url_prefix: str | None = None
    """Inserted after the host in every `url_matches` pattern, e.g.
    `/t/summit-cu`."""

    frame_map: dict[str, str] = {}
    anchor_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    value_overrides: dict[str, ValueRef] = {}
    inserted_steps: list[InsertedStep] = []

    @classmethod
    def load(cls, path: str | Path) -> "Overlay":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def for_tenant(cls, tenant_id: str, directory: Path | None = None) -> "Overlay | None":
        candidate = (directory or OVERLAY_DIR) / f"{tenant_id}.json"
        return cls.load(candidate) if candidate.is_file() else None


def apply_overlay(capability: Capability, overlay: Overlay) -> Capability:
    """Return a new capability specialised for the overlay's tenant.

    Pure: the base artifact is never mutated, so the same recording can serve
    every tenant in the same process without them contaminating each other.
    """
    if overlay.capability_id != capability.id:
        raise OverlayError(
            f"overlay targets {overlay.capability_id!r}, not {capability.id!r}"
        )

    steps = [_rewrite_step(step, overlay) for step in capability.steps]
    steps = _insert(steps, overlay)
    steps = _renumber(steps)

    target = capability.target.model_copy(
        update={
            "tenant_id": overlay.tenant_id,
            "entry_point": overlay.entry_point or capability.target.entry_point,
        }
    )

    # The step ids may have shifted, so output bindings have to follow.
    produced = {s.output.name: s.id for s in steps if s.output is not None}
    outputs = [
        field.model_copy(update={"from_step": produced.get(field.name, field.from_step)})
        for field in capability.contract.outputs
    ]

    return capability.model_copy(
        update={
            "target": target,
            "steps": steps,
            "contract": capability.contract.model_copy(update={"outputs": outputs}),
            "outcomes": [
                o.model_copy(update={"detect": _rewrite_condition(o.detect, overlay)})
                for o in capability.outcomes
            ],
            "recoveries": [
                r.model_copy(
                    update={
                        "when": _rewrite_condition(r.when, overlay),
                        "target": _rewrite_spec(r.target, overlay) if r.target else None,
                    }
                )
                for r in capability.recoveries
            ],
            "success_condition": _rewrite_condition(capability.success_condition, overlay),
        }
    )


# --------------------------------------------------------------- rewriting


def _frames(frame_path: list[str], overlay: Overlay) -> list[str]:
    return [overlay.frame_map.get(name, name) for name in frame_path]


def _rewrite_candidate(candidate: TargetCandidate, overlay: Overlay) -> TargetCandidate:
    updates: dict = {}
    if candidate.anchor and candidate.anchor in overlay.anchor_map:
        updates["anchor"] = overlay.anchor_map[candidate.anchor]
    if candidate.name and candidate.name in overlay.name_map:
        updates["name"] = overlay.name_map[candidate.name]
    return candidate.model_copy(update=updates) if updates else candidate


def _rewrite_spec(spec: TargetSpec, overlay: Overlay) -> TargetSpec:
    return spec.model_copy(
        update={
            "primary": _rewrite_candidate(spec.primary, overlay),
            "fallbacks": [_rewrite_candidate(c, overlay) for c in spec.fallbacks],
        }
    )


def _rewrite_pattern(pattern: str, overlay: Overlay) -> str:
    if not overlay.url_prefix or overlay.url_prefix in pattern:
        return pattern
    # Patterns are host-relative globs like `*/members/detail?...`; the tenant
    # prefix goes between the wildcard and the path.
    if pattern.startswith("*/"):
        return f"*{overlay.url_prefix}/{pattern[2:]}"
    return pattern


def _rewrite_condition(condition: Condition, overlay: Overlay) -> Condition:
    kind = condition.kind
    if kind == "url_matches":
        return condition.model_copy(
            update={
                "pattern": _rewrite_pattern(condition.pattern, overlay),
                "frame_path": _frames(condition.frame_path, overlay),
            }
        )
    if kind in ("text_present", "text_absent"):
        text = overlay.name_map.get(condition.text, condition.text)
        text = overlay.anchor_map.get(text, text)
        return condition.model_copy(
            update={"text": text, "frame_path": _frames(condition.frame_path, overlay)}
        )
    if kind in ("node_present", "node_absent"):
        return condition.model_copy(
            update={
                "name": overlay.name_map.get(condition.name, condition.name)
                if condition.name
                else None,
                "frame_path": _frames(condition.frame_path, overlay),
            }
        )
    if kind in ("all_of", "any_of"):
        return condition.model_copy(
            update={
                "conditions": [
                    _rewrite_condition(c, overlay) for c in condition.conditions
                ]
            }
        )
    return condition


def _rewrite_step(step: Step, overlay: Overlay) -> Step:
    updates: dict = {
        "frame_path": _frames(step.frame_path, overlay),
        "postcondition": _rewrite_condition(step.postcondition, overlay),
    }
    if step.target is not None:
        updates["target"] = _rewrite_spec(step.target, overlay)
    if step.value is not None and step.value.literal and overlay.url_prefix:
        updates["value"] = ValueRef(
            literal=_insert_prefix_into_url(step.value.literal, overlay.url_prefix),
            sensitivity=step.value.sensitivity,
        )
    if step.id in overlay.value_overrides:
        updates["value"] = overlay.value_overrides[step.id]
    return step.model_copy(update=updates)


def _insert_prefix_into_url(url: str, prefix: str) -> str:
    if prefix in url or "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme}://{host}{prefix}{slash}{path}" if slash else url


def _insert(steps: list[Step], overlay: Overlay) -> list[Step]:
    if not overlay.inserted_steps:
        return steps
    known = {s.id for s in steps}
    for inserted in overlay.inserted_steps:
        if inserted.after not in known:
            raise OverlayError(
                f"overlay inserts after {inserted.after!r}, which the base "
                "capability does not contain"
            )
    out: list[Step] = []
    for step in steps:
        out.append(step)
        for inserted in overlay.inserted_steps:
            if inserted.after == step.id:
                out.append(_rewrite_step(inserted.step, overlay))
    return out


def _renumber(steps: list[Step]) -> list[Step]:
    """Re-id sequentially after insertion, keeping references consistent.

    An inserted step would otherwise collide with an existing id, and the
    schema rejects duplicates — correctly, since two steps called `s7` make
    every `from_step` ambiguous.
    """
    mapping = {step.id: f"s{i + 1}" for i, step in enumerate(steps)}
    return [step.model_copy(update={"id": mapping[step.id]}) for step in steps]


def write_overlay(overlay: Overlay, directory: Path | None = None) -> Path:
    path = (directory or OVERLAY_DIR) / f"{overlay.tenant_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(json.loads(overlay.model_dump_json()), indent=2), encoding="utf-8"
    )
    return path
