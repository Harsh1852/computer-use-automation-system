"""Turning a successful model run into a capability artifact.

The recorder runs *alongside* the agent, not inside it. The model decides what
to do; the recorder decides what that means as a reusable capability. Keeping
them apart is what stops the model's output shape from becoming the schema —
the model never sees a `TargetSpec`, and could not write one if it wanted to.

For each accepted action it does six things:

1. **Builds the full candidate ladder**, not just the strategy that worked,
   by constructing every applicable rung and testing each one against the live
   observation.
2. **Counts matches per rung**, which is the uniqueness evidence a reviewer
   reads. A rung that matched three elements is recorded as having matched
   three, and replay will treat it as a miss.
3. **Records observed latency** and sets a generous timeout from it.
4. **Infers a postcondition** from what actually changed between the
   observation before the action and the one after.
5. **Parameterises literals** — typed values that match a declared input
   become `{"param": ...}`, credentials become `{"secret_ref": ...}`, and
   concrete routes are canonicalised to `/members/detail?mbr={member_id}`.
6. **Classifies risk** from the action and where it landed.

What the model proposes in `done()` is a *proposal*. The recorder validates it
against what was actually typed and read, and drops anything unsupported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..schema import (
    ActionType,
    AllOf,
    Approval,
    BusinessOutcome,
    Capability,
    Condition,
    Contract,
    ExtractFrom,
    InputParam,
    NodePresent,
    Observation,
    OutputBinding,
    OutputField,
    ParamType,
    Provenance,
    RecordedEvidence,
    RecoveryAction,
    RecoveryRule,
    Risk,
    Sensitivity,
    Step,
    SurfaceKind,
    TargetBinding,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    TextPresent,
    Timing,
    Transform,
    UiNode,
    UrlMatches,
    ValueRef,
)
from ..surface.base import Surface

MONEY = re.compile(r"^-?[\d,]+\.\d{2}$")

DATA_LIKE = re.compile(r"\d[\d,]*\.\d{2}|\d{4,}")
"""Text that looks like a record's data rather than screen furniture."""


@dataclass
class RiskRules:
    """Where the risk classes come from.

    These are the same rules the policy file will own once policy exists; the
    recorder holds a copy so a recording made today is classified the same way
    the gate will judge it tomorrow.
    """

    irreversible_routes: tuple[str, ...] = ("*/confirm", "*/transfer/*", "*/delete*")
    irreversible_labels: tuple[str, ...] = (
        "CONFIRM",
        "SUBMIT",
        "SUBMIT TRANSFER",
        "DELETE",
        "POST",
    )


@dataclass
class RecordedStep:
    step: Step
    node_name: str | None
    typed_text: str | None
    output_name: str | None
    ladder_counts: dict[str, int]


class Recorder:
    def __init__(
        self,
        surface: Surface,
        *,
        goal: str,
        entry_url: str,
        tenant_id: str,
        vendor_product: str,
        product_version: str,
        surface_kind: SurfaceKind,
        risk_rules: RiskRules | None = None,
    ) -> None:
        self.surface = surface
        self.goal = goal
        self.entry_url = entry_url
        self.tenant_id = tenant_id
        self.vendor_product = vendor_product
        self.product_version = product_version
        self.surface_kind = surface_kind
        self.rules = risk_rules or RiskRules()
        self.steps: list[RecordedStep] = []
        self.skipped: list[str] = []
        self._counter = 0

    # ----------------------------------------------------------- recording

    async def prepare(
        self, node: UiNode | None, before: Observation, *, reading: bool = False
    ) -> TargetSpec | None:
        """Build and score the locator ladder *before* the action runs.

        This has to happen while the element is still on screen. Scoring a
        ladder after a click that navigates away measures the next page, every
        rung comes back with zero matches, and the step is silently dropped
        from the recording — which is exactly what happened the first time.
        """
        if node is None:
            return None
        # A cell's accessible name *is* the value being read. Locating it by
        # that name produces an artifact that only works for the record it was
        # recorded against, so those rungs are withheld for reads whose
        # content is their name.
        return await self._build_ladder(
            node, before, exclude_own_text=reading and node.value is None
        )

    async def record(
        self,
        *,
        tool: str,
        node: UiNode | None,
        args: Mapping[str, Any],
        typed_text: str | None,
        secret_ref: str | None,
        before: Observation,
        after: Observation,
        duration_ms: int,
        read_value: str | None,
        target: TargetSpec | None = None,
    ) -> None:
        self._counter += 1
        step_id = f"s{self._counter}"
        action = {
            "navigate": ActionType.NAVIGATE,
            "click": ActionType.CLICK,
            "type": ActionType.TYPE,
            "select": ActionType.SELECT,
            "read": ActionType.READ,
        }[tool]

        if node is not None and target is None:
            self._counter -= 1
            self.skipped.append(
                f"{tool} on {node.role} {node.name!r}: no locator identified it uniquely"
            )
            return

        value = self._value_ref(tool, args, typed_text, secret_ref)
        output = (
            OutputBinding(
                name=args["output_name"],
                extract=ExtractFrom.TEXT,
                transform=Transform.MONEY if MONEY.match((read_value or "").strip()) else Transform.STRIP,
            )
            if tool == "read"
            else None
        )

        self.steps.append(
            RecordedStep(
                step=Step(
                    id=step_id,
                    intent=self._intent(tool, node, args, secret_ref, target),
                    action=action,
                    frame_path=list(node.frame_path) if node else [],
                    target=target,
                    value=value,
                    output=output,
                    postcondition=self._postcondition(
                        tool, node, before, after, value, target
                    ),
                    risk=self._risk(tool, node, after),
                    timing=Timing(
                        observed_ms_p50=duration_ms,
                        timeout_ms=max(5_000, duration_ms * 10),
                    ),
                ),
                node_name=node.name if node else None,
                typed_text=typed_text,
                output_name=args.get("output_name"),
                ladder_counts={
                    c.strategy.value: (c.matches_at_record or 0)
                    for c in (target.ladder if target else [])
                },
            )
        )

    # ------------------------------------------------------- locator ladder

    async def _build_ladder(
        self, node: UiNode, observation: Observation, *, exclude_own_text: bool = False
    ) -> TargetSpec | None:
        """Every applicable rung, in preference order, each with its count.

        Deliberately keeps rungs that matched zero or many. A rung recorded as
        matching nothing is evidence — it tells a reviewer that the preferred
        strategy was tried and does not work on this surface, which is exactly
        the situation on a field whose label is table text.
        """
        frame_path = list(node.frame_path)
        candidates: list[TargetCandidate] = []
        usable_name = node.name and not exclude_own_text

        if usable_name:
            candidates.append(
                TargetCandidate(
                    strategy=TargetStrategy.A11Y_ROLE_NAME, role=node.role, name=node.name
                )
            )
            if node.name_source.value == "label":
                candidates.append(
                    TargetCandidate(
                        strategy=TargetStrategy.LABEL_TEXT, role=node.role, name=node.name
                    )
                )

        if near := self._near_text_candidate(node, observation):
            candidates.append(near)

        if usable_name:
            candidates.append(
                TargetCandidate(
                    strategy=TargetStrategy.EXACT_TEXT, role=node.role, name=node.name
                )
            )

        if native := self.surface.native_locator(node.ref):
            candidates.append(native)

        scored: list[TargetCandidate] = []
        for candidate in candidates:
            matches = await self.surface.find(candidate, frame_path)
            scored.append(candidate.model_copy(update={"matches_at_record": len(matches)}))

        winner = next((c for c in scored if c.matches_at_record == 1), None)
        if winner is None:
            return None

        return TargetSpec(
            primary=scored[0],
            fallbacks=scored[1:],
            recorded=RecordedEvidence(
                winning_strategy=winner.strategy,
                candidates_seen=1,
                bbox=node.bbox,
                a11y_snippet=self._snippet(
                    node,
                    observation,
                    anchor=next(
                        (
                            c.anchor
                            for c in scored
                            if c.strategy is TargetStrategy.NEAR_TEXT and c.anchor
                        ),
                        None,
                    ),
                    exclude_own_text=exclude_own_text,
                ),
            ),
        )

    def _near_text_candidate(
        self, node: UiNode, observation: Observation
    ) -> TargetCandidate | None:
        """Anchor on neighbouring text, with the ordinal among same-role peers.

        This is the rung that carries the legacy case. It is built from the
        same containment relationship the surface exposes as `container_ref`,
        so it means the same thing on a surface with no DOM.
        """
        anchor = self._anchor_for(node, observation)
        if anchor is None:
            return None
        peers = sorted(
            (
                n
                for n in observation.nodes
                if n.frame_path == node.frame_path
                and n.role == node.role
                and (
                    n.container_ref == anchor.container_ref
                    if anchor.container_ref is not None
                    else anchor.order <= n.order <= anchor.order + 8
                )
            ),
            key=lambda n: n.order,
        )
        if node not in peers:
            return None
        return TargetCandidate(
            strategy=TargetStrategy.NEAR_TEXT,
            role=node.role,
            anchor=anchor.name,
            index=peers.index(node),
        )

    @staticmethod
    def _looks_like_a_label(text: str) -> bool:
        """Screen-furniture text, as opposed to a record's data.

        Anchoring on data is the failure mode worth avoiding: in the
        sub-accounts table the cell nearest the balance holds the account's
        *nickname*, which differs per member. An artifact anchored there works
        for member 10001 and nobody else. Column headings and field captions
        are shouty and digit-free in this class of application, which is a
        crude test but a stable one.
        """
        return bool(text) and text.isupper() and not any(c.isdigit() for c in text)

    @classmethod
    def _anchor_for(cls, node: UiNode, observation: Observation) -> UiNode | None:
        same_frame = [n for n in observation.nodes if n.frame_path == node.frame_path]
        in_container = [
            n
            for n in same_frame
            if n.name
            and n.order < node.order
            and node.container_ref is not None
            and n.container_ref == node.container_ref
        ]
        if in_container:
            labels = [n for n in in_container if cls._looks_like_a_label(n.name or "")]
            return labels[0] if labels else in_container[-1]
        preceding = [n for n in same_frame if n.name and n.order < node.order]
        if not preceding:
            return None
        labels = [n for n in preceding if cls._looks_like_a_label(n.name or "")]
        return labels[-1] if labels else preceding[-1]

    @classmethod
    def _snippet(
        cls,
        node: UiNode,
        observation: Observation,
        *,
        anchor: str | None = None,
        exclude_own_text: bool = False,
    ) -> str:
        """Describe where the element sits, without quoting anyone's data.

        The evidence is meant to let a reviewer judge the locator. Quoting the
        surrounding row would put a member's balance into an artifact bound
        for a git repository — the same leak the locator rules just closed,
        through a field nobody was looking at.
        """
        container = (
            observation.by_ref(node.container_ref)
            if node.container_ref is not None
            else None
        )
        holds_data = bool(container and container.name and DATA_LIKE.search(container.name))
        if exclude_own_text or holds_data:
            where = f" anchored on {anchor!r}" if anchor else ""
            return f"{container.role if container else 'group'}{where} -> {node.role}"
        if container and container.name:
            return f'{container.role} "{container.name[:80]}" -> {node.role}'
        return f'{node.role} "{node.name or ""}" (name_source={node.name_source.value})'

    # ------------------------------------------------------- inferred parts

    def _value_ref(self, tool, args, typed_text, secret_ref) -> ValueRef | None:
        if secret_ref is not None:
            return ValueRef(secret_ref=secret_ref)
        if tool == "navigate":
            return ValueRef(literal=args["url"])
        if tool == "type":
            return ValueRef(literal=typed_text or "")
        if tool == "select":
            return ValueRef(literal=str(args.get("option", "")))
        return None

    def _postcondition(
        self,
        tool,
        node,
        before: Observation,
        after: Observation,
        value: ValueRef | None,
        target: TargetSpec | None = None,
    ) -> Condition:
        """Infer a checkpoint from what actually changed.

        Only obvious, checkable things: a value landed in the field, a frame
        moved, or distinctive text appeared. If nothing observable changed, the
        postcondition asserts the target is still there — weak, but honest, and
        visible as weak to a reviewer.
        """
        if tool in ("type", "select") and value is not None:
            from ..schema import ValueEquals

            return ValueEquals(expected=value)

        if tool == "read":
            # Never check a read against the value it read. "1,234.56" is data
            # that differs per invocation; the stable thing is the column
            # heading beside it.
            anchor = self._anchor_text(target, node, after)
            if anchor:
                return TextPresent(
                    text=anchor, frame_path=list(node.frame_path) if node else []
                )
            return NodePresent(
                role=node.role if node else "generic",
                frame_path=list(node.frame_path) if node else [],
            )

        moved = self._frame_that_moved(before, after)
        if moved is not None:
            path, url = moved
            return UrlMatches(pattern=self._canonical(url), frame_path=list(path))

        if new_text := self._distinctive_new_text(before, after, node):
            return TextPresent(
                text=new_text, frame_path=list(node.frame_path) if node else []
            )

        if node is not None and node.name:
            return NodePresent(
                role=node.role, name=node.name, frame_path=list(node.frame_path)
            )
        return NodePresent(role=node.role if node else "generic")

    @staticmethod
    def _frame_that_moved(
        before: Observation, after: Observation
    ) -> tuple[tuple[str, ...], str] | None:
        for key, url in after.frame_urls.items():
            if before.frame_urls.get(key) != url:
                return tuple(key.split("/")) if key else (), url
        return None

    @staticmethod
    def _distinctive_new_text(before, after, node) -> str | None:
        scope = list(node.frame_path) if node else []
        old = {
            n.name
            for n in before.nodes
            if n.name and (not scope or n.frame_path == scope)
        }
        fresh = [
            n.name
            for n in after.nodes
            if n.name and n.name not in old and (not scope or n.frame_path == scope)
        ]
        # Prefer a stable-looking label over a value: "CURRENT BALANCE" is a
        # checkpoint, "1,234.56" is data that changes per invocation.
        labels = [t for t in fresh if t.isupper() and not any(c.isdigit() for c in t)]
        return (labels or fresh or [None])[0]

    @staticmethod
    def _canonical(url: str) -> str:
        """Drop scheme and host so the pattern survives a different deployment.

        ``http://app:5000/members/detail?mbr=10001`` becomes
        ``*/members/detail?mbr=10001``. Concrete values in the path are turned
        into ``{param}`` placeholders later, once the contract is known.
        """
        without_scheme = url.split("://", 1)[-1]
        parts = without_scheme.split("/", 1)
        return f"*/{parts[1]}" if len(parts) > 1 and parts[1] else "*"

    @staticmethod
    def _anchor_text(
        target: TargetSpec | None, node, observation: Observation
    ) -> str | None:
        """The near-text anchor the locator itself uses, if it has one."""
        if target is not None:
            for candidate in target.ladder:
                if candidate.strategy is TargetStrategy.NEAR_TEXT and candidate.anchor:
                    return candidate.anchor
        if node is not None and node.container_ref is not None:
            container = observation.by_ref(node.container_ref)
            if container and container.name:
                return container.name
        return None

    def _risk(self, tool, node, after: Observation) -> Risk:
        if tool in ("read", "observe"):
            return Risk.SAFE_REVERSIBLE
        label = (node.name or "").upper() if node else ""
        if any(label == l for l in self.rules.irreversible_labels):
            return Risk.IRREVERSIBLE
        import fnmatch

        for url in after.frame_urls.values():
            if any(fnmatch.fnmatchcase(url, r) for r in self.rules.irreversible_routes):
                return Risk.IRREVERSIBLE
        if tool == "click":
            return Risk.STATE_CHANGING
        return Risk.SAFE_REVERSIBLE

    @staticmethod
    def _intent(tool, node, args, secret_ref, target=None) -> str:
        anchor = None
        if target is not None:
            anchor = next(
                (
                    c.anchor
                    for c in target.ladder
                    if c.strategy is TargetStrategy.NEAR_TEXT and c.anchor
                ),
                None,
            )
        if tool == "read" and anchor:
            # Naming the value here would put the data in the artifact's
            # human-readable line as surely as putting it in a locator.
            where = f"the {node.role} beside {anchor!r}"
        elif node and node.name:
            where = f"{node.role} {node.name!r}"
        elif node:
            where = f"the {node.role}"
        else:
            where = ""
        if tool == "navigate":
            return f"Navigate to {args['url']}"
        if tool == "type":
            what = f"the {secret_ref} credential" if secret_ref else "the supplied value"
            return f"Type {what} into {where or 'the field'}"
        if tool == "select":
            return f"Select {args.get('option')!r} in {where or 'the dropdown'}"
        if tool == "read":
            return f"Read {args['output_name']} from {where or 'the screen'}"
        return f"Click {where or 'the control'}"

    # --------------------------------------------------- building the artifact

    def build_capability(
        self,
        *,
        capability_id: str,
        proposal: Mapping[str, Any],
        run_id: str,
        model: str,
        transcript_sha256: str,
        known_outcomes: Sequence[BusinessOutcome] = (),
        known_recoveries: Sequence[RecoveryRule] = (),
        title: str | None = None,
    ) -> Capability:
        """Validate the model's proposed contract, then parameterise the run.

        The model proposes; the recorder verifies. An input it named but never
        typed is dropped, an output it named but never read is dropped, and an
        output it read but forgot to declare is added anyway — because what
        the run actually did is the evidence, and the summary is only a claim
        about it.
        """
        bindings = self._bind_inputs(proposal.get("inputs") or [])
        steps = [self._parameterise(rs.step, bindings) for rs in self.steps]

        inputs = [
            InputParam(
                name=name,
                type=ParamType.STRING,
                pattern=self._infer_pattern(value),
                required=True,
                sensitivity=Sensitivity.INTERNAL,
                description=str(meta.get("description") or f"Value for {name}."),
                example=value,
            )
            for name, (value, meta) in bindings.items()
        ]

        outputs = self._build_outputs(proposal.get("outputs") or [], steps)
        content_frame = self._content_frame()

        return Capability(
            schema_version="1.0",
            id=capability_id,
            version="1.0.0",
            title=(title or proposal.get("summary") or capability_id)[:120],
            description=str(proposal.get("summary") or f"Recorded capability: {self.goal}"),
            approval=Approval(),  # draft: nothing unattended until it is verified
            target=TargetBinding(
                vendor_product=self.vendor_product,
                product_version=self.product_version,
                tenant_id=self.tenant_id,
                entry_point=self.entry_url,
                surface_kind=self.surface_kind,
            ),
            contract=Contract(inputs=inputs, outputs=outputs),
            outcomes=list(known_outcomes),
            recoveries=list(known_recoveries),
            steps=steps,
            success_condition=self._success_condition(steps, content_frame),
            provenance=Provenance(
                model=model,
                run_id=run_id,
                transcript_sha256=transcript_sha256,
                recorded_at=datetime.now(timezone.utc),
                goal=self.goal,
                recorder_version="1.0",
            ),
        )

    # ------------------------------------------------------ parameterisation

    def _bind_inputs(
        self, proposed: Sequence[Mapping[str, Any]]
    ) -> dict[str, tuple[str, Mapping[str, Any]]]:
        """Match each proposed input to a value the run actually typed.

        A name with no corresponding keystroke is a claim the run does not
        support, so it is dropped rather than becoming a parameter that
        nothing ever substitutes.
        """
        typed = [
            rs.typed_text
            for rs in self.steps
            if rs.typed_text and rs.step.value and rs.step.value.literal is not None
        ]
        bindings: dict[str, tuple[str, Mapping[str, Any]]] = {}
        unclaimed = list(dict.fromkeys(typed))

        for item in proposed:
            name = str(item.get("name", "")).strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                self.skipped.append(f"input {name!r}: not a usable parameter name")
                continue
            example = str(item.get("example") or "")
            value = example if example in unclaimed else (unclaimed[0] if unclaimed else None)
            if value is None:
                self.skipped.append(f"input {name!r}: nothing in the run typed it")
                continue
            unclaimed.remove(value)
            bindings[name] = (value, item)
        return bindings

    def _parameterise(
        self, step: Step, bindings: Mapping[str, tuple[str, Mapping[str, Any]]]
    ) -> Step:
        """Replace concrete values with parameter references, everywhere.

        Not only in the typed value: the same member number reappears in the
        URL the search redirected to, so the checkpoint has to be
        parameterised too, or the artifact only ever works for member 10001.
        """
        replacements = {value: name for name, (value, _) in bindings.items()}
        if not replacements:
            return step

        value = step.value
        if value is not None and value.literal is not None:
            if value.literal in replacements:
                value = ValueRef(param=replacements[value.literal])
            else:
                value = ValueRef(literal=self._templatise(value.literal, replacements))

        return step.model_copy(
            update={
                "value": value,
                "postcondition": self._templatise_condition(
                    step.postcondition, replacements
                ),
            }
        )

    @staticmethod
    def _templatise(text: str, replacements: Mapping[str, str]) -> str:
        for concrete, name in replacements.items():
            if concrete:
                text = text.replace(concrete, "{" + name + "}")
        return text

    def _templatise_condition(self, condition, replacements):
        kind = condition.kind
        if kind == "url_matches":
            return condition.model_copy(
                update={"pattern": self._templatise(condition.pattern, replacements)}
            )
        if kind in ("text_present", "text_absent"):
            return condition.model_copy(
                update={"text": self._templatise(condition.text, replacements)}
            )
        if kind in ("node_present", "node_absent") and condition.name:
            return condition.model_copy(
                update={"name": self._templatise(condition.name, replacements)}
            )
        if kind == "value_equals":
            ref = condition.expected
            if ref.literal is not None and ref.literal in replacements:
                return condition.model_copy(
                    update={"expected": ValueRef(param=replacements[ref.literal])}
                )
            return condition
        if kind in ("all_of", "any_of"):
            return condition.model_copy(
                update={
                    "conditions": [
                        self._templatise_condition(c, replacements)
                        for c in condition.conditions
                    ]
                }
            )
        return condition

    # -------------------------------------------------------------- outputs

    def _build_outputs(
        self, proposed: Sequence[Mapping[str, Any]], steps: Sequence[Step]
    ) -> list[OutputField]:
        produced = {s.output.name: s.id for s in steps if s.output is not None}
        described = {
            str(o.get("name")): str(o.get("description") or "") for o in proposed
        }

        for name in described:
            if name not in produced:
                self.skipped.append(f"output {name!r}: no read step produced it")

        return [
            OutputField(
                name=name,
                type=ParamType.STRING,
                from_step=step_id,
                sensitivity=self._output_sensitivity(name),
                description=described.get(name)
                or f"Value read from the screen at step {step_id}.",
            )
            for name, step_id in produced.items()
        ]

    @staticmethod
    def _output_sensitivity(name: str) -> Sensitivity:
        """Default to regulated for anything that reads like money or an account.

        Conservative on purpose: over-tagging costs a redacted line in the
        evidence, under-tagging writes a customer's balance into a git
        repository. The artifact is a draft so a reviewer can loosen it
        deliberately.
        """
        lowered = name.lower()
        if any(k in lowered for k in ("balance", "amount", "account", "ssn", "card")):
            return Sensitivity.PII
        return Sensitivity.INTERNAL

    # ----------------------------------------------------------- conditions

    def _content_frame(self) -> str:
        for rs in reversed(self.steps):
            if rs.step.frame_path:
                return rs.step.frame_path[-1]
        return ""

    def _success_condition(self, steps: Sequence[Step], content_frame: str) -> Condition:
        """The last navigation checkpoint, plus proof each output was on screen.

        Reusing checkpoints the run actually verified keeps the success
        condition honest, rather than inventing a fresh claim nobody checked.
        """
        parts: list[Condition] = []
        last_nav = next(
            (
                s.postcondition
                for s in reversed(steps)
                if s.postcondition.kind == "url_matches"
            ),
            None,
        )
        if last_nav is not None:
            parts.append(last_nav)
        for step in steps:
            if step.output is not None and step.postcondition.kind == "text_present":
                parts.append(step.postcondition)
        if not parts:
            parts.append(steps[-1].postcondition)
        return parts[0] if len(parts) == 1 else AllOf(conditions=parts)

    @staticmethod
    def _infer_pattern(value: str) -> str | None:
        return f"^[0-9]{{{len(value)}}}$" if value.isdigit() else None
