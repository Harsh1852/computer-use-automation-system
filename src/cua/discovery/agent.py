"""The observe → decide → act loop.

This is the only place in the system that talks to a model, and it is
deliberately small. Its job is to run the loop and to *stop* — the interesting
engineering in discovery is the stopping conditions and the recorder, not the
prompting.

Four independent stops, because an agent that cannot stop is an agent that
burns a budget on a page it will never understand:

* a step budget (25 tool calls)
* a wall clock (5 minutes)
* three consecutive observations with an identical digest — the model is
  acting but nothing is changing
* an explicit `stuck` call, which is the model's own escalation channel

Screenshots go to the model on the first observation and after a `stuck`
signal, and nowhere else. Text observations are an order of magnitude cheaper
and are what the recorded artifact is built from; sending an image every turn
would make the model's competence depend on pixels that no desktop surface
would reproduce the same way. The first image orients it, and the one after
`stuck` is the only moment where "what does this actually look like" is worth
paying for.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schema import Observation
from ..surface.base import Surface
from .prompts import goal_prompt, render_for_model, system_prompt
from .recorder import Recorder
from .tools import ACTING_TOOLS, MUTATING_TOOLS, TERMINAL_TOOLS, TOOL_SPECS, ToolBox

MAX_STEPS = 25
WALL_CLOCK_S = 300
NO_PROGRESS_LIMIT = 3


@dataclass
class DiscoveryResult:
    status: str  # "done" | "stuck" | "blocked" | "exhausted" | "error"
    reason: str = ""
    proposal: dict[str, Any] = field(default_factory=dict)
    tool_calls: int = 0
    duration_s: float = 0.0
    transcript_path: Path | None = None
    transcript_sha256: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "done"


class Transcript:
    """Append-only JSONL of everything said, hashed at the end.

    The hash is what goes into the artifact. The transcript itself stays in
    the evidence directory, so the capability is decoupled from the raw model
    output and cannot carry it into the repository by accident.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._digest = ""

    @property
    def closed(self) -> bool:
        return self._file.closed

    def write(self, kind: str, payload: Any) -> None:
        self._file.write(
            json.dumps({"kind": kind, "payload": payload}, default=str) + "\n"
        )
        self._file.flush()

    def close(self) -> str:
        import hashlib

        if not self._file.closed:
            self._file.close()
            self._digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return self._digest


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        recorder: Recorder,
        *,
        goal: str,
        entry_url: str,
        run_dir: str | Path,
        log,
        client: Any | None = None,
        model: str | None = None,
        secrets: dict[str, str] | None = None,
        max_steps: int = MAX_STEPS,
        wall_clock_s: int = WALL_CLOCK_S,
        supervised: bool = False,
        on_blocked: Any | None = None,
    ) -> None:
        self.surface = surface
        self.recorder = recorder
        self.goal = goal
        self.entry_url = entry_url
        self.run_dir = Path(run_dir)
        self.log = log
        self.max_steps = max_steps
        self.wall_clock_s = wall_clock_s
        self.supervised = supervised
        self.on_blocked = on_blocked
        self.system_prompt = system_prompt(supervised)

        self.model = model or os.environ.get("OPENAI_MODEL")
        if not self.model:
            raise RuntimeError("OPENAI_MODEL is not set; the model name is never hardcoded")
        self.client = client or self._default_client()
        self.tools = ToolBox(surface, recorder, secrets or dict(os.environ))

    @staticmethod
    def _default_client():
        from openai import AsyncOpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; discovery needs model access")
        # A per-minute token limit is a queue, not a verdict: the provider
        # says how long to wait and the SDK honours it. Retrying is still
        # bounded by the run's own wall clock, so this cannot turn a rate
        # limit into a hang - and a run abandoned at step nine because of a
        # one-second budget hiccup is the more expensive outcome.
        return AsyncOpenAI(max_retries=5)

    # ------------------------------------------------------------- the loop

    async def run(self) -> DiscoveryResult:
        transcript = Transcript(self.run_dir / "transcript.jsonl")
        started = time.perf_counter()

        try:
            return await self._loop(transcript, started)
        finally:
            # The transcript is the evidence. It gets closed and hashed
            # whatever happened, including a model that never answered.
            if not transcript.closed:
                transcript.close()

    async def _loop(self, transcript: "Transcript", started: float) -> DiscoveryResult:
        calls = 0
        digests: list[str] = []
        status, reason, proposal = "exhausted", "step budget exhausted", {}

        observation = await self.tools.observe()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": await self._first_turn_content(observation),
            },
        ]
        transcript.write("system", self.system_prompt)
        transcript.write("mode", {"supervised": self.supervised})
        transcript.write("goal", {"goal": self.goal, "entry_url": self.entry_url})

        while True:
            if calls >= self.max_steps:
                status, reason = "exhausted", f"step budget of {self.max_steps} reached"
                break
            if time.perf_counter() - started > self.wall_clock_s:
                status, reason = "exhausted", f"wall clock of {self.wall_clock_s}s reached"
                break

            try:
                choice = await self._ask(messages, transcript)
            except Exception as exc:
                # The model being unavailable is an operational condition, not
                # a bug in the loop. Report it as one, keep the partial
                # transcript, and let the caller see why nothing was recorded.
                detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                transcript.write("model_error", detail)
                self.log.event("agent.model_error", detail=detail)
                status, reason = "error", detail
                break

            if choice is None:
                status, reason = "exhausted", "model returned no tool call"
                break

            messages.append(choice.model_dump(exclude_none=True))
            finished = False

            for call in choice.tool_calls or []:
                calls += 1
                name = call.function.name
                args = self._parse_args(call.function.arguments)
                self.log.event("agent.tool", tool=name, args=self._loggable(name, args))
                transcript.write("tool_call", {"name": name, "args": self._loggable(name, args)})

                if name in TERMINAL_TOOLS:
                    status = "done" if name == "done" else "stuck"
                    reason = str(args.get("reason") or args.get("summary") or "")
                    proposal = args if name == "done" else {}
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": "acknowledged"}
                    )
                    finished = True
                    break

                if name not in ACTING_TOOLS and name != "observe":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"unknown tool {name}",
                        }
                    )
                    continue

                outcome = await self.tools.call(name, args)
                transcript.write(
                    "tool_result",
                    {"name": name, "error": outcome.error, "chars": len(outcome.text)},
                )
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": outcome.text}
                )

                if outcome.error == "policy_blocked":
                    blocked = self.tools.blocked
                    status = "blocked"
                    reason = f"RISKY_APPROVAL: {blocked} (rule: {blocked.rule})"
                    self.log.event(
                        "agent.policy_blocked",
                        step=name,
                        rule=blocked.rule,
                        reason="RISKY_APPROVAL",
                        detail=str(blocked),
                    )
                    if self.on_blocked is not None:
                        self.on_blocked(reason, str(blocked))
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": outcome.text}
                    )
                    finished = True
                    break

                if outcome.observation_after is not None and name in MUTATING_TOOLS:
                    digests.append(outcome.observation_after.digest())
                    if self._stalled(digests):
                        status, reason = (
                            "stuck",
                            f"{NO_PROGRESS_LIMIT} consecutive observations were identical",
                        )
                        finished = True
                        break

            if finished:
                break

        if status == "stuck":
            # The one other moment a picture is worth its tokens: whatever the
            # model could not read, a human reviewing the intervention can.
            await self._capture_stuck(transcript)

        digest = transcript.close()
        duration = time.perf_counter() - started
        self.log.event(
            "agent.finish",
            status=status,
            reason=reason,
            tool_calls=calls,
            duration_s=round(duration, 1),
            recorded_steps=len(self.recorder.steps),
            skipped=self.recorder.skipped,
        )
        return DiscoveryResult(
            status=status,
            reason=reason,
            proposal=proposal,
            tool_calls=calls,
            duration_s=duration,
            transcript_path=transcript.path,
            transcript_sha256=digest,
        )

    # ----------------------------------------------------------- model call

    async def _ask(self, messages, transcript):
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOL_SPECS,
            tool_choice="auto",
        )
        choice = response.choices[0].message
        transcript.write(
            "model",
            {
                "content": choice.content,
                "tool_calls": [
                    {"name": c.function.name, "arguments": c.function.arguments}
                    for c in (choice.tool_calls or [])
                ],
                "usage": getattr(response, "usage", None)
                and response.usage.model_dump(),
            },
        )
        return choice if (choice.tool_calls or choice.content) else None

    async def _first_turn_content(self, observation: Observation) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": goal_prompt(self.goal, self.entry_url)},
            {"type": "text", "text": render_for_model(observation)},
        ]
        if image := await self._screenshot_data_url("00-start"):
            parts.append({"type": "image_url", "image_url": {"url": image}})
        return parts

    async def _screenshot_data_url(self, label: str) -> str | None:
        try:
            snapshot = await self.surface.snapshot(label)
        except Exception:  # pragma: no cover - never let capture break a run
            return None
        if not snapshot.screenshot_ref:
            return None
        path = self.run_dir / snapshot.screenshot_ref
        if not path.exists():
            return None
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/png;base64,{encoded}"

    async def _capture_stuck(self, transcript) -> None:
        snapshot = await self.surface.snapshot("stuck")
        transcript.write(
            "stuck_capture",
            {"screenshot": snapshot.screenshot_ref, "a11y": snapshot.a11y_ref},
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _parse_args(raw: str | None) -> dict[str, Any]:
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _loggable(name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Credentials reach the browser as placeholders, so nothing to redact
        here — but the guard stays, because a future tool might not."""
        if name == "type" and "{{" not in str(args.get("text", "")):
            return {**args, "text": "<value>"}
        return args

    @staticmethod
    def _stalled(digests: list[str]) -> bool:
        return (
            len(digests) >= NO_PROGRESS_LIMIT
            and len(set(digests[-NO_PROGRESS_LIMIT:])) == 1
        )
