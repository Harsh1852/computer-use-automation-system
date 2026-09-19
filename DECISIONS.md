# DECISIONS.md

Running log of forks the spec left open, the option taken, and the trade-off.

## Phase 1 — target application

- **Fault flags are process-global, not session-scoped.** The spec says
  `POST /admin/inject` sets "a server-side flag on the session", but faults are
  armed out-of-band (`make inject`, curl, a test fixture) by a client that does
  not share a cookie jar with the browser being automated — a session-scoped
  flag would be unreachable from the only place that arms it. Trade-off: two
  concurrent runs would see each other's faults. Acceptable because the app is a
  single-process demo fixture, and it keeps the injection interface a one-liner.
- **One blueprint mounted twice, not two apps.** `summit-cu` is the same code
  with a different tenant config (labels, frame names, colours, an extra
  required BRANCH field). Two real codebases would model "two tenants on the
  same vendor product" less honestly than one codebase with per-tenant config,
  which is what the vendor actually ships.
- **Faults apply only to `/members/**` routes.** The frameset shell, the nav
  frame and sign-on stay stable so an armed fault lands on the servicing screen
  under test rather than on the shell that happens to load first.
- **`GET /members/subaccount/confirm` redirects to review instead of opening an
  account.** Only `POST` mutates. Avoids a browser refresh silently opening a
  second account, which would make the "irreversible" classification a lie.
- **Admin routes are mounted at the application root**, outside both tenant
  prefixes, so the policy allowlist can deny them with a single `**/admin/**`
  pattern.
- **`app` runs on `python:3.11-slim`, not the Playwright image.** The target
  application must not share a filesystem or Python environment with the thing
  automating it; the automation should have no privileged path in.

## Phase 2 — schema

Deviations from the original sketch, each one a deliberate tightening:

- **`Step.postcondition` is required, not optional.** The sketch typed it
  `Condition | None`. Making it mandatory means "click and hope" is
  unrepresentable, which is the stronger form of the stated defence
  "postconditions on every step". Cost: a hand-written artifact is more
  verbose. Worth it — every step now states how it knows it worked.
- **`wait` steps may carry no target and no value.** Their only content is the
  postcondition they wait on, so there is no field a fixed sleep could live
  in. The "no fixed sleeps" rule is enforced by the schema rather than by
  reviewer discipline.
- **`matches_at_record` added to `TargetCandidate`.** The sketch put a single
  `candidates_seen` on `RecordedEvidence`, but the recorder is specified to
  count matches *per strategy*. Both now exist: per-rung counts on each
  candidate, and the winning rung's count on the evidence block.
- **`css`/`xpath` candidates coerce `brittle` to True rather than rejecting
  `brittle: false`.** Coercion makes the invariant unconditional; rejecting
  would leave a window where a hand-edited artifact is briefly invalid instead
  of simply correct.
- **`RecoveryRule` added to the artifact.** Replay is specified to "dismiss per
  a declared recovery rule", so the declaration has to live somewhere
  reviewable. Recovery is data, not code: if the executor could invent a
  response, replay would stop being deterministic. `max_attempts` is capped at
  2 in the schema.
- **Structural PII screening on literals, Luhn-gated.** Tagging a literal
  `pii` is rejected, but a tag is a promise. A regex screen for SSN and card
  shapes is the enforcement. The card check requires a Luhn-valid 13–19 digit
  run so ordinary long identifiers pass — a screen that fires on every long
  number gets switched off within a week, which is worse than no screen.
- **`extra="forbid"` on every artifact model.** Unknown keys are a loud error,
  which is what makes "provenance has nowhere to put a transcript" an
  enforced property rather than a convention. The cost is that an older reader
  rejects a newer artifact outright; that is what `schema_version` is for.
- **Closed enums for `InputParam.type` and `OutputBinding.transform`.** The
  sketch had `type: str`. A closed set is what lets the catalog emit
  tool-shaped JSON Schema mechanically, and a free-form transform string would
  be an unreviewable code-injection seam.
- **Results carry `capability_id`, `capability_version` and `run_id` on all
  three arms**, plus `drift` on success and failure. A result that does not say
  which version produced it cannot be debugged, and fallback-win telemetry is
  the cross-tenant drift signal described in the write-up.
- **`schema/observation.py` landed with the schema, not with the surface.** It
  is pure Pydantic and the surface Protocol cannot be typed without it. `Handle`
  deliberately did *not* land here: a handle is an opaque, surface-owned
  reference and belongs in `surface/`.
- **Tests run in a `test` image, not on the host.** Keeps the "no host-side
  Python install" promise. Sources are bind-mounted so the suite reruns without
  a rebuild.

## Phase 3 — surface abstraction

- **Perception is computed by an injected scanner, not taken from the driver's
  accessibility snapshot.** Four things are needed at once: a role and an
  accessible name, coverage of *every* frame, a stable handle back to the
  element, and a shape a UIA/AX surface could fill identically. The driver's
  a11y snapshot gives neither frame traversal nor element mapping, and its
  ARIA-snapshot replacement returns a string. So `scan.js` computes the same
  information from the same inputs (ARIA attributes, label association,
  implicit roles) and returns descriptors alongside the elements they describe.
  Trade-off: roughly 200 lines of JS we own, against a perception layer that
  actually covers the target and can be mirrored on a desktop surface.
- **The locator ladder lives in `walk_ladder` in `surface/base.py`, not in each
  surface.** Surfaces implement `find(candidate) -> list[Handle]`; "try the
  primary, then fallbacks in order, ambiguity is a miss" is a rule about
  robustness, not about browsers. A desktop surface inherits determinism
  instead of reimplementing it slightly differently.
- **Only `css` and `xpath` reach the driver.** The four portable rungs resolve
  by filtering `UiNode`s in Python. This is what makes the ladder meaningful on
  a surface with no DOM: such a surface simply offers no brittle rungs.
- **Hidden nodes are dropped at the perception boundary.** A hidden
  `__VIEWSTATE` input is a textbox in the raw tree; leaving it in would inflate
  the match counts that "ambiguity is a miss" depends on and make unambiguous
  targets look ambiguous. Found by inspecting the real page, asserted in a test.
- **Layout-table chrome is suppressed.** A `<tr>` whose every cell just holds
  another table, and a `<td>` containing a table, are dropped: emitting them
  duplicates the text of everything inside and creates phantom anchors that
  `near_text` could latch onto. Cut the frameset observation from 20 nodes to 16.
- **A text leaf is emitted when its enclosing cell was dropped.** Found by a
  failing test: `YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD` sits in a `<font>`
  inside a layout cell we suppress, so the message was invisible to perception
  and no detector could ever have matched it.
- **`act()` waits for the `load` event after navigating actions.** Found by
  three failing tests: a click that navigates leaves subframes in flight, and a
  request from the *previous* action was still arriving and consuming an
  injected fault while the next action was being decided. Waiting on an
  observable event, not a duration.
- **`Handle` is opaque and carries provenance but no behaviour.** It records
  which rung won and how many nodes matched — enough to report drift — and has
  no `click`. If a handle exposed behaviour, every caller would start depending
  on the driver and the seam would leak.
- **`Gate` and `Lease` are Protocols with null-object defaults, wired into
  `act()` now.** The call sites are real from this phase on, so the policy and
  escalation phases supply implementations rather than inserting new
  enforcement points. `OpenGate` is a null object, not a stub for a missing
  mechanism.
- **`start`, `close` and `find` were added to the Protocol** beyond the six
  methods originally sketched. A protocol with no lifecycle cannot be
  constructed generically, and without `find` the ladder could not be shared.
- **`render_table` lives in `base.py`, not in the driver module.** Importing it
  into the CLI by its module name would put the driver's name outside
  `surface/` and fail the boundary check — caught by the test, not by review.
  It is a pure function of `Observation`, so the driver-free side is where it
  belonged anyway.
- **Playwright pinned to `1.55.0` with `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`.**
  The official image ships browser builds but not the Python package; pinning
  to the image tag keeps driver and browser revisions in agreement.
- **`UiNode` gained `name_source`, `order` and `container_ref`.** These are what
  let `near_text` and `label_text` be implemented off the observation rather
  than off the DOM. All three have UIA/AX analogues — name provenance, tree
  order, and the nearest grouping ancestor.

## Phase 4 — deterministic replay

- **Detector precedence is the design, and declared outcomes win.** The
  permission-denied screen is simultaneously a declared business outcome, an
  HTTP 403 and an angry red banner. Ordering `DeclaredOutcomeDetector` first is
  how the code refuses to conflate "the answer is no" with "the run broke".
- **Detectors run inside the wait loop, ahead of the postcondition.** When the
  app answers "no such member", `s6`'s postcondition will never become true.
  Checking detectors first turns that into a returned answer instead of a
  checkpoint timeout twenty-five seconds later.
- **Drift means "resolved differently than when recorded", not "a fallback
  won".** This artifact deliberately records `a11y_role_name` as the preferred
  rung for a field that has no accessible name, so a fallback wins on every
  single run. Defining drift as `winning_index > 0` would emit a signal every
  time and bury the real one. It is now `winner.strategy !=
  recorded.winning_strategy`, plus a separate signal when a rung's match count
  moves away from `matches_at_record`.
- **`Observation` gained `frame_urls` and `frame_statuses`.** A frameset
  navigates a child frame without changing the top URL, so `page.url` cannot
  express "we reached the detail screen" and a single `http_status` cannot say
  *which* document returned 500. Both were found by running the acceptance
  paths: the first made `s6`'s checkpoint unwritable, the second produced
  `status 500 at http://app:5000/app`, which points a debugger at the wrong
  page. `http_status` is now the worst status among frames on screen.
- **Document status is tracked per URL, not "most recent".** Two frames load
  concurrently, so "the last document response" is a race — whether an error
  page is visible would depend on which frame finished second.
- **Slow absorption is measured across the whole step, not the wait.** First
  implementation measured only the checkpoint poll and reported nothing,
  because the injected six-second delay lands *inside* the click, while the
  browser waits for the page it triggered. A run that silently absorbs six
  seconds is exactly the swallowing this record exists to prevent. Threshold is
  `3 × observed_ms_p50` floored at 1s — derived from the artifact's own timing
  evidence rather than a global constant.
- **`FailureKind.CONTRACT_VIOLATION` added.** A caller passing `member_id:
  "abc"` is not `POLICY_BLOCKED` (the gate refused a well-formed action) and
  not `UNRECOVERABLE_CONDITION` (the app did something). The fix belongs to the
  caller, and the taxonomy should say so. It fails before the browser opens.
- **Only `safe_reversible` steps are retried.** Retrying a state-changing click
  is how one confirmation becomes two accounts. The bound is on the declared
  class of the action, not on a guess about whether the last attempt landed.
- **Polling, with a justification.** "No fixed sleeps" means never waiting
  *instead of* checking. The executor polls observations every 150ms, bounded
  by the step's own `timeout_ms`, and the only other sleep is the retry backoff
  the spec asks for. A desktop surface would poll the same way.
- **Recovery is data, not code**, with three bounds: per-rule `max_attempts`
  (schema-capped at 2), a global per-run budget, and no recovery at all for
  conditions without a declared rule.
- **Evidence redacts by declared sensitivity; the caller does not.** The
  balance is tagged `pii`, so `result.json` and `run.jsonl` carry
  `<redacted>` while the returned `Success.outputs` carries the real value.
  The caller asked for it; the repository is not where it belongs.
- **A business outcome exits 0, a failure exits 1.** Shell semantics should
  match the result contract: "no such member" is an answer.
- **`replay/conditions.py` is a module the layout did not name.** One evaluator
  serves postconditions, outcome detectors and recovery triggers, so "how do I
  know I got there" is defined once. It uses `fnmatchcase`, because `fnmatch`
  normalises case via `os.path.normcase` and a checkpoint must not match
  differently on a Windows runner than on Linux.

## Phase 5 - discovery agent

- **The model never sees markup, ids, or selectors.** Its entire vocabulary
  for "which element" is a `ref` from the last observation. Given HTML it
  would reach for `#ctl00_ContentPlaceHolder1_txtMbrNo`, which is generated
  from the ASP.NET control hierarchy and renamed by any server-side refactor
  - and a flow recorded against markup stops being portable the moment a
  surface has none.
- **The model never sees credentials.** It is told to type literal
  placeholder tokens; the tool layer substitutes them on the way to the
  browser. The transcript therefore contains the placeholder, and the
  recorder emits a `secret_ref` without having to recognise a password after
  the fact. An unset variable is an error, never a guess.
- **Unnamed fields are rendered with a `near=` hint**, computed from the same
  containment relationship `near_text` uses. What the model reads and what the
  recorder writes down therefore cannot disagree.
- **The ladder is scored before the action, not after.** Found by a failing
  test: scoring after a click that navigates measures the *next* page, every
  rung returns zero matches, and the step is silently dropped. Two of seven
  steps vanished from the recording that way.
- **A `read` is never located by the data it reads.** Also found by a test:
  the balance cell's accessible name *is* the balance, so `a11y_role_name`
  won with one match and became the primary rung - an artifact that works for
  member 10001 and nobody else. Name-based rungs are withheld for reads whose
  content is their name, leaving `near_text` and the native rung.
- **Anchors prefer labels over data.** The cell nearest the balance holds the
  account *nickname*, which differs per member. Candidate anchors that look
  like screen furniture (uppercase, no digits) are preferred over the merely
  nearest.
- **Recorded evidence describes structure, not content.** The `a11y_snippet`
  quoted the whole containing row, which put a balance into an artifact bound
  for a git repository - the same leak the locator rules had just closed,
  through a field nobody was looking at.
- **The outcome taxonomy is per-product configuration, not discovery.** A
  happy-path run cannot observe "no such member" while successfully finding a
  member. `discovery/catalogue.py` holds the known outcomes and transient
  conditions for the vendor product and attaches them to every capability
  recorded against it. Stated plainly rather than presented as discovered.
- **The model proposes a contract; the recorder verifies it.** An input it
  named but never typed is dropped, an output it named but never read is
  dropped, and an output it read but forgot to declare is added. What the run
  did is the evidence; the summary is only a claim about it.
- **Outputs default to `pii` when they read like money or an account.**
  Over-tagging costs a redacted line in the evidence; under-tagging writes a
  customer's balance into a repository. The artifact is emitted as `draft` so
  a reviewer can loosen it deliberately.
- **Screenshots go to the model on the first observation and after `stuck`,
  nowhere else.** Text observations are far cheaper and are what the artifact
  is built from; an image every turn would make the model's competence depend
  on pixels no desktop surface reproduces the same way.
- **A scripted-model test drives the real browser through the whole pipeline.**
  It exercises every line of the recorder without spending a token, and ends
  by replaying the emitted artifact against a *different member* than the one
  recorded - which is what proves the parameterisation rather than assuming it.

### Phase 5 - found by the genuine model runs

Four defects the scripted test could not have caught, because they only
appear when a real model chooses its own path:

- **Supervised recording is an explicit, off-by-default mode.** Told not to
  act irreversibly, the model walked the whole sub-account form and then
  called `stuck` at the review screen - correct, and exactly the
  RISKY_APPROVAL case. But a capability whose entire purpose *is* the
  confirmation has to be recorded once. `--supervised` swaps that one prompt
  rule and nothing else; unattended discovery still refuses. Once the policy
  gate exists it enforces the same asymmetry rather than trusting the prompt.
- **A `read` must not count toward the no-progress stop.** The supervised run
  clicked CONFIRM, then read two values off the confirmation screen, and was
  killed as "3 consecutive identical observations". A read deliberately
  leaves the page unchanged; only navigate/click/type/select claim to change
  it, so only those count.
- **Parameters bind on an exact match or not at all.** The binder used to
  fall back to "take the next unclaimed typed value", which bound
  `account_type` to the nickname field and `nickname` to the deposit field.
  Every replay would have typed the wrong value into the wrong box. A dropped
  parameter is a visible gap; a mis-bound one is a landmine. Selects are now
  bindable too, which is what let all five inputs bind correctly.
- **A select records what the control holds, not the label used to pick it.**
  The dropdown shows `10001-C01 (CHECKING)` while its value is `10001-C01`.
  Recording the label made the `value_equals` postcondition compare two
  different things, so the step failed its own checkpoint immediately after
  succeeding.

And one in replay, surfaced by the same run:

- **An action that will not run is not automatically a broken surface.**
  A dropdown option that does not exist times out against a perfectly healthy
  page. `SURFACE_ERROR` now means the browser or transport is gone; a timeout
  is `TIMEOUT`, and anything else that fails while the surface still responds
  is `UNRECOVERABLE_CONDITION`. Telling an operator the surface crashed sends
  them to debug the wrong thing.

Known limitation, left deliberately: the `open_subaccount` recording assumes
the funding account exists for the member being serviced. Replaying it for a
member with no checking account fails at that step. That is a real property
of the flow rather than a bug, it is why the artifact is emitted as `draft`,
and it is the sort of latent assumption human review exists to catch.

## Phase 6 - escalation and control transfer

- **The lease is the authority; the UI follows it.** `Surface.act()` asks the
  lease before every action, so "automation and the human both acted" is a
  `LeaseViolation` rather than a race to be avoided by convention. The console
  swaps its iframe to the interactive endpoint *because* the lease moved, not
  the other way round.
- **Two VNC servers on one display, not one server with a URL flag.**
  `x11vnc -viewonly` owns port 5900 (monitor, bridged to 6080); a second
  x11vnc without the flag owns 5901 (interactive, bridged to 6081). View-only
  is therefore a property of the socket, not of a `view_only=true` parameter
  anyone could delete from the address bar.
- **An expired operator hold terminates the run as HUMAN_TIMEOUT.** It never
  reverts to automation. If somebody took control because something was wrong
  and then walked away, the page is in whatever state they left it and the
  executor's model of where it is no longer matches reality.
- **Resume is bounded at two escalations.** On hand-back the executor re-runs
  the guard rather than continuing blindly, so an operator who did not
  actually fix the condition produces the same finding again - and the second
  time it stops instead of looping.
- **Operator hold time is excluded from step timeouts.** Found while building
  the demo: a step's `timeout_ms` measures the application, not the person. A
  ten-minute hold against a 25-second step would have guaranteed a checkpoint
  failure the instant control came back, punishing the run for the handoff
  that rescued it. The same exclusion applies to the slow-response signal - a
  five minute hold is not a five minute page load.
- **Human capture is gated on the lease, not on the listeners.** The
  capture-phase listeners stay installed after hand-back, so without a gate
  every automation click after a resume was logged as something a person did.
  Caught by reading the first demo's timeline: three automation actions were
  attributed to the operator.
- **The hand-back signal is armed before the request is published.** A real
  race, caught by a test that failed only in suite order: an operator fast
  enough to resolve an intervention the instant it appeared had that
  resolution wiped by the reset, hanging the run until timeout.
- **Captured values are shapes, never content.** A password entry records
  `PASSWORD`, `sensitive: true` and nothing else; other fields record
  `<redacted:N chars>`. Which control the operator touched explains the
  resume; what they typed into it does not need to be in a repository. The
  name of a clicked element is taken only from a short leaf - an earlier
  version grabbed a container's `innerText` and wrote the whole screen,
  including whatever record was on display, into the evidence log.
- **The console runs in the replay process.** It has to see the live lease and
  the live intervention; sharing those across processes would mean building
  the queue the brief explicitly says not to build.
- **`act_as_operator` exists for scripted operators only.** In production a
  person drives the session through VNC, which bypasses the Surface API
  entirely - bypassing it is what VNC *is*. The method is gated by the same
  lease check as everything else.

## Phase 7 - policy and data handling

- **One enforcement point, and it is checkable.** `PolicyGate.check()` is
  called from `Surface.act()` and nowhere else. A longer rule list spread over
  three call sites has three places to forget, and "the automation cannot
  reach /admin" is only as strong as the number of places it has to be true.
- **The gate does not trust the artifact's risk label.** An artifact is data;
  a recording that under-declared a CONFIRM button as safe should still be
  caught. The gate re-derives risk from the policy's route and label rules and
  takes the higher of declared and derived. Demonstrated: discovery was run in
  supervised mode, where the model had been *told* it could complete the
  confirmation, and the gate refused anyway.
- **Denials are evaluated first and independently.** A broader allow pattern
  cannot re-open something explicitly closed.
- **The current URL is checked, not only the destination.** If the application
  redirects somewhere unsanctioned, the next click stops the run rather than
  typing into whatever arrived.
- **Double-star is expanded explicitly rather than left to fnmatch.** fnmatch
  lets a single star swallow a path separator, so the members pattern would
  have matched a lookalike host. There is a test for it.
- **An absent policy file is an error, not an empty allowlist.** The most
  dangerous possible default is "no rules loaded, therefore nothing refused".
- **Redaction is on by default at the log sink.** RunLog installs the redactor
  at position zero of the processor chain unless explicitly disabled.
  Redaction that depends on each call site remembering is redaction that fails
  the first time somebody adds a log line in a hurry.
- **Two redaction mechanisms, because they fail differently.** Exact secret
  values catch a credential in any shape, including inside a URL or an
  exception message. Regex patterns catch regulated *shapes* that nobody
  registered because nobody knew they were about to be logged. Secrets are
  scrubbed longest-first, or a short secret that prefixes a longer one leaves
  the tail behind.
- **Regulated screenshots are quarantined, not blurred.** Of the two options
  the spec offers, blurring is a guess about where the data sits on screen,
  and a guess that is wrong once has published a balance. Captures taken after
  a pii-tagged output has been read move to `restricted/`, which is
  gitignored; the manifest records that they exist, why, and that they are not
  committed - so the evidence says what it is missing rather than pretending
  to be whole. The trigger is the *contract*, not pixel-scanning.
- **Automatic re-authentication is off, and the flag is real.**
  `reauth_allowed: false` ships as the default: a session that dies mid-flow
  may have died from a lockout, a policy change, or a concurrent sign-on, and
  signing back in silently hides all three while risking a loop against an
  account being locked. Escalating surfaces it once. When the flag is true, a
  capability may declare a `reauthenticate` recovery rule that *names the
  steps that sign on* - the executor may not guess which steps re-enter a
  credential. Both branches are tested against the live app with the same
  artifact and the same fault, one flag apart.
- **The secret sweep covers every configured secret, not just the password.**
  The test reads the policy's secret_env list and scans every committed byte
  of artifacts/ and evidence/ for each value, including the model API key.

The asymmetry, stated plainly because it is the point: discovery may never
perform an irreversible action, because the model is exploratory and fallible
and an account opened by mistake cannot be un-opened. Replay may, because a
human has already reviewed exactly which step is irreversible - but only once
the artifact has earned approval. Today open_subaccount is a draft, so
replaying it runs twelve steps, reaches the review screen, and stops.
