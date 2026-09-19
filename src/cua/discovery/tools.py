"""The tool surface exposed to the model, and its dispatch.

Eight tools, none of which accepts a selector. The model's entire vocabulary
for "which element" is a `ref` from the last observation — which is what makes
the recording portable to a surface with no DOM, and what stops the model from
quietly inventing a brittle locator that then gets baked into an artifact.

Dispatch lives here rather than in the agent so the agent loop stays about
*deciding when to stop*, and so every executed action passes one place that
notifies the recorder.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from ..schema import Action, ActionType, ExtractFrom, Observation
from ..surface.base import Surface
from .prompts import render_for_model, substitute_secrets

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "observe",
            "description": (
                "Look at the screen. Returns the current accessibility nodes with "
                "their ref numbers. Refs are only valid until the next action."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Go to a URL in the top-level document.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the element with this ref.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "integer"}},
                "required": ["ref"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": (
                "Type text into the field with this ref. For credentials use the "
                "literal tokens {{APP_USER}} or {{APP_PASSWORD}}."
            ),
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["ref", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select",
            "description": "Choose an option in the dropdown with this ref.",
            "parameters": {
                "type": "object",
                "properties": {"ref": {"type": "integer"}, "option": {"type": "string"}},
                "required": ["ref", "option"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Mark the value of this element as an output of the capability. "
                "Use this for every value the caller asked for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "integer"},
                    "output_name": {
                        "type": "string",
                        "description": "snake_case name, e.g. savings_balance",
                    },
                },
                "required": ["ref", "output_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "The goal is met. Declare the capability contract: the inputs a "
                "caller supplies per invocation and the outputs they receive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "inputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "example": {"type": "string"},
                            },
                            "required": ["name", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "outputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["name", "description"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["summary", "inputs", "outputs"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stuck",
            "description": (
                "Stop: the goal cannot be reached safely, or the next step needs "
                "human approval. Say precisely what is blocking."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

ACTING_TOOLS = {"navigate", "click", "type", "select", "read"}
TERMINAL_TOOLS = {"done", "stuck"}


@dataclass
class ToolOutcome:
    """What the model is told back, plus what the recorder needs."""

    text: str
    observation_after: Observation | None = None
    recorded: bool = False
    error: str | None = None


class ToolBox:
    """Executes a model tool call against the surface."""

    def __init__(
        self,
        surface: Surface,
        recorder,
        secrets: Mapping[str, str],
        *,
        allow_irreversible: bool = True,
    ) -> None:
        self.surface = surface
        self.recorder = recorder
        self.secrets = dict(secrets)
        self.allow_irreversible = allow_irreversible
        self.last_observation: Observation | None = None

    async def observe(self) -> Observation:
        self.last_observation = await self.surface.observe()
        return self.last_observation

    async def call(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        if name == "observe":
            observation = await self.observe()
            return ToolOutcome(text=render_for_model(observation), observation_after=observation)
        if name in ACTING_TOOLS:
            return await self._act(name, args)
        raise ValueError(f"{name} is not an acting tool")  # pragma: no cover

    async def _act(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        before = self.last_observation or await self.observe()
        node = None
        handle = None
        secret_ref = None
        text = args.get("text")

        if name != "navigate":
            ref = int(args["ref"])
            node = before.by_ref(ref)
            if node is None:
                return ToolOutcome(
                    text=f"No element with ref {ref} in the current observation. Call observe first.",
                    error="bad_ref",
                )
            handle = await self.surface.handle_for(ref)
            if handle is None:
                return ToolOutcome(text=f"ref {ref} is stale; call observe again.", error="stale_ref")

        if name == "type":
            text, secret_ref = substitute_secrets(str(text or ""), self.secrets)

        # Score the locator ladder while the element is still on screen.
        target = await self.recorder.prepare(node, before, reading=name == "read")

        action = self._build(name, args, text, secret_ref, node)
        started = time.perf_counter()
        result = await self.surface.act(action, handle)
        duration_ms = int((time.perf_counter() - started) * 1000)

        after = await self.observe()
        if not result.ok:
            return ToolOutcome(
                text=f"{name} failed: {result.detail}\n\n{render_for_model(after)}",
                observation_after=after,
                error=result.detail,
            )

        await self.recorder.record(
            tool=name,
            node=node,
            args=args,
            typed_text=text,
            secret_ref=secret_ref,
            before=before,
            after=after,
            duration_ms=duration_ms,
            read_value=result.read_value,
            target=target,
        )

        confirmation = f"{name} ok"
        if name == "read":
            confirmation = f"read {args['output_name']} = {result.read_value!r}"
        return ToolOutcome(
            text=f"{confirmation}\n\n{render_for_model(after)}",
            observation_after=after,
            recorded=True,
        )

    def _build(self, name, args, text, secret_ref, node) -> Action:
        kinds = {
            "navigate": ActionType.NAVIGATE,
            "click": ActionType.CLICK,
            "type": ActionType.TYPE,
            "select": ActionType.SELECT,
            "read": ActionType.READ,
        }
        return Action(
            type=kinds[name],
            step_id="discovery",
            intent=f"discovery:{name}",
            url=args.get("url") if name == "navigate" else None,
            text=text if name == "type" else None,
            option=args.get("option") if name == "select" else None,
            extract=ExtractFrom.TEXT if name == "read" else None,
            sensitive=secret_ref is not None,
            timeout_ms=20_000,
        )
