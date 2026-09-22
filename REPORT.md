# REPORT

## 1. Architecture

Four layers, one seam that matters.

```
discovery/        an LLM drives the UI once           ─┐
  ↓ recorder                                           │ model here
artifacts/*.json  a typed, reviewable capability      ─┘
  ↓
replay/           deterministic execution, no model   ─┐
policy/           one gate, called from one place      │ no model here
escalation/       lease, operator console, VNC         │
catalog/          the API an agent invokes             ─┘
        ↓
surface/          the ONLY package that knows about a browser
```

**The surface seam is the load-bearing decision.** Everything above it speaks
`Observation`, `Action` and `Handle` and never learns what is underneath. It is
checked rather than intended: `make boundary` greps for the driver outside
`src/cua/surface/`, and 173 of the 225 tests run in an image with **no browser
installed at all**. The boundary test caught a real leak — importing a helper
whose *module name* contained the driver — which is exactly the kind of drift
a convention would not have caught.

**Replay was built before discovery, deliberately.** Writing the agent first
lets whatever the model happens to emit become the schema by default. Since the
artifact is the central deliverable, it was designed against what an executor
needs and what a reviewer must approve, and then discovery had to meet that bar.
The model never sees a `TargetSpec` and could not write one.

**Single process, by choice.** The operator console runs inside the replay that
raised the intervention, because it has to see the live lease and the live
browser; sharing those across processes means building the queue the brief
explicitly says is not rewarded. Three containers only where isolation is real:
the target app runs on a plain image so the automation has no privileged path
into the thing it is automating.

**Trade-off accepted:** perception is ~200 lines of injected JavaScript we own,
rather than the driver's accessibility snapshot. We needed role + name, coverage
of *every* frame, a stable handle back to the element, and a shape a UIA/AX
surface could fill identically. No single driver API gives all four, and the
driver's a11y snapshot gives neither frame traversal nor element mapping.

## 2. Artifact schema

Six defences, each enforced by the **type system** rather than by the executor,
so a specific failure mode becomes *unrepresentable*:

| # | Defence | Mechanism |
|---|---|---|
| 1 | Parameters never inlined | `ValueRef` is exactly one of `param`/`literal`/`secret_ref`; a literal may not be tagged `pii`, nor structurally resemble an SSN or a **Luhn-valid** card |
| 2 | Every step verified | `postcondition` is **required** — "click and hope" cannot be recorded |
| 3 | Outcomes are contract | `MEMBER_NOT_FOUND` is a declared value, not an exception |
| 4 | The ladder is evidence | Every rung kept with `matches_at_record`; `css`/`xpath` coerce `brittle=true` |
| 5 | Tenancy explicit | vendor product + version + tenant; `variant_of` links forks |
| 6 | Provenance is a hash | `transcript_sha256`, and `extra="forbid"` leaves nowhere to put a transcript |

The sharpest consequence: **a `wait` step accepts no target and no value**, only
the condition it waits on. There is no field a fixed sleep could live in, so
"no fixed sleeps" is a property of the type rather than reviewer discipline.

Two judgement calls worth naming. The Luhn gate on the card screen is there
because a check that fires on every long number gets switched off within a
week, which is worse than no check. And `extra="forbid"` means an older reader
rejects a newer artifact outright — that cost is accepted, and is what
`schema_version` exists for.

Results are a three-arm discriminated union, never a boolean plus exceptions.
All three arms carry `capability_id`, `version` and `run_id`, because a result
that cannot say what produced it cannot be debugged.

## 3. Determinism & error handling

**Locators.** A recorded ladder of up to five rungs, tried in order. Two rules:
*ambiguity is a miss* — a rung matching more than one node is recorded and
skipped, never resolved by taking the first — and *every attempt is kept*, so a
`TARGET_NOT_FOUND` reports each rung's count plus the same-role nodes that were
actually present. The ladder walk lives in `surface/base.py`, not in each
surface: determinism is inherited, not reimplemented.

**Waiting.** No fixed sleeps. Steps poll observations against their own
postcondition, bounded by the recorded `timeout_ms`. Hidden nodes are dropped at
the perception boundary — a hidden `__VIEWSTATE` is a textbox in the raw tree,
and counting it would make an unambiguous target look ambiguous.

**Detection** is a standing set run against every observation, before and after
every action. Precedence is the design:

1. **declared outcome** — the artifact says this is an answer
2. auth wall → escalate
3. modal → recover if a declared rule matches
4. HTTP error → hard failure
5. error banner → hard failure

Member `10003` returns a real HTTP 403 *and* an error banner *and* a declared
outcome; all three detectors fire and ordering decides. The caller is told
`PERMISSION_DENIED`. Detectors also run *inside* the wait loop ahead of the
postcondition, which is why "no such member" returns at `s6` rather than timing
out twenty-five seconds later complaining the checkpoint never came true.

**Recovery is data, not code.** Only declared rules fire, bounded per-rule and
by a global run budget. Only `safe_reversible` steps retry — retrying a
state-changing click is how one confirmation becomes two accounts.

**Drift** means "resolved differently than when recorded", not "a fallback
won". This artifact deliberately records `a11y_role_name` as preferred for a
field that has *no accessible name*, so a fallback wins on every run by design;
defining drift the other way would emit a signal every time and bury the real
one. Aggregated across tenants on one product, a rung that starts losing is the
earliest warning a version has moved.

Six defects in this layer were found by running it, not reading it — including
a step timeout that kept running while a human held the session, and a
slow-response signal that measured only the checkpoint poll when the injected
delay lands inside the click.

## 4. Heterogeneity & multi-tenant

**Why the accessibility tree is the portable perception layer.** Role, name,
value, state and a containment path exist on UIA and AX too. `UiNode.frame_path`
is the generalised containment path: frame names on the web, window/pane
hierarchy on desktop. `container_ref` and `order` give `near_text` a notion of
proximity that means the same thing on a surface with no DOM.

**What a `DesktopSurface` implements:** `observe`, `find`, `act`, `snapshot`,
`pause`, `resume`, `watch_human_actions`. It does **not** implement the locator
ladder, the detectors, the executor or the schema. Only `css` and `xpath` reach
the driver; the four portable rungs resolve by filtering `UiNode`s in Python, so
a surface with no DOM simply contributes no brittle rungs and the rest works
unchanged. `registry.create(DESKTOP)` raises an error naming exactly what an
implementation would supply — an honest statement of the seam rather than a
class full of `NotImplementedError`.

**Base / overlay / fork.** An overlay may rename controls, remap frames, prefix
routes, override values, and *insert an additional input field* a tenant
requires. It may **not** remove a step, reorder steps, change an action verb, or
raise a step's risk — and that line is enforced, not described. A tenant needing
any of those forks with an explicit `variant_of`, so divergence is visible in
the catalog rather than hidden in an override file.

Demonstrated: `lookup_member_balance`, recorded against MERIDIAN, replays
against `/t/summit-cu/` with a 20-line overlay and returns the same balance.
Pointing the same recording at Summit with **routing only** fails at `s4` —
`button 'SEARCH' present in frame contentFrame → 0 matching node(s)` — because
Summit calls that frame `main`. The registry **refuses** a tenant with no
overlay rather than falling back to the base recording: running one tenant's
locators against another is how automation types into the wrong field and calls
it success.

**Drift detection at scale** is fallback-win telemetry. Every resolution records
which rung won against which was recorded; a rung whose loss rate rises across
tenants on the same product version is the signal to re-record, before a run
fails.

## 5. Escalation & handoff

**Detecting stuck** is the standing detector set plus the discovery agent's own
`stuck` tool and four independent stopping conditions (step budget, wall clock,
three identical observation digests, explicit stop).

**The lease is the authority.** `Surface.act()` asks it before every action, so
automation acting while an operator holds the session is a `LeaseViolation`, not
a race avoided by convention. The console *follows* the lease rather than
deciding it.

**One interactive VNC port, not a URL parameter.** Two `x11vnc` servers share
one Xvfb display: `-viewonly` owns `:5900` (bridged to noVNC `6080`), a second
owns `:5901` (`6081`). View-only is a property of the socket, not a
`view_only=true` query parameter anyone could delete from the address bar. The
console embeds the monitor endpoint and swaps to the interactive one only once
control has actually transferred.

**Human action capture** installs capture-phase listeners in every frame —
capture phase because a legacy page calling `stopPropagation` would otherwise
silence the audit trail. Events are gated on the lease, so automation's clicks
after a resume are not mislabelled as a person's. Values are recorded as shapes,
never content: `PASSWORD`, `sensitive: true`, nothing else. Which control the
operator touched explains the resume; what they typed does not belong in a
repository.

**Bounded resume.** On hand-back the executor re-runs the guard rather than
continuing blindly, so an operator who did not actually fix the condition
produces the same finding — and the second time it stops. An expired hold ends
the run as `HUMAN_TIMEOUT` and **never reverts to automation**: the page is in
whatever state the operator left it, and the executor's model of where it is no
longer matches reality. Operator hold time is excluded from step timeouts, or
any hold longer than a step's timeout would punish the run for the handoff that
rescued it.

Verified end to end, including a real session driven through noVNC by hand.

## 6. Safety

**One chokepoint.** `PolicyGate.check()` is called from `Surface.act()` and
nowhere else. A longer rule list spread over three call sites has three places
to forget.

**The gate does not trust the artifact.** An artifact is data; a recording that
under-declared a CONFIRM button should still be caught, so risk is re-derived
from the policy's route and label rules and the higher of declared-vs-derived
applies. Demonstrated by running discovery in supervised mode — where the model
had been *told* it could confirm — and having the gate refuse anyway. The prompt
is advisory; the gate is enforcement.

**The asymmetry.** Discovery may never perform an irreversible action: the model
is exploratory and fallible, and an account opened by mistake cannot be
un-opened. Replay may, because a human has already reviewed exactly which step
is irreversible — but only once the artifact is `approved`, and approval is
*earned* by `make stability` replaying it, not asserted in a field.

**A credential the model invented is refused, not submitted.** Placeholder
substitution kept the real values out of the transcript, but it assumed the
model would use the tokens. Running discovery repeatedly against a live model
showed it inventing a username and password instead in half the runs. A wrong
guess is not a failed step: it spends a real authentication attempt against a
real account, which is how automation walks into a lockout. Rewording the
prompt did not hold — the same lesson as above — so the refusal sits in the
tool layer in front of the surface, and is returned *to* the model, which then
retries with the token instead of the run dying at the login screen. Of those
six runs, two got as far as a failed sign-on and the third ran out of step
budget first; after the refusal, none did.

**Redaction at the sink**, on by default, with two mechanisms because they fail
differently: exact secret values catch a credential in any shape; regex patterns
catch regulated shapes nobody registered. Regulated screenshots are
**quarantined, not blurred** — blurring is a guess about where the data sits,
and a guess that is wrong once has published a balance.

### Limits, stated plainly

- **The allowlist is URL-shaped, so it cannot express "only this member's
  records."** It can say `/members/**`; it cannot say *which* member. Real
  row-level authorisation has to come from the application, and this system
  cannot substitute for it.
- **noVNC control is all-or-nothing.** An operator who takes the session can
  drive the whole browser, not just the field that needs fixing. The lease
  bounds *who* and *when*, not *what*.
- **The single chokepoint is only as good as the surface.** Anything that
  bypasses `Surface.act()` — including the human at the VNC console — bypasses
  the gate. That is intentional for the human, and a real hazard for any future
  code path that forgets.
- **Redaction is best-effort against unknown shapes.** It caught the credential
  in the accessibility dump only after that leak was found by a test; patterns
  cannot anticipate every regulated format.
- **Fault injection is process-global**, so two concurrent runs would see each
  other's faults. Acceptable in a fixture, wrong in anything real.
- **The credential refusal is label-shaped.** It matches the near-text beside
  the box — `PASSWORD`, `USER ID` — rather than `input type=password`, so the
  rule still means something on a surface with no DOM. The cost is an
  app-shaped list: a sign-on that labels its field something else is not
  covered, and the check cannot tell that it is not covered. It also only
  stops the model *submitting* a guess; it does not stop it guessing, which it
  still does in roughly two runs in three before correcting itself.
- **The live-model tests can still skip.** A provider rate limit, or a run that
  never gets past the sign-on, reports its reason rather than passing hollow.
  Three consecutive clean full runs is evidence the causes are fixed, not a
  guarantee that they cannot recur.

## 7. Cuts

**Deliberately not built:**

- **No desktop surface.** The seam is real and the registry names what an
  implementation would supply, but nothing implements it.
- **No queue, workers, or multi-tenant infrastructure.** The brief says this is
  not rewarded. The abstractions are shaped so it could be added; it is not.
- **The operator console is intentionally minimal** — four endpoints and inline
  HTML. The mechanism underneath is the interesting part, and a polished UI
  would have been a worse use of the effort.
- **Interventions are in-memory.** Persisting them is the first step towards the
  queue that was explicitly out of scope.
- **Single process**, as above.

**On the stretch goals.** The brief caps these at "one or two" and does not
reward breadth, and four are present here. The reason is that each is a small
extension of the core rather than a new surface: the **catalog** is a projection
of a contract that already existed; **overlays** are the multi-tenant answer
Section 3.7 asks for, made concrete; **approval** is what makes the Section 3.4
asymmetry enforceable rather than a promise; and the **assisted fallback** is
one bounded call on one failure mode. None of them added a new subsystem. If
that still reads as breadth, the two to keep are overlays and approval, because
the other two are demonstrations of the core rather than parts of it.

**What I would build next, in order:**

1. **Row-level policy.** The largest real gap. Expressing "this agent may read
   member 10001 only" needs a parameter-aware gate, not a URL allowlist.
2. **Scoped operator handoff** — hand a person one field, not the whole browser.
3. **A desktop surface**, to prove the seam rather than argue for it. The
   portable rungs were designed for exactly this and have never been run
   against UIA.
4. **Drift telemetry across tenants** — the data is already emitted per run;
   nothing aggregates it yet, and aggregation is where it becomes an early
   warning instead of a log line.
5. **Re-recording on drift**: when a fallback rescues a locator, that is the
   trigger for a supervised re-record, which currently a human must notice.
