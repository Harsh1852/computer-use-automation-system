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
from ..surface.base import PolicyViolation, Surface
from .prompts import SECRET_TOKENS, near_hint, render_for_model, substitute_secrets

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

MUTATING_TOOLS = {"navigate", "click", "type", "select"}
"""Tools that claim to change the screen.

Only these count toward the no-progress stop. A `read` marks a value for
extraction and deliberately leaves the page exactly as it was, so a run that
reads three outputs from one confirmation screen produces three identical
observations while making perfect progress."""
TERMINAL_TOOLS = {"done", "stuck"}

CREDENTIAL_LABELS = ("PASSWORD", "USER ID", "USERID", "USERNAME", "USER NAME")
"""Labels that mark a field the model must not fill in from its own head.

Credential fields in these applications carry no accessible name, so this
matches on the same near-text a human reads. It is a label test rather than a
DOM test on purpose: the rule has to mean something on a desktop surface too,
where there is no `input type=password` to inspect."""


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
        self.blocked: PolicyViolation | None = None

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
            if refusal := self._invented_credential(before, node, secret_ref):
                return refusal
        elif name == "select":
            # The chosen option is a value the caller supplied as surely as
            # typed text is, so it has to be bindable as a parameter.
            text = str(args.get("option") or "")

        # Score the locator ladder while the element is still on screen.
        target = await self.recorder.prepare(node, before, reading=name == "read")

        action = self._build(name, args, text, secret_ref, node)
        started = time.perf_counter()
        try:
            result = await self.surface.act(action, handle)
        except PolicyViolation as exc:
            # The gate refused. That is not an error in the run — it is the
            # run reaching a decision a person has to make — so it is
            # reported as such and the agent stops rather than retrying.
            self.blocked = exc
            return ToolOutcome(
                text=(
                    f"BLOCKED BY POLICY: {exc}. This needs human approval. "
                    "Call stuck describing what requires sign-off."
                ),
                error="policy_blocked",
            )
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

    def _invented_credential(
        self, before: Observation, node, secret_ref: str | None
    ) -> "ToolOutcome | None":
        """Refuse a made-up credential instead of submitting it.

        The prompt already tells the model to use the placeholder tokens, and
        the model mostly does — but "mostly" is the wrong guarantee for a
        sign-on. A guess does not merely fail: it spends a real authentication
        attempt against a real account, which is how automation walks into a
        lockout. So the refusal lives here, in front of the surface, next to
        every other rule the model is not trusted to keep.

        Returning it as a tool result rather than raising is deliberate — the
        model reads the error and retries with the token, so the run continues
        instead of dying at the login screen.
        """
        if secret_ref is not None or node is None:
            return None
        label = f"{node.name or ''} {near_hint(before, node) or ''}".upper()
        if not any(marker in label for marker in CREDENTIAL_LABELS):
            return None
        tokens = ", ".join(sorted(SECRET_TOKENS))
        return ToolOutcome(
            text=(
                "REFUSED: that field is a credential and the text you supplied "
                "is not one of the placeholders. You do not know these values. "
                f"Type one of {tokens} literally and the runtime will substitute "
                "the real value."
            ),
            error="invented_credential",
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
