# PROJECT_SPEC.md — Computer-Use Automation System

> **How to use this file:** drop it in an empty repo, then tell Claude Code:
> `Read PROJECT_SPEC.md in full. Build it phase by phase. Do not skip ahead. After each phase, run the acceptance checks and stop for my review.`

---

## 0. Your role

You are building a take-home submission for an engineering team that has stated they are grading
**judgment and integration**, not feature volume. Every file you write should be defensible in an
interview. When you hit a fork the spec doesn't resolve, pick the simpler option, implement it, and
add a one-line note to `DECISIONS.md` explaining the trade-off. Never leave a `TODO` where the spec
asks for a real mechanism.

Two hard rules:

1. **Work in phases.** Complete a phase, run its acceptance checks, commit, then stop and report.
   Do not scaffold all eight phases at once.
2. **Run what you write.** Every phase has commands that must actually execute successfully. Do not
   report a phase complete based on reading the code.

---

## 1. What the system does

An LLM drives a real UI once to accomplish a natural-language goal. The successful run is recorded
as a typed, versioned **capability artifact**. That artifact is then replayed **deterministically,
with no model in the decision loop**, by a production executor that returns a structured result. When
the executor cannot safely proceed, it escalates to a human who takes control of the *same live
browser session*, fixes the situation, and hands control back so the run resumes.

The through-line: *the model discovers, the artifact becomes a reusable capability, deterministic
replay is how an agent invokes it in production.*

---

## 2. Stack and environment

| Concern | Choice |
|---|---|
| Language | Python 3.11 |
| Package manager | `uv` |
| Browser automation | Playwright (Python), **headed** Chromium |
| Typed models | Pydantic v2 |
| Discovery LLM | **OpenAI**, model from `OPENAI_MODEL` env var — never hardcode a model name |
| Operator console + capability API | FastAPI + Uvicorn |
| Target app | Flask (local, in-repo) |
| Logging | `structlog`, JSONL output |
| Tests | pytest |
| Runtime | **Docker Compose** |

### Docker layout

Base image: the official Playwright Python image. Add `xvfb`, `x11vnc`, `novnc`, `websockify`.

Three services:

- **`app`** — the Flask target application, port `5000`.
- **`cua`** — the automation runtime. Runs `Xvfb :99 -screen 0 1280x900x24`, launches Playwright
  Chromium with `headless=False` and `DISPLAY=:99`. Also runs the operator console on `8080` and the
  capability API on `8081`.
- **VNC bridges inside `cua`:**
  - `x11vnc -display :99 -viewonly -rfbport 5900` → noVNC on `6080` (monitor view)
  - `x11vnc -display :99 -rfbport 5901` → noVNC on `6081` (interactive control)

Two separate VNC endpoints is deliberate: view-only enforcement lives on the server, not in a noVNC
URL parameter a user could edit. The operator console embeds `6080` normally and swaps the iframe to
`6081` only once the lease has actually been transferred. Document this reasoning.

Everything must come up with `docker compose up` and no host-side Python install.

---

## 3. Repository layout

```
README.md
REPORT.md
DECISIONS.md
policy.yaml
pyproject.toml
docker-compose.yml
Dockerfile
.env.example
.gitignore
Makefile

apps/corebank/            # the target application
  app.py  state.py  seed.py  inject.py
  templates/  static/

src/cua/
  cli.py
  schema/       artifact.py  actions.py  results.py  observation.py
  surface/      base.py  web_playwright.py  registry.py
  discovery/    agent.py  prompts.py  tools.py  recorder.py
  replay/       executor.py  resolver.py  detectors.py  recovery.py  fallback.py
  policy/       gate.py  redaction.py  config.py
  escalation/   lease.py  intervention.py  operator_app.py  human_capture.py
  evidence/     logger.py  capture.py  manifest.py
  catalog/      registry.py  api.py  toolspec.py

artifacts/
  lookup_member_balance.v1.json
  open_subaccount.v1.json
  overlays/summit-cu.json

evidence/
  discovery-lookup-balance/
  replay-success/
  replay-business-outcome/
  replay-recoverable/
  replay-hard-failure/
  replay-escalation/

tests/
```

---

## 4. Build order

**Build replay before discovery.** Hand-write an artifact JSON, get it replaying, *then* write the
agent that produces artifacts in that shape. The reverse order lets the model's output dictate the
schema, which is backwards when the schema is the graded deliverable.

---

## Phase 1 — The target application

Build a deliberately hostile legacy-style web app: **"MERIDIAN CoreBank — Servicing Console."**

### Legacy characteristics (all required)

- **Frameset-based.** `/app` renders `<frameset>` with a `nav` frame and a `content` frame. Real
  `<frameset>`/`<frame>`, not CSS panes — cross-frame traversal is the point.
- Table-based layout, nested three deep in places.
- ASP.NET-style generated ids: `ctl00_ContentPlaceHolder1_txtMbrNo`, `ctl00_..._btnSearch`.
- **No `data-testid` anywhere.** Adding one is a spec violation.
- Inline `onclick="__doPostBack(...)"` handlers on some controls.
- Labels are plain `<td>` text adjacent to inputs, not `<label for=...>`, on at least two fields.
  This is what forces a real locator ladder.
- A couple of `<font>` tags and a spacer GIF, for flavour.

### Flow

1. `GET /login` — username/password form. Any username, password `demo1234`. Sets a session cookie.
2. `GET /app` — frameset shell.
3. `content → /members/search` — "MEMBER NUMBER" field + SEARCH button.
4. `content → /members/detail?mbr=NNNNN` — member summary with a sub-accounts table containing
   account type, account number, and **CURRENT BALANCE**.
5. `content → /members/subaccount/new?mbr=NNNNN` — multi-field form (account type dropdown, nickname,
   initial deposit, funding source).
6. `content → /members/subaccount/review` — review screen with a **CONFIRM** button.
7. `content → /members/subaccount/confirm` — confirmation with a new account number.

### Seed data

Members `10001`–`10005`. Give `10003` a `restricted` flag that triggers a permission denial on
detail view. Balances are fake, obviously synthetic values.

### Fault injection

`POST /admin/inject {"fault": "...", "once": true}` sets a server-side flag on the session.
Supported faults:

| Fault | Behaviour |
|---|---|
| `not_found` | Search returns "NO MATCHING MEMBER RECORD FOUND" |
| `validation` | Sub-account form rejects with "INITIAL DEPOSIT MUST BE AT LEAST $25.00" |
| `permission_denied` | Detail view returns "YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD" |
| `session_expired` | Next request bounces to `/login` with "YOUR SESSION HAS TIMED OUT" |
| `slow` | Next page sleeps 6 seconds |
| `interstitial` | Next page renders a modal div: "SYSTEM MAINTENANCE NOTICE" + DISMISS button |
| `app_error` | Next page returns HTTP 500 with a stack-trace-looking page |

`POST /admin/reset` clears all flags and reseeds.

### Second tenant variant

Mount the same app under `/t/summit-cu/` with:
- Different branding and colours
- "ACCOUNT NUMBER" instead of "MEMBER NUMBER"
- An extra required "BRANCH" dropdown on the sub-account form
- Different frame `name` attributes

This is your stand-in for two institutions on the same vendor product.

### Acceptance

- `docker compose up app`, then manually walk login → search `10001` → detail → new sub-account →
  review → confirm.
- Each fault demonstrably fires.
- `/t/summit-cu/` works and differs as described.

---

## Phase 2 — Schema

Pure Pydantic v2, no dependencies on anything else in the repo. Think hardest here; this is the most
heavily graded file in the submission.

### `schema/artifact.py`

```python
class TargetStrategy(str, Enum):
    A11Y_ROLE_NAME = "a11y_role_name"      # preferred
    LABEL_TEXT     = "label_text"
    NEAR_TEXT      = "near_text"           # anchor text + role + ordinal
    EXACT_TEXT     = "exact_text"
    CSS            = "css"                 # last resort, always brittle=True
    XPATH          = "xpath"

class TargetCandidate(BaseModel):
    strategy: TargetStrategy
    role: str | None = None
    name: str | None = None
    anchor: str | None = None
    index: int | None = None
    value: str | None = None
    brittle: bool = False

class RecordedEvidence(BaseModel):
    winning_strategy: TargetStrategy
    candidates_seen: int          # how many elements matched at record time
    bbox: tuple[int, int, int, int] | None
    a11y_snippet: str | None

class TargetSpec(BaseModel):
    primary: TargetCandidate
    fallbacks: list[TargetCandidate] = []
    recorded: RecordedEvidence
```

Steps:

```python
class Risk(str, Enum):
    SAFE_REVERSIBLE = "safe_reversible"
    STATE_CHANGING  = "state_changing"
    IRREVERSIBLE    = "irreversible"

class Step(BaseModel):
    id: str                       # "s1", "s2", ...
    intent: str                   # human-readable; what a reviewer reads
    action: ActionType            # navigate|click|type|select|read|wait|assert
    frame_path: list[str] = []    # frame names from top document down
    target: TargetSpec | None
    value: ValueRef | None        # literal | param ref | never raw PII
    output: OutputBinding | None  # for read actions
    postcondition: Condition | None
    risk: Risk = Risk.SAFE_REVERSIBLE
    timing: Timing                # observed_ms_p50, timeout_ms
```

`ValueRef` is either `{"param": "member_id"}` or `{"literal": "..."}`. **A literal may never carry a
value tagged as sensitive** — enforce this with a Pydantic validator so PII cannot enter an artifact
by construction. This is a point you will make in REPORT.md.

Contract and outcomes:

```python
class Sensitivity(str, Enum):
    PUBLIC = "public"; INTERNAL = "internal"; PII = "pii"; SECRET = "secret"

class InputParam(BaseModel):
    name: str; type: str; pattern: str | None
    required: bool = True
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    description: str

class OutputField(BaseModel):
    name: str; type: str; from_step: str
    sensitivity: Sensitivity
    description: str

class BusinessOutcome(BaseModel):
    code: str                     # MEMBER_NOT_FOUND, PERMISSION_DENIED
    description: str
    detect: Condition
    terminal: bool = True
```

Top level:

```python
class Capability(BaseModel):
    schema_version: Literal["1.0"]
    id: str
    version: str                  # semver
    title: str
    description: str              # what a calling agent reads
    approval: Approval            # state: draft|approved, replays, failures, last_verified
    target: TargetBinding         # vendor_product, product_version, tenant_id,
                                  # variant_of, entry_point, surface_kind
    contract: Contract            # inputs + outputs
    outcomes: list[BusinessOutcome]
    steps: list[Step]
    success_condition: Condition
    provenance: Provenance        # model, run_id, transcript_sha256, recorded_at
```

`Provenance` stores a **hash** of the discovery transcript, never the transcript itself — the artifact
must be decoupled from the raw model output.

### `schema/results.py`

A discriminated union, not a boolean plus exceptions:

```python
class Success(BaseModel):
    status: Literal["success"]
    outputs: dict[str, Any]
    steps_executed: int
    duration_ms: int
    recoveries: list[RecoveryRecord]
    evidence_ref: str

class BusinessOutcomeResult(BaseModel):
    status: Literal["business_outcome"]
    code: str
    message: str
    at_step: str
    evidence_ref: str

class Failure(BaseModel):
    status: Literal["failure"]
    kind: FailureKind             # TARGET_NOT_FOUND | CHECKPOINT_FAILED | TIMEOUT
                                  # | POLICY_BLOCKED | SURFACE_ERROR | HUMAN_TIMEOUT
                                  # | UNRECOVERABLE_CONDITION
    at_step: str
    expected: str
    observed: str
    evidence_ref: str

ReplayResult = Annotated[Success | BusinessOutcomeResult | Failure, Field(discriminator="status")]
```

### Acceptance

- `Capability.model_json_schema()` emits valid JSON Schema.
- Round-trip test: a hand-written artifact JSON loads, serialises, and compares equal.
- Validator test: an artifact with a `pii`-tagged literal is rejected.

---

## Phase 3 — Surface abstraction

`surface/base.py` defines a Protocol that everything downstream speaks. **No Playwright type may
appear outside `surface/`.** That boundary is your answer to the heterogeneity question.

```python
class Surface(Protocol):
    kind: SurfaceKind                                  # web | legacy_web | desktop
    async def observe(self) -> Observation: ...
    async def resolve(self, spec: TargetSpec, frame_path: list[str]) -> Handle | None: ...
    async def act(self, action: Action, handle: Handle | None) -> ActionResult: ...
    async def snapshot(self) -> Snapshot: ...          # screenshot + a11y tree + url
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
```

`Observation` is surface-agnostic: a list of `UiNode` (role, name, value, enabled, focused, bbox,
frame_path) plus page title, URL, and any detected banners or dialogs. A desktop implementation would
populate `UiNode` from UIA/AX instead of Chromium's a11y tree, and nothing above this layer changes.

`WebPlaywrightSurface` builds `Observation` primarily from Playwright's accessibility snapshot,
walking every frame in the frameset and tagging each node with its `frame_path`. Fall back to a DOM
walk only for nodes the a11y tree omits, and mark those nodes as `a11y_visible=False`.

### Acceptance

- Script that opens the target app, prints the observation as a flat table, and shows nodes in both
  the `nav` and `content` frames with correct `frame_path`.
- `grep -r "playwright" src/cua --include="*.py" | grep -v "surface/"` returns nothing.

---

## Phase 4 — Deterministic replay

The heart of the submission.

### Resolver (`replay/resolver.py`)

Try `primary`, then each fallback in recorded order. Rules:

- If a strategy matches **more than one** node, treat it as a miss and move down the ladder — never
  silently take the first. Ambiguity is a failure mode, not a coin flip.
- If a fallback wins instead of the primary, record a `DriftSignal` in evidence with both strategies.
  Replay still succeeds; drift is reported, not swallowed.
- If nothing matches within the step timeout, return `Failure(TARGET_NOT_FOUND)` with the observed
  nodes of the same role for debugging.

### Waiting

No fixed sleeps anywhere. Wait on observable conditions: network idle, a required node appearing, or
a postcondition becoming true. Timeouts come from `step.timing.timeout_ms`.

### Detectors (`replay/detectors.py`)

Detection is **not** per-step ad-hoc checks. It's a standing detector set run as a guard against every
observation, before and after each action:

- `ErrorBannerDetector` — configured text patterns in known regions
- `ModalDetector` — unexpected dialog/overlay present
- `AuthWallDetector` — the login page appeared mid-flow, i.e. session died
- `HttpErrorDetector` — 4xx/5xx page
- `DeclaredOutcomeDetector` — matches the artifact's own `outcomes` list

### Classification and handling

| Class | Handling |
|---|---|
| **Recoverable** | Handled in-loop, logged as `RecoveryRecord`, not surfaced to the caller. Transient slowness → bounded retry with backoff (max 2). Known interstitial → dismiss per a declared recovery rule. Stale frame → re-resolve once. |
| **Business outcome** | Matched against the artifact's declared `outcomes`. Return `BusinessOutcomeResult` cleanly. This is a legitimate answer, not a crash — the brief names conflating these as the most common design mistake. |
| **Hard failure** | Stop. Return `Failure` with step id, expected, observed, and an evidence reference. |
| **Escalation** | Session expiry and other unrecoverable-but-human-fixable states hand off to Phase 6. |

Session expiry specifically: re-authenticate automatically **only** if re-auth is allowlisted in
`policy.yaml`; otherwise escalate. Justify that choice in REPORT.md.

### Executor loop

```
for step in artifact.steps:
    await lease.acquire_or_wait()            # Phase 6
    obs = await surface.observe()
    if cond := detectors.scan(obs): handle per table above
    policy.check(action, context)            # Phase 7 — the single chokepoint
    handle = resolver.resolve(step.target, step.frame_path)
    result = await surface.act(action, handle)
    await verify(step.postcondition)         # never assume the click worked
    evidence.record(step, obs, result)
verify(artifact.success_condition)
return Success(outputs=...)
```

### Acceptance

Hand-write `artifacts/lookup_member_balance.v1.json` and demonstrate all four paths:

```bash
make replay ARTIFACT=lookup_member_balance PARAMS='{"member_id":"10001"}'   # Success
make replay ARTIFACT=lookup_member_balance PARAMS='{"member_id":"99999"}'   # MEMBER_NOT_FOUND
make inject FAULT=slow && make replay ...                                   # recovered Success
make inject FAULT=app_error && make replay ...                              # Failure, debuggable
```

---

## Phase 5 — Discovery agent

### Loop

`observe → decide → act`, with OpenAI function calling. Tools exposed to the model:

- `observe()` → compact text rendering of the `Observation` (role, name, value, frame — *not* raw HTML)
- `click(node_ref)`, `type(node_ref, text)`, `select(node_ref, option)`, `navigate(url)`
- `read(node_ref, output_name)` — marks a value for extraction
- `done(summary)` / `stuck(reason)`

`node_ref` is a stable index the surface assigns per observation. The model never writes CSS
selectors — it names things the way an operator would, which is exactly what makes the recorded
artifact portable to a surface with no DOM.

Stopping conditions: max 25 steps, 5-minute wall clock, three consecutive no-progress observations
(identical observation hash), or an explicit `stuck` call. `stuck` routes to escalation (Phase 6).

Send screenshots to the model only on the first observation and after a `stuck` signal — text
observations elsewhere. Note the cost/robustness trade-off in REPORT.md.

### Recorder (`discovery/recorder.py`)

Runs alongside the agent, not inside it. For each accepted action it:

1. Captures the node's a11y role/name and builds the **full candidate ladder** (not just the one that
   worked) by testing each strategy against the live observation and counting matches.
2. Records `candidates_seen` per strategy — the uniqueness evidence.
3. Records observed latency as `observed_ms_p50`, setting `timeout_ms` to a generous multiple.
4. Infers a postcondition where obvious (typed value equals input; navigation changed URL; expected
   text appeared).
5. **Parameterises literals.** If a typed value matches a declared input, store a `param` ref. Also
   canonicalise routes: `/members/detail?mbr=10001` → `/members/detail?mbr={member_id}`.
6. Classifies risk per action: reads and searches are `safe_reversible`, form submissions are
   `state_changing`, anything on a confirmation route is `irreversible`.

The agent proposes inputs/outputs in its `done()` summary; the recorder validates them against what
was actually read and typed. Emit the artifact as `draft`.

### Acceptance

A real run, with `/evidence/discovery-lookup-balance/` containing `transcript.jsonl`, `steps.jsonl`,
`screenshots/`, and a generated `artifact.json` that then replays clean via Phase 4.

Then record `open_subaccount` too — it contains an `irreversible` step, which you need for Phase 7.

---

## Phase 6 — Escalation and control transfer

Graders single this out as the thing most submissions fake. Make it real.

### Lease (`escalation/lease.py`)

```python
class SessionLease(BaseModel):
    session_id: str
    holder: Literal["automation", "operator"]
    token: str                    # bearer token for the current holder
    since: datetime
    reason: str | None
    expires_at: datetime | None   # operator holds are time-boxed
```

Single-writer invariant: `Surface.act()` asserts the caller holds the lease. The executor awaits an
`asyncio.Event` while the operator holds it. An expired operator lease terminates the run as
`Failure(HUMAN_TIMEOUT)` — it never silently reverts to automation mid-flow.

### Intervention request

```python
class InterventionRequest(BaseModel):
    id: str; session_id: str
    capability_id: str; capability_version: str
    goal: str
    at_step: str; step_intent: str
    reason: InterventionReason    # STUCK_DISCOVERY | UNRECOVERABLE | RISKY_APPROVAL | POLICY_BLOCKED
    observed: str; expected: str
    screenshot_ref: str; a11y_snapshot_ref: str
    redacted_params: dict[str, str]
    created_at: datetime
```

### Operator console (`escalation/operator_app.py`)

FastAPI on `8080`. Minimal and deliberately plain — document it as an intentionally minimal UI over a
real mechanism:

- `GET /` — list of open intervention requests with full context
- `GET /session/{id}` — detail page: reason, screenshot, step intent, and the **monitor** noVNC iframe
  (`6080`, view-only)
- `POST /session/{id}/take` — flips the lease to `operator`, returns a token, page swaps the iframe to
  the **interactive** noVNC endpoint (`6081`)
- `POST /session/{id}/release` — flips back with an operator note; executor resumes
- `POST /session/{id}/abort` — terminates the run

### Capturing what the human did (`escalation/human_capture.py`)

`context.add_init_script` installs capture-phase listeners for `click`, `change`, and `submit` in
every frame, reporting through `context.expose_binding`. Each event becomes a `HumanStep` with role,
accessible name, frame path, and a **redacted** value (raw value kept only for `public` fields). These
land in the evidence log interleaved with automation steps, so the timeline shows exactly who did what.

### Resume semantics

On hand-back the executor does **not** blindly continue. It re-runs the pre-step guard and
re-evaluates the failed step's precondition. If still unsatisfied, it escalates again with an
incremented `escalation_count`; at 2 it hard-fails. Bounded, never a loop.

### Acceptance — the demo that matters

1. Start a replay of `lookup_member_balance`.
2. Inject `session_expired` mid-run.
3. Executor detects the auth wall, pauses, raises an intervention request.
4. Open `localhost:8080`, take control, log back in **through noVNC in the live browser**, release.
5. Executor resumes and returns `Success`.
6. `/evidence/replay-escalation/` shows the full interleaved timeline including the human's steps.

Record this as a short screen capture if you can — it's the single most convincing artifact in the
submission.

---

## Phase 7 — Policy and data handling

### Single chokepoint

`PolicyGate.check(action, context)` is called from exactly one place: `Surface.act()`. Nothing else
enforces anything. Being able to say "there is exactly one enforcement point" is worth more than a
longer rule list.

### `policy.yaml`

```yaml
allowlist:
  url_patterns:
    - "http://app:5000/login"
    - "http://app:5000/app"
    - "http://app:5000/members/**"
    - "http://app:5000/t/summit-cu/**"
  denied_patterns:
    - "**/admin/**"
  action_types: [navigate, click, type, select, read, wait, assert]

risk_rules:
  irreversible_routes: ["**/subaccount/confirm", "**/transfer/**"]
  irreversible_labels: ["CONFIRM", "SUBMIT TRANSFER", "DELETE"]

enforcement:
  discovery:
    irreversible: block          # the model must never execute an irreversible action
    state_changing: allow
  replay:
    irreversible: require_approved_artifact
    state_changing: allow
  reauth_allowed: true

redaction:
  patterns:
    ssn: '\b\d{3}-\d{2}-\d{4}\b'
    card: '\b(?:\d[ -]*?){13,19}\b'
    account: '\b\d{8,}\b'
```

**Justify the asymmetry in REPORT.md:** discovery is exploratory and the model is fallible, so it may
never execute an irreversible action — it stops at the review screen and escalates for approval.
Replay is a reviewed, versioned artifact where a human has already seen exactly which step is
irreversible, so it may proceed if approved.

### Redaction

A `Redactor` installed as a structlog processor, so redaction happens at the sink and cannot be
bypassed by a caller forgetting to call it. Driven by sensitivity tags plus the regex patterns above.
Screenshots of pages flagged as containing PII get the relevant regions blurred, or are stored with a
`contains_pii` marker and excluded from the committed evidence — pick one and document it.

Credentials come from env vars, are referenced in artifacts as `{"secret_ref": "APP_PASSWORD"}`, and
never appear in an artifact, log, or transcript. Add a test that greps all of `artifacts/` and
`evidence/` for the seeded password and fails if found.

### Acceptance

- Navigating to `/admin/inject` from the agent raises `Failure(POLICY_BLOCKED)`.
- Discovery hitting CONFIRM blocks and raises `RISKY_APPROVAL`.
- The secret-leak test passes.

---

## Phase 8 — Stretch goals, evidence, write-up

The brief caps stretch goals at "one or two" and explicitly does not reward breadth. Build these four
because each is a small extension of the core rather than a new surface — and **say exactly that in
REPORT.md** so it doesn't read as ignoring the instruction.

1. **Capability catalog** (`catalog/`). FastAPI on `8081`: `GET /capabilities` returns the artifacts as
   OpenAI-tool-shaped JSON Schema (Pydantic gives you this nearly free), `POST /capabilities/{id}/invoke`
   replays with typed args. Include a small script that lets a model discover and call one by name —
   it demonstrates the brief's own through-line back to them.
2. **Cross-tenant overlay.** `artifacts/overlays/summit-cu.json` overrides only locator names and adds
   the branch field. Show `lookup_member_balance` recorded against the base app replaying against
   `/t/summit-cu/` with the overlay applied, and failing without it. Overlays may override targets and
   values; a tenant needing *structural* change must fork with an explicit `variant_of` link.
3. **Approval and stability.** `make stability ARTIFACT=... N=10` replays N times, reports a flakiness
   score, and promotes `draft → approved` at a configured threshold. Unattended replay of irreversible
   steps requires `approved` — this is what makes the Phase 7 asymmetry enforceable.
4. **Bounded assisted fallback.** On `TARGET_NOT_FOUND` only, allow one policy-checked LLM call scoped
   to re-identifying *that single element* from the current observation. It may not choose a new
   action, skip a step, or run twice. Record every invocation as evidence. Frame it as a drift
   absorber with a hard blast radius.

### Evidence directories

Each contains `run.jsonl` (structured log), `result.json`, `screenshots/`, and `manifest.json`
(git SHA, artifact version, params redacted, timings, environment).

Required runs: discovery, replay-success, replay-business-outcome, replay-recoverable,
replay-hard-failure, replay-escalation.

### REPORT.md — exactly these seven headings

1. **Architecture** — the surface seam, why replay-first, single-process justification.
2. **Artifact schema** — the six defences: params never inlined; postconditions on every step;
   business outcomes declared in the contract; the recorded fallback ladder with match counts;
   `variant_of` plus overlays; provenance as a hash.
3. **Determinism & error handling** — locator ladder, ambiguity-is-a-miss, no fixed sleeps, standing
   detector set, the three-arm result contract, drift signals.
4. **Heterogeneity & multi-tenant** — why the a11y tree is the portable perception layer (it exists on
   UIA and AX too), what a `DesktopSurface` would implement, base/overlay/fork model, drift detection
   via fallback-win telemetry across tenants.
5. **Escalation & handoff** — the lease state machine, why one interactive VNC port instead of a URL
   parameter, human action capture, bounded resume.
6. **Safety** — single chokepoint, the discovery/replay asymmetry, redaction at the sink, and the
   limits: the allowlist is URL-shaped so it can't express "only this member's records," and noVNC
   control is all-or-nothing rather than scoped.
7. **Cuts** — the operator console is deliberately minimal; no desktop surface; no queue or
   multi-tenant plumbing (the brief explicitly says not to build it); single-process by choice.

### README.md

Setup, `.env.example` contents, and a copy-pasteable demo path: bring up compose → run discovery on a
goal → show the emitted artifact → replay it → replay the error case → trigger the escalation demo.
Include how to run everything except discovery with no API key.

---

## 5. Testing

- **Unit:** schema round-trip and validators, policy gate decisions, redactor, resolver ladder
  including the ambiguity rule, detector classification, result discriminated union.
- **Integration** (against the live Flask app): each of the four replay paths, the overlay replay, the
  escalation flow with a scripted operator.
- **Discovery:** marked `@pytest.mark.llm` and skipped without `OPENAI_API_KEY`.

`make test` runs everything except `llm`.

---

## 6. Do not

- Do not add `data-testid` to the target app.
- Do not let Playwright types escape `surface/`.
- Do not use fixed `sleep()` for synchronisation.
- Do not persist the raw model transcript inside an artifact.
- Do not build queues, workers, or multi-tenant infrastructure — the brief explicitly says this is not
  rewarded.
- Do not commit `.env`, API keys, or any evidence file containing the seeded password.
- Do not leave a `TODO` where the spec asks for a real mechanism. Mock deliberately, at a clean seam,
  and document it in `DECISIONS.md`.
