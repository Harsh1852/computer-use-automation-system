"""Capabilities as an agent-facing catalog: tool specs, overlays, approval."""

from .approval import DEFAULT_THRESHOLD, StabilityReport, promote
from .api import create_catalog_app, replay_runner
from .overlay import InsertedStep, Overlay, OverlayError, apply_overlay, write_overlay
from .registry import CapabilityRegistry
from .toolspec import catalog_entry, describe, parameter_schema, tool_spec

__all__ = [
    "CapabilityRegistry",
    "DEFAULT_THRESHOLD",
    "InsertedStep",
    "Overlay",
    "OverlayError",
    "StabilityReport",
    "apply_overlay",
    "catalog_entry",
    "create_catalog_app",
    "describe",
    "parameter_schema",
    "promote",
    "replay_runner",
    "tool_spec",
    "write_overlay",
]
