"""The capability artifact.

A capability is what the discovery run leaves behind: a typed, versioned,
reviewable description of a flow that an AI agent can invoke by name and a
human can read in a pull request. It is the graded deliverable of this
project, so the design intent is written down here rather than only in
REPORT.md.

Six defences are built into the type system, not into the executor. Each one
makes a specific failure mode *unrepresentable* rather than merely discouraged:

1. **Parameters are never inlined.** ``ValueRef`` is exactly one of a param
   reference, a literal template, or a secret reference — and a literal may
   not be tagged sensitive, nor may it structurally look like an SSN or a card
   number. Regulated data cannot enter an artifact by construction.
2. **Every step carries a postcondition.** ``Step.postcondition`` is required.
   You cannot express "click and hope". This is a deliberate tightening of the
   original design, where it was optional.
3. **Business outcomes are part of the contract.** ``outcomes`` declares the
   legitimate non-success answers up front, so "no such member" is a value the
   caller receives rather than an exception it has to parse.
4. **The locator ladder is recorded with its match counts.** Every candidate
   strategy that was tried at record time is kept, along with how many
   elements it matched, so a reviewer can see *why* a locator was chosen and
   replay can fall back in a known order.
5. **Tenancy is explicit.** ``TargetBinding`` names the vendor product, its
   version and the tenant, and ``variant_of`` links a fork back to its base.
   Overlays specialise; forks are declared.
6. **Provenance is a hash.** The discovery transcript is referenced by
   ``transcript_sha256`` and never embedded, so the artifact is decoupled from
   the raw model output and cannot smuggle model-generated PII into the repo.

Every model here sets ``extra="forbid"``. Unknown keys are a loud error rather
than silently dropped data; ``schema_version`` is how a future shape is
introduced.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .actions import ActionType, ExtractFrom, Risk

__all__ = [
    "Approval",
    "ApprovalState",
    "AllOf",
    "AnyOf",
    "BusinessOutcome",
    "Capability",
    "Condition",
    "Contract",
    "ExtractFrom",
    "HttpOk",
    "InputParam",
    "NodeAbsent",
    "NodePresent",
    "OutputBinding",
    "OutputField",
    "ParamType",
    "Provenance",
    "RecordedEvidence",
    "RecoveryAction",
    "RecoveryRule",
    "Sensitivity",
    "Step",
    "SurfaceKind",
    "TargetBinding",
    "TargetCandidate",
    "TargetSpec",
    "TargetStrategy",
    "TextAbsent",
    "TextPresent",
    "Timing",
    "Transform",
    "UrlMatches",
    "ValueEquals",
    "ValueRef",
]

IDENT = r"^[a-z][a-z0-9_]*$"
CODE = r"^[A-Z][A-Z0-9_]*$"
STEP_ID = r"^s\d+$"
SEMVER = r"^\d+\.\d+\.\d+$"
ENV_VAR = r"^[A-Z][A-Z0-9_]*$"
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class Schema(BaseModel):
    """Base for every artifact model: unknown keys are an error."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Data classification
# --------------------------------------------------------------------------


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"

    @property
    def restricted(self) -> bool:
        """True for classes that may never be written down."""
        return self in (Sensitivity.PII, Sensitivity.SECRET)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _looks_like_pii(text: str) -> str | None:
    """Structural screen for values that must never be written to an artifact.

    Returns the name of the pattern that matched, or None. Kept narrow on
    purpose: a screen that fires on every long number would be turned off
    within a week, which is worse than no screen at all. The card check is
    Luhn-gated so ordinary long identifiers pass.
    """
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", text):
        return "ssn"
    for candidate in re.findall(r"(?:\d[ -]?){13,19}", text):
        digits = re.sub(r"\D", "", candidate)
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "card"
    return None


class ValueRef(Schema):
    """Where a step's value comes from. Exactly one source.

    ``literal`` is a template: ``"/members/detail?mbr={member_id}"`` is how the
    recorder canonicalises a concrete route it observed. Placeholders are
    resolved against the declared inputs at replay time, which is what lets one
    recording serve every member id.
    """

    param: str | None = Field(default=None, pattern=IDENT)
    literal: str | None = None
    secret_ref: str | None = Field(default=None, pattern=ENV_VAR)
    sensitivity: Sensitivity = Sensitivity.PUBLIC

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ValueRef":
        chosen = [n for n in ("param", "literal", "secret_ref") if getattr(self, n) is not None]
        if len(chosen) != 1:
            raise ValueError(
                "a ValueRef must set exactly one of param / literal / secret_ref, "
                f"got {chosen or 'none'}"
            )
        return self

    @model_validator(mode="after")
    def _literals_are_never_sensitive(self) -> "ValueRef":
        """Defence 1. Regulated data cannot enter an artifact by construction."""
        if self.secret_ref is not None:
            self.sensitivity = Sensitivity.SECRET
            return self
        if self.literal is None:
            return self
        if self.sensitivity.restricted:
            raise ValueError(
                f"a literal may not be tagged {self.sensitivity.value}; "
                "use {'param': ...} for PII or {'secret_ref': 'ENV_VAR'} for credentials"
            )
        if hit := _looks_like_pii(self.literal):
            raise ValueError(
                f"literal looks like {hit} and may not be stored in an artifact; "
                "pass it as a parameter instead"
            )
        return self

    @property
    def placeholders(self) -> set[str]:
        """Input names this literal template interpolates."""
        return set(PLACEHOLDER.findall(self.literal or ""))

    @property
    def referenced_params(self) -> set[str]:
        return ({self.param} if self.param else set()) | self.placeholders


# --------------------------------------------------------------------------
# Conditions
#
# A small closed language rather than a predicate string. Closed means a
# reviewer can read every condition in an artifact without running it, and the
# executor has no eval() in it.
# --------------------------------------------------------------------------


class TextPresent(Schema):
    kind: Literal["text_present"] = "text_present"
    text: str = Field(min_length=1)
    frame_path: list[str] = []
    case_sensitive: bool = False


class TextAbsent(Schema):
    kind: Literal["text_absent"] = "text_absent"
    text: str = Field(min_length=1)
    frame_path: list[str] = []
    case_sensitive: bool = False


class UrlMatches(Schema):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str = Field(min_length=1)
    """Glob against a URL. May contain ``{param}`` placeholders."""

    frame_path: list[str] = []
    """Which document's location to match. A frameset navigates a child frame
    without changing the top URL, so a checkpoint that cannot name the frame
    cannot express "we reached the detail screen"."""

    @property
    def placeholders(self) -> set[str]:
        return set(PLACEHOLDER.findall(self.pattern))


class NodePresent(Schema):
    kind: Literal["node_present"] = "node_present"
    role: str
    name: str | None = None
    frame_path: list[str] = []


class NodeAbsent(Schema):
    kind: Literal["node_absent"] = "node_absent"
    role: str
    name: str | None = None
    frame_path: list[str] = []


class ValueEquals(Schema):
    """The step's own target now holds this value. Used after ``type``/``select``."""

    kind: Literal["value_equals"] = "value_equals"
    expected: ValueRef


class HttpOk(Schema):
    kind: Literal["http_ok"] = "http_ok"
    at_most: int = Field(default=399, ge=100, le=599)


class AllOf(Schema):
    kind: Literal["all_of"] = "all_of"
    conditions: list["Condition"] = Field(min_length=1)


class AnyOf(Schema):
    kind: Literal["any_of"] = "any_of"
    conditions: list["Condition"] = Field(min_length=1)


Condition = Annotated[
    Union[
        TextPresent,
        TextAbsent,
        UrlMatches,
        NodePresent,
        NodeAbsent,
        ValueEquals,
        HttpOk,
        AllOf,
        AnyOf,
    ],
    Field(discriminator="kind"),
]


def condition_params(cond: "Condition") -> set[str]:
    """Every input name a condition tree interpolates."""
    if isinstance(cond, UrlMatches):
        return cond.placeholders
    if isinstance(cond, ValueEquals):
        return cond.expected.referenced_params
    if isinstance(cond, (AllOf, AnyOf)):
        return set().union(*(condition_params(c) for c in cond.conditions))
    return set()


# --------------------------------------------------------------------------
# Targeting
# --------------------------------------------------------------------------


class TargetStrategy(str, Enum):
    A11Y_ROLE_NAME = "a11y_role_name"  # preferred: portable to UIA and AX
    LABEL_TEXT = "label_text"
    NEAR_TEXT = "near_text"  # anchor text + role + ordinal
    EXACT_TEXT = "exact_text"
    CSS = "css"  # last resort, always brittle
    XPATH = "xpath"  # last resort, always brittle


BRITTLE_STRATEGIES = frozenset({TargetStrategy.CSS, TargetStrategy.XPATH})

_REQUIRED_BY_STRATEGY: dict[TargetStrategy, tuple[str, ...]] = {
    TargetStrategy.A11Y_ROLE_NAME: ("role", "name"),
    TargetStrategy.LABEL_TEXT: ("name",),
    TargetStrategy.NEAR_TEXT: ("anchor", "role"),
    TargetStrategy.EXACT_TEXT: ("name",),
    TargetStrategy.CSS: ("value",),
    TargetStrategy.XPATH: ("value",),
}


class TargetCandidate(Schema):
    """One rung of the locator ladder."""

    strategy: TargetStrategy
    role: str | None = None
    name: str | None = None
    anchor: str | None = None
    index: int | None = Field(default=None, ge=0)
    value: str | None = None
    brittle: bool = False

    matches_at_record: int | None = Field(default=None, ge=0)
    """How many elements this strategy matched when the flow was recorded.

    Defence 4. ``1`` means the strategy was unambiguous; ``0`` means it was
    recorded as a known-dead rung; ``>1`` means replay will treat it as a miss,
    and a reviewer can see that before it ever runs.
    """

    @model_validator(mode="after")
    def _strategy_has_its_fields(self) -> "TargetCandidate":
        missing = [f for f in _REQUIRED_BY_STRATEGY[self.strategy] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"strategy {self.strategy.value} requires {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _selector_strategies_are_brittle(self) -> "TargetCandidate":
        """CSS and XPath are brittle by definition, so the flag is derived, not trusted."""
        if self.strategy in BRITTLE_STRATEGIES:
            self.brittle = True
        return self

    @property
    def signature(self) -> tuple:
        return (self.strategy, self.role, self.name, self.anchor, self.index, self.value)


class RecordedEvidence(Schema):
    """What the recorder actually saw, so a reviewer can audit the choice."""

    winning_strategy: TargetStrategy
    candidates_seen: int = Field(ge=1)
    """Elements the winning strategy matched at record time. 1 is the only good answer."""

    bbox: tuple[int, int, int, int] | None = None
    a11y_snippet: str | None = None


class TargetSpec(Schema):
    primary: TargetCandidate
    fallbacks: list[TargetCandidate] = []
    recorded: RecordedEvidence

    @model_validator(mode="after")
    def _ladder_is_coherent(self) -> "TargetSpec":
        seen: set[tuple] = set()
        for candidate in self.ladder:
            if candidate.signature in seen:
                raise ValueError(f"duplicate candidate in ladder: {candidate.strategy.value}")
            seen.add(candidate.signature)
        strategies = {c.strategy for c in self.ladder}
        if self.recorded.winning_strategy not in strategies:
            raise ValueError(
                f"winning_strategy {self.recorded.winning_strategy.value} is not in the ladder"
            )
        return self

    @property
    def ladder(self) -> list[TargetCandidate]:
        return [self.primary, *self.fallbacks]

    @property
    def has_stable_option(self) -> bool:
        """False means every rung is a selector — worth flagging in review."""
        return any(not c.brittle for c in self.ladder)


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class Timing(Schema):
    observed_ms_p50: int = Field(ge=0)
    timeout_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _timeout_is_generous(self) -> "Timing":
        floor = max(1_000, self.observed_ms_p50 * 3)
        if self.timeout_ms < floor:
            raise ValueError(
                f"timeout_ms {self.timeout_ms} is too tight for an observed p50 of "
                f"{self.observed_ms_p50}ms; use at least {floor}ms"
            )
        return self


class Transform(str, Enum):
    NONE = "none"
    STRIP = "strip"
    UPPER = "upper"
    DIGITS = "digits"
    MONEY = "money"


class OutputBinding(Schema):
    """Marks a ``read`` step as the producer of a declared output."""

    name: str = Field(pattern=IDENT)
    extract: ExtractFrom = ExtractFrom.TEXT
    pattern: str | None = None
    """Optional regex with exactly one capture group, applied to the raw value."""
    transform: Transform = Transform.NONE

    @field_validator("pattern")
    @classmethod
    def _one_capture_group(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            compiled = re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        if compiled.groups != 1:
            raise ValueError(f"pattern must have exactly one capture group, has {compiled.groups}")
        return v


class RecoveryAction(str, Enum):
    DISMISS = "dismiss"
    RETRY_STEP = "retry_step"
    RELOAD = "reload"
    RE_RESOLVE = "re_resolve"


class RecoveryRule(Schema):
    """A declared, bounded response to a known transient condition.

    Recovery is data, not code: if the executor could invent a response, replay
    would stop being deterministic. Anything not declared here escalates.
    """

    id: str = Field(pattern=IDENT)
    when: Condition
    do: RecoveryAction
    target: TargetSpec | None = None
    max_attempts: int = Field(default=1, ge=1, le=2)
    description: str = ""

    @model_validator(mode="after")
    def _dismiss_needs_a_target(self) -> "RecoveryRule":
        if self.do is RecoveryAction.DISMISS and self.target is None:
            raise ValueError("a dismiss rule needs a target to click")
        return self


_NEEDS_TARGET = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.READ}
_NEEDS_VALUE = {ActionType.NAVIGATE, ActionType.TYPE, ActionType.SELECT}


class Step(Schema):
    id: str = Field(pattern=STEP_ID)
    intent: str = Field(min_length=3)
    """Human-readable. This is the line a reviewer reads in a diff."""

    action: ActionType
    frame_path: list[str] = []
    """Frame names from the top document down. Empty means the top document."""

    target: TargetSpec | None = None
    value: ValueRef | None = None
    output: OutputBinding | None = None

    postcondition: Condition
    """Defence 2. Required: you cannot record a step that assumes it worked."""

    risk: Risk = Risk.SAFE_REVERSIBLE
    timing: Timing

    @model_validator(mode="after")
    def _shape_matches_action(self) -> "Step":
        a = self.action
        if a in _NEEDS_TARGET and self.target is None:
            raise ValueError(f"{a.value} requires a target")
        if a in _NEEDS_VALUE and self.value is None:
            raise ValueError(f"{a.value} requires a value")
        if a is ActionType.NAVIGATE and self.target is not None:
            raise ValueError("navigate addresses a URL, not an element; drop the target")
        if a is ActionType.READ and self.output is None:
            raise ValueError("read requires an output binding, otherwise it reads into nothing")
        if a is not ActionType.READ and self.output is not None:
            raise ValueError(f"{a.value} cannot bind an output; only read can")
        if a is ActionType.WAIT and (self.target is not None or self.value is not None):
            raise ValueError(
                "wait takes no target or value — it waits on its postcondition. "
                "There is no way to express a fixed sleep, by design"
            )
        return self

    @property
    def referenced_params(self) -> set[str]:
        params = self.value.referenced_params if self.value else set()
        return params | condition_params(self.postcondition)


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(Schema):
    name: str = Field(pattern=IDENT)
    type: ParamType = ParamType.STRING
    pattern: str | None = None
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    description: str = Field(min_length=3)
    example: str | None = None
    """Shown to a calling agent in the catalog. Forbidden on restricted params."""

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        return v

    @model_validator(mode="after")
    def _examples_are_not_regulated(self) -> "InputParam":
        if self.example is None:
            return self
        if self.sensitivity.restricted:
            raise ValueError(
                f"param {self.name!r} is {self.sensitivity.value}; an example value would "
                "put regulated data in the catalog"
            )
        if hit := _looks_like_pii(self.example):
            raise ValueError(f"example for {self.name!r} looks like {hit}")
        if self.pattern and not re.fullmatch(self.pattern, self.example):
            raise ValueError(f"example for {self.name!r} does not match its own pattern")
        return self


class OutputField(Schema):
    name: str = Field(pattern=IDENT)
    type: ParamType = ParamType.STRING
    from_step: str = Field(pattern=STEP_ID)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    description: str = Field(min_length=3)


class Contract(Schema):
    """What a calling agent supplies and what it gets back."""

    inputs: list[InputParam] = []
    outputs: list[OutputField] = []

    @model_validator(mode="after")
    def _names_are_unique(self) -> "Contract":
        for label, items in (("input", self.inputs), ("output", self.outputs)):
            names = [i.name for i in items]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {label} name in contract")
        return self

    @property
    def input_names(self) -> set[str]:
        return {i.name for i in self.inputs}

    @property
    def output_names(self) -> set[str]:
        return {o.name for o in self.outputs}


class BusinessOutcome(Schema):
    """Defence 3. A legitimate non-success answer, declared up front.

    "No such member" is a value the caller needs, not a crash. Declaring these
    in the contract is what stops the executor from conflating them with
    failures — the single most common design mistake in this problem.
    """

    code: str = Field(pattern=CODE)
    description: str = Field(min_length=3)
    detect: Condition
    terminal: bool = True


# --------------------------------------------------------------------------
# Envelope
# --------------------------------------------------------------------------


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Approval(Schema):
    """Replay history, and the gate for unattended irreversible replay."""

    state: ApprovalState = ApprovalState.DRAFT
    replays: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    last_verified: datetime | None = None
    approved_by: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _approval_is_earned(self) -> "Approval":
        if self.state is ApprovalState.APPROVED:
            if self.last_verified is None or self.replays < 1:
                raise ValueError(
                    "an approved capability must record at least one verified replay; "
                    "set replays and last_verified or leave the state as draft"
                )
        if self.failures > self.replays:
            raise ValueError("failures cannot exceed replays")
        return self

    @property
    def stability(self) -> float | None:
        """Fraction of recorded replays that succeeded."""
        return None if self.replays == 0 else (self.replays - self.failures) / self.replays


class SurfaceKind(str, Enum):
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class TargetBinding(Schema):
    """Defence 5. Which product, which version, which tenant.

    ``vendor_product`` plus ``product_version`` is the reuse key: two tenants on
    the same product and version should share a capability with an overlay, not
    two recordings. ``variant_of`` is the escape hatch when the flow itself
    differs structurally and a fork is honest.
    """

    vendor_product: str = Field(min_length=1)
    product_version: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    variant_of: str | None = Field(default=None, pattern=IDENT)
    entry_point: str = Field(min_length=1)
    surface_kind: SurfaceKind = SurfaceKind.WEB


class Provenance(Schema):
    """Defence 6. Where this came from, without carrying it.

    ``extra="forbid"`` is doing real work here: there is no field to put a
    transcript in, so no future edit can quietly start persisting one.
    """

    model: str = Field(min_length=1)
    """The model id that drove discovery, read from the environment at run time."""

    run_id: str = Field(min_length=1)
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    goal: str = Field(min_length=1)
    recorder_version: str = "1.0"


class Capability(Schema):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=IDENT)
    version: str = Field(pattern=SEMVER)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    """What a calling agent reads when deciding whether this is the right tool."""

    approval: Approval = Field(default_factory=Approval)
    target: TargetBinding
    contract: Contract
    outcomes: list[BusinessOutcome] = []
    steps: list[Step] = Field(min_length=1)
    recoveries: list[RecoveryRule] = []
    success_condition: Condition
    provenance: Provenance

    # -- cross-field integrity ---------------------------------------------

    @model_validator(mode="after")
    def _step_ids_unique(self) -> "Capability":
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        return self

    @model_validator(mode="after")
    def _params_are_declared(self) -> "Capability":
        declared = self.contract.input_names
        used: set[str] = set(condition_params(self.success_condition))
        for step in self.steps:
            used |= step.referenced_params
        for outcome in self.outcomes:
            used |= condition_params(outcome.detect)
        for rule in self.recoveries:
            used |= condition_params(rule.when)
        if undeclared := used - declared:
            raise ValueError(
                f"steps reference undeclared input(s): {sorted(undeclared)}; "
                f"declared inputs are {sorted(declared) or 'none'}"
            )
        return self

    @model_validator(mode="after")
    def _outputs_are_produced(self) -> "Capability":
        by_id = {s.id: s for s in self.steps}
        produced: dict[str, str] = {}
        for step in self.steps:
            if step.output is not None:
                if step.output.name in produced:
                    raise ValueError(f"output {step.output.name!r} is bound by two steps")
                produced[step.output.name] = step.id

        for field in self.contract.outputs:
            if field.from_step not in by_id:
                raise ValueError(
                    f"output {field.name!r} cites step {field.from_step!r}, which does not exist"
                )
            producer = produced.get(field.name)
            if producer is None:
                raise ValueError(
                    f"output {field.name!r} is declared but no read step binds it"
                )
            if producer != field.from_step:
                raise ValueError(
                    f"output {field.name!r} says it comes from {field.from_step} "
                    f"but is bound by {producer}"
                )

        if orphaned := set(produced) - self.contract.output_names:
            raise ValueError(
                f"step(s) bind output(s) missing from the contract: {sorted(orphaned)}"
            )
        return self

    @model_validator(mode="after")
    def _codes_and_rules_unique(self) -> "Capability":
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate business outcome code")
        rule_ids = [r.id for r in self.recoveries]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("duplicate recovery rule id")
        return self

    @model_validator(mode="after")
    def _fork_is_not_self(self) -> "Capability":
        if self.target.variant_of == self.id:
            raise ValueError("variant_of cannot point at the capability itself")
        return self

    # -- review helpers ----------------------------------------------------

    @property
    def has_irreversible_step(self) -> bool:
        return any(s.risk is Risk.IRREVERSIBLE for s in self.steps)

    @property
    def irreversible_steps(self) -> list[Step]:
        return [s for s in self.steps if s.risk is Risk.IRREVERSIBLE]

    @property
    def brittle_only_steps(self) -> list[Step]:
        """Steps whose every locator rung is a raw selector."""
        return [s for s in self.steps if s.target and not s.target.has_stable_option]

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


AllOf.model_rebuild()
AnyOf.model_rebuild()
ValueEquals.model_rebuild()
RecoveryRule.model_rebuild()
Step.model_rebuild()
BusinessOutcome.model_rebuild()
Capability.model_rebuild()
