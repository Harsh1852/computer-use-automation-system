# Computer-Use Automation System

An LLM drives a hostile legacy UI once to accomplish a natural-language goal. The successful run
is recorded as a typed, versioned **capability artifact**. That artifact is then replayed
**deterministically, with no model in the decision loop**, and returns a structured result. When
the executor cannot safely proceed it escalates to a human who takes control of the *same live
browser session*, fixes the situation, and hands control back so the run resumes.

**[REPORT.md](REPORT.md)** is the design write-up. **[DECISIONS.md](DECISIONS.md)** is the
running log of every fork the design left open and the trade-off taken.

---

## Quick start

```bash
docker compose up --build -d app
```

Then open <http://localhost:5000/login> and sign on with any user id and password `demo1234`.

No host-side Python install is required.

### Configuration

Copy `.env.example` to `.env`. Nothing in the target application or the replay path needs an API
key; only the discovery agent does.

| Variable | Purpose |
|---|---|
| `APP_USER`, `APP_PASSWORD` | Credentials for the target app. Artifacts reference the password only as `{"secret_ref": "APP_PASSWORD"}`. |
| `APP_SECRET_KEY` | Flask session signing key for the demo app. |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Discovery agent only. The model name is never hardcoded. |

### Running without `make`

`make` is a convenience, not a dependency — it is not installed by default on Windows, and every
target in the [Makefile](Makefile) is one `docker compose` command. The rest of this README uses
the short `make` form; this table is the authoritative translation. Run all of them from the
repository root.

| `make …` | Equivalent command |
|---|---|
| `make up` | `docker compose up --build -d` |
| `make down` | `docker compose down -v` |
| `make logs` | `docker compose logs -f` |
| `make test` | the `boundary` check below, then `docker compose run --rm cua pytest -q -m "not llm"` |
| `make test-schema` | `docker compose --profile test run --rm --build tests pytest -q -m "not llm and not integration"` |
| *(no target — opt in)* | `docker compose run --rm cua pytest -v -m llm` — the 4 live-model tests, see [Running the tests](#running-the-tests) |
| `make boundary` | `grep -r playwright src/cua --include="*.py" \| grep -v surface/` — must print nothing |
| `make observe` | `docker compose run --rm cua python -m cua.cli observe` |
| `make observe TENANT=/t/summit-cu` | `docker compose run --rm cua python -m cua.cli observe --tenant-prefix /t/summit-cu` |
| `make reset` | `curl -s -X POST http://localhost:5000/admin/reset` |
| `make inject FAULT=slow` | `curl -s -X POST http://localhost:5000/admin/inject -H 'Content-Type: application/json' -d '{"fault":"slow","once":true}'` |
| `make faults` | `curl -s http://localhost:5000/admin/status` |
| `make replay ARTIFACT=A PARAMS=P` | `docker compose run --rm cua python -m cua.cli replay --artifact A --params 'P'` |
| `make discover CAPID=C GOAL=G WRITE=1` | `docker compose run --rm cua python -m cua.cli discover --id C --goal "G" --write-artifact` |
| `make stability ARTIFACT=A PARAMS=P N=5 WRITE=1` | `docker compose run --rm cua python -m cua.cli stability --artifact A --params 'P' --n 5 --write` |
| `make escalation-demo [MANUAL=1]` | `docker compose exec cua python scripts/escalation_demo.py [--manual]` |
| `make catalog` | `docker compose exec cua python -m cua.cli catalog` |
| `make agent-demo` | `docker compose exec -T cua python scripts/agent_calls_capability.py --catalog http://localhost:8081` |

The remaining `make` variables map to flags on the same commands, each on the targets whose CLI
subcommand accepts it:

| Variable | Becomes | Targets |
|---|---|---|
| `TENANT_ARGS=…` | appended verbatim | `replay` |
| `TRACE=1` | `--trace` | `replay` |
| `ASSIST=1` | `--assist` | `replay` |
| `EVIDENCE=x` | `--evidence x` | `replay`, `discover`, `stability` |
| `SUPERVISED=1` | `--supervised` | `discover`, `stability` |
| `TENANT=x` | `--tenant-prefix x` on `observe` (a URL prefix, `/t/summit-cu`); `--tenant x` on `agent-demo` (a tenant id, `summit-cu`) | `observe`, `agent-demo` |

`escalation-demo`, `catalog` and `agent-demo` use `docker compose exec` rather than `run`
deliberately: the already-running `cua` service holds host ports 8080/8081/6080/6081, so a
one-off container's console and VNC bridges would be unreachable.

#### Windows

The commands above work as written in **Git Bash** and **WSL**. In **PowerShell** two of them
need adjusting:

- **JSON arguments must escape their inner quotes.** PowerShell strips them when building the
  command line for a native executable, so `--params '{"member_id":"10001"}'` arrives at the CLI
  as `{member_id:10001}` and fails to parse. Write `--params '{\"member_id\":\"10001\"}'`.
- **`curl` is an alias for `Invoke-WebRequest`** and rejects `-X`/`-H`/`-d`. Use `curl.exe`.

PowerShell has no `grep`, so the `boundary` check becomes:

```powershell
Get-ChildItem src\cua -Recurse -Filter *.py | Select-String playwright |
  Where-Object { $_.Path -notmatch '\\surface\\' }
```

Two further notes, whichever shell you use. Git for Windows ships with `core.autocrlf=true`;
[`.gitattributes`](.gitattributes) pins the working tree to LF so the container entrypoint does
not die on a `bash\r` shebang, but a clone made before that file existed needs re-cloning. And in
Git Bash, MSYS rewrites leading-slash arguments into Windows paths — pass `--tenant-prefix` and
`--path` only when you need a non-default value, or prefix the command with `MSYS_NO_PATHCONV=1`.

---

## The target application

**MERIDIAN CoreBank — Servicing Console** (`apps/corebank/`) is a deliberately hostile stand-in for
a legacy core-banking servicing screen. It exists to make the automation problem realistic rather
than to be pleasant:

- a real `<frameset>` with a `nav` and a `content` frame, so every locator has to carry a frame path
- table-based layout nested three deep
- ASP.NET-style generated ids (`ctl00_ContentPlaceHolder1_txtMbrNo`)
- inline `__doPostBack(...)` navigation that swaps the other frame
- plain `<td>` text where a `<label for>` should be, on several fields — so those inputs have **no
  accessible name** and a naive role+name locator cannot find them
- **no `data-testid` anywhere**, by design

### Flow

`/login` → `/app` (frameset) → `content: /members/search` → `/members/detail?mbr=NNNNN` →
`/members/subaccount/new?mbr=NNNNN` → `/members/subaccount/review` → `/members/subaccount/confirm`

Members `10001`–`10005` are seeded with obviously synthetic data. Member `10003` is flagged
`restricted` and always returns a permission denial on the detail screen.

### Fault injection

Faults are armed out of band and consumed by the next applicable request on a `/members/**` route.

```bash
curl -s -X POST http://localhost:5000/admin/inject \
  -H 'Content-Type: application/json' -d '{"fault":"slow","once":true}'

curl -s -X POST http://localhost:5000/admin/reset
curl -s http://localhost:5000/admin/status
```

| Fault | Behaviour |
|---|---|
| `not_found` | Search returns `NO MATCHING MEMBER RECORD FOUND` |
| `validation` | Sub-account form rejects with `INITIAL DEPOSIT MUST BE AT LEAST $25.00` |
| `permission_denied` | Detail view returns `YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD` (HTTP 403) |
| `session_expired` | Next servicing request bounces to `/login` with `YOUR SESSION HAS TIMED OUT` |
| `slow` | Next servicing page sleeps 6 seconds |
| `interstitial` | Next servicing page renders a `SYSTEM MAINTENANCE NOTICE` modal with a DISMISS button |
| `app_error` | Next servicing page returns HTTP 500 with an ASP.NET-looking stack trace |

`make inject FAULT=slow`, `make reset` and `make faults` wrap these.

### Second tenant

The same blueprint is mounted again at `/t/summit-cu/` as a stand-in for a second institution
running the same vendor product: different branding, `ACCOUNT NUMBER` instead of `MEMBER NUMBER`,
an extra required `BRANCH` dropdown on the sub-account form, and different frame `name` attributes
(`sidebar`/`main` instead of `navFrame`/`contentFrame`).

---

## The capability artifact

`src/cua/schema/` is pure Pydantic v2 and imports nothing else in the repo — the artifact is the
contract between the discovery agent, the replay executor, the policy gate and any calling agent,
so it must not be able to acquire a dependency on any one of them.

Six defences are enforced by the type system rather than by the executor. Each makes a specific
failure mode *unrepresentable*:

| # | Defence | How it is enforced |
|---|---|---|
| 1 | Parameters are never inlined | `ValueRef` is exactly one of `param` / `literal` / `secret_ref`; a literal may not be tagged `pii`/`secret`, nor structurally resemble an SSN or a Luhn-valid card number |
| 2 | Every step has a postcondition | `Step.postcondition` is required. There is no way to record "click and hope" |
| 3 | Business outcomes are contract | `outcomes` declares the legitimate non-success answers, so "no such member" is a value, not an exception |
| 4 | The locator ladder is recorded | Every candidate strategy is kept with its `matches_at_record` count; `css`/`xpath` are always `brittle` |
| 5 | Tenancy is explicit | `TargetBinding` names vendor product, version and tenant; `variant_of` links a fork to its base |
| 6 | Provenance is a hash | `transcript_sha256` only, and `extra="forbid"` means there is nowhere to put a transcript |

A `wait` step accepts no target and no value — only the condition it waits on. **A fixed sleep is
not expressible in the schema.**

Replay returns a three-arm discriminated union, never a boolean plus exceptions:

```python
match result.status:
    case "success":           result.outputs        # declared outputs
    case "business_outcome":  result.code           # MEMBER_NOT_FOUND
    case "failure":           result.kind, result.at_step, result.expected, result.observed
```

---

## The surface seam

`src/cua/surface/` is the only package permitted to know what a browser is. Everything above it —
resolver, detectors, executor, the discovery agent's prompt — speaks `Observation`, `Action` and
`Handle`. That boundary is checked, not just intended:

```bash
make boundary        # grep -r playwright src/cua | grep -v surface/  must be empty
make test-schema     # the schema and seam tests pass in an image with no browser installed
```

A new surface implements ten things: the perception and actuation core — `observe`, `find`,
`resolve`, `act`, `snapshot` — plus `start`/`close` for lifecycle, `pause`/`resume` for the
handoff, and `watch_human_actions` so an operator's steps reach the audit trail. It does **not**
implement the locator ladder — `walk_ladder` is shared policy, because "try the primary, then
fallbacks in order, and treat an ambiguous match as a miss" is a rule about robustness, not
about browsers. A `DesktopSurface` supplies perception and actuation; determinism is inherited.

**Only `css` and `xpath` touch the driver.** The four portable rungs (`a11y_role_name`,
`label_text`, `near_text`, `exact_text`) resolve by filtering `UiNode`s in Python. A surface with
no DOM simply offers no brittle rungs and the rest works unchanged.

```bash
make observe                         # base tenant
make observe TENANT=/t/summit-cu     # same product, different frame names and labels
```

```
 REF  FRAME          ROLE           NAME                 VALUE   SRC
   3  navFrame       link           MEMBER SEARCH                text
   9  contentFrame   cell           MEMBER NUMBER                text
  10  contentFrame   textbox                                     none      <- no accessible name
  11  contentFrame   button         SEARCH                       value
```

Node 10 is the whole problem in one line: the member-number field has **no accessible name**,
because its label is plain `<td>` text. `a11y_role_name` finds nothing and the ladder falls
through to a `near_text` anchor on node 9. The same page under `/t/summit-cu/` reports frames
`sidebar`/`main` and an `ACCOUNT NUMBER` anchor — the per-tenant drift an overlay has to absorb.

---

## Deterministic replay

No model is consulted anywhere on this path. Given an artifact and parameters, the same inputs
produce the same steps and the same outputs.

```bash
make replay ARTIFACT=lookup_member_balance PARAMS='{"member_id":"10001"}'
```

One artifact, four classes of answer, decided only by what the application did:

| Command | Result |
|---|---|
| `PARAMS='{"member_id":"10001"}'` | `SUCCESS` — `{"savings_balance": "1234.56"}`, 7 steps |
| `PARAMS='{"member_id":"99999"}'` | `BUSINESS_OUTCOME` — `MEMBER_NOT_FOUND` at `s6`, exit **0** |
| `PARAMS='{"member_id":"10003"}'` | `BUSINESS_OUTCOME` — `PERMISSION_DENIED` (the page is an HTTP 403) |
| `make inject FAULT=slow` then replay | `SUCCESS` with a `SlowResponse` recovery: *step took 6594ms against a recorded p50 of 250ms* |
| `make inject FAULT=interstitial` then replay | `SUCCESS` with a `dismiss_maintenance_notice` recovery |
| `make inject FAULT=app_error` then replay | `FAILURE` — `UNRECOVERABLE_CONDITION` at `s4`, *status 500 in contentFrame at http://app:5000/members/search*, exit **1** |
| `PARAMS='{"member_id":"abc"}'` | `FAILURE` — `CONTRACT_VIOLATION`, 0 steps, browser never opened |

Three things that are deliberate:

- **A declared outcome beats the HTTP error that carries it.** Member `10003` returns a real 403.
  Because the artifact declares `PERMISSION_DENIED` and `DeclaredOutcomeDetector` runs first, the
  caller is told the answer rather than handed a crash. Conflating those is the mistake the
  ordering exists to prevent.
- **Recoveries are reported, not swallowed.** A run that absorbs six seconds says so.
- **A failure names the frame.** `status 500 in contentFrame at …/members/search`, not the shell
  URL that returned 200.

Each run writes `run.jsonl`, `result.json`, `manifest.json`, `screenshots/` and `observations/`
into its evidence directory. Secrets and `pii`-tagged outputs are redacted there by their
**declared sensitivity** — the caller receives the real balance, the repository does not.

---

## Discovery

```bash
make discover CAPID=lookup_member_balance_llm WRITE=1 EVIDENCE=evidence/discovery-lookup-balance \
  GOAL="Look up member 10001 and read their current savings balance."
```

Needs `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`. The model name is never hardcoded.

`CAPID` is both the artifact id and, unless `EVIDENCE` overrides it, the evidence directory
(`evidence/discovery-<CAPID>/`). It is deliberately **not** `lookup_member_balance` here:
`WRITE=1` installs `artifacts/<CAPID>.v1.json`, and reusing the id would overwrite the
hand-written, `approved` artifact the replay and stability sections below depend on.

The model observes, decides and acts; a **recorder running alongside it** turns the run into a
capability. That split is the point: the model never sees a `TargetSpec` and could not write one,
so its output shape cannot become the schema.

What the model can and cannot see:

- **No markup, no ids, no selectors.** Its whole vocabulary for "which element" is a `ref` from
  the last observation. Fields with no accessible name are rendered with a `near="MEMBER NUMBER"`
  hint, computed from the same containment relation the `near_text` locator uses, so what the
  model reads and what the recorder writes down cannot disagree.
- **No credentials.** It types placeholder tokens which the tool layer substitutes on the way to
  the browser, so the transcript holds the placeholder and the recorder emits a `secret_ref`.
  A model asked to sign on will sometimes type a credential it invented instead, so that is
  **refused in front of the surface** rather than submitted — a guess is not a failed step, it is
  a real authentication attempt against a real account, which is how automation walks into a
  lockout. The refusal is returned *to the model*, which retries with the token, so the run
  continues instead of dying at the login screen. The rule matches on the near-text label rather
  than on `input type=password`, so it still means something on a surface with no DOM.
- **One screenshot at the start, one after `stuck`.** Text observations everywhere else.

Per accepted action the recorder scores the **full** candidate ladder against the live
observation — before acting, while the element is still on screen — records each rung's match
count, infers a postcondition from what actually changed, parameterises typed values *and the
URLs they reappear in*, and classifies risk. The model's proposed contract is then validated
against what was really typed and read.

Stopping conditions: 25 tool calls, a 5-minute wall clock, three consecutive identical
observation digests *from mutating actions*, or an explicit `stuck`.

### The real runs

Two genuine `gpt-4.1` runs are recorded in `evidence/`, each with `transcript.jsonl`,
`steps.jsonl`, `screenshots/` and the emitted `artifact.json`.

| Run | Result |
|---|---|
| `discovery-lookup-balance/` → `lookup_member_balance_llm` | 8 tool calls, 13s, **7 steps**. Replays: `10001 → 1234.56`, `10002 → 88.00`, `99999 → MEMBER_NOT_FOUND` |
| `discovery-open-subaccount/` → `open_subaccount` | 15 tool calls, 51s, **14 steps, 1 irreversible**. Recorded on 10001/SAVINGS/VACATION/50.00; replayed as CHECKING/HOLIDAY/75.00 in [`evidence/replay-open-subaccount/`](evidence/replay-open-subaccount/manifest.json) |

Told not to act irreversibly, the model walked the entire sub-account form and then **stopped at
the review screen and called `stuck`** — the safety rule holding on its own. Recording that
capability therefore needs `--supervised`, an explicit off-by-default mode that swaps exactly one
prompt rule. Unattended discovery still refuses, and the policy gate enforces the same asymmetry
rather than trusting a prompt: under the shipped `policy.yaml`, `--supervised` reaches the
confirmation and is **still blocked**, because relaxing a prompt must not be enough to execute an
irreversible action. Recording that step is a deliberate act — a person present *and* a policy
edited to say so. All three of those paths are asserted against a live model in
[`tests/test_discovery_llm.py`](tests/test_discovery_llm.py).

Both are emitted as `draft` — approval is earned later, by `make stability` — and both reference
credentials only as `secret_ref` and contain no seeded password anywhere; there is a test that
greps for it.

---

## Escalation and control transfer

```bash
make escalation-demo            # scripted operator, unattended
make escalation-demo MANUAL=1   # stops and hands the session to you
```

A replay that cannot safely continue hands the **same live browser** to a person. The lease is
the authority: `Surface.act()` asks it before every action, so automation acting while an
operator holds the session raises `LeaseViolation` rather than racing.

| Port | Server | Purpose |
|---|---|---|
| `6080` | `x11vnc -viewonly` on `:5900` | monitor — the console embeds this by default |
| `6081` | `x11vnc` on `:5901` | interactive — swapped in **only once the lease has moved** |

View-only is a property of the socket, not a `view_only=true` parameter anyone could delete
from the address bar. The console at `localhost:8080` follows the lease rather than deciding it.

An expired hold ends the run as `HUMAN_TIMEOUT` and never reverts — the page is in whatever
state the operator left it. Resume is bounded at two escalations: on hand-back the executor
re-runs the guard, so an operator who did not fix the condition produces the same finding and
the second time it stops. Operator hold time is excluded from step timeouts.

Operator actions are captured by capture-phase listeners in every frame and land in
`run.jsonl` interleaved with the machine's, gated on the lease so automation is never
mislabelled:

```
02:16:11.123  intervention.open    observed='YOUR SESSION HAS TIMED OUT' on screen
02:16:11.422  human.action         kind=change name=USER ID   value=<redacted:9 chars>
02:16:11.507  human.action         kind=change name=PASSWORD  value=<password>
02:16:11.509  human.action         kind=click  name=SIGN ON
02:16:11.554  intervention.closed  outcome=released human_steps=4
02:16:14.357  run.finish           status=success steps_executed=7
```

Values are shapes, never content: which control the operator touched explains the resume; what
they typed does not need to be in a repository.

---

## Safety and data handling

[`policy.yaml`](policy.yaml) is the single source of what the automation may do, and
`PolicyGate.check()` is called from **`Surface.act()` and nowhere else**.

```
$ make replay ARTIFACT=tests/fixtures/reach_admin.v1.json
FAILURE  POLICY_BLOCKED  at s1
refusing to navigate to http://app:5000/admin/inject:
matches denied pattern '**/admin/**'  (rule: allowlist.denied_patterns)
```

**The gate does not trust the artifact's own risk label.** A recording that under-declared a
CONFIRM button should still be caught, so risk is re-derived from the policy's route and label
rules and the higher of the two applies. Demonstrated by running discovery in `--supervised`
mode — where the model had been *told* it could confirm — and having the gate refuse anyway
with `RISKY_APPROVAL`. The prompt is advisory; the gate is enforcement.

**The asymmetry.** Discovery may never perform an irreversible action: the model is exploratory
and fallible, and an account opened by mistake cannot be un-opened. Replay may, because a human
has already reviewed exactly which step is irreversible — but only once the artifact is
`approved`. While `open_subaccount` was a draft, replaying it ran twelve steps, reached the
review screen, and stopped at `s13` with `POLICY_BLOCKED`. It ships `approved` today, having
earned it through `make stability SUPERVISED=1` — so both halves of the asymmetry are asserted
against the live app in
[`tests/test_policy_live.py`](tests/test_policy_live.py), which forces the draft state itself
rather than depending on what the shipped artifact happens to say.

**Redaction is at the sink.** `RunLog` installs the redactor at the front of the processor
chain unless explicitly disabled, so a log line written in a hurry is still covered. Exact
secret values catch a credential in any shape; regex patterns catch regulated shapes nobody
registered.

**Regulated screenshots are quarantined, not blurred.** Blurring is a guess about where the
data sits on screen, and a guess that is wrong once has published a balance. Captures taken
after a `pii`-tagged output is read move to `restricted/` (gitignored); the manifest records
that they exist and that they are not committed.

**Automatic re-authentication is off by default**, and the flag is real rather than decorative.
When enabled, a capability may declare a `reauthenticate` rule that *names the steps that sign
on* — the executor may not guess which steps re-enter a credential. Both branches are tested
against the live app, one flag apart.

---

## Demo path

Everything except discovery runs with **no API key**. If you do not have `make`, every command
below has a one-line `docker compose` equivalent in [Running without `make`](#running-without-make).

```bash
cp .env.example .env          # only OPENAI_* are needed, and only for discovery
docker compose up --build -d  # app on :5000, runtime on :8080/:8081/:6080/:6081
make test                     # 229 tests (the 4 `llm` ones are opted into separately)
```

**1. Discovery — a model drives the UI once** *(needs `OPENAI_API_KEY`)*

```bash
make discover CAPID=lookup_member_balance_llm WRITE=1 EVIDENCE=evidence/discovery-lookup-balance \
  GOAL="Look up member 10001 and read their current savings balance."
```

8 tool calls, ~13s, 7 recorded steps. Evidence and the emitted artifact land in
`evidence/discovery-lookup-balance/`, and `WRITE=1` installs
`artifacts/lookup_member_balance_llm.v1.json` — a separate id from the hand-written
`lookup_member_balance` the steps below replay, so a discovery run cannot overwrite it.

**2. Look at what it recorded**

```bash
cat evidence/discovery-lookup-balance/artifact.json
```

Credentials are `secret_ref`; the member number is a `param` — including inside the URL
checkpoint where it reappears; every locator carries its full ladder with match counts.

**3. Replay it deterministically** — no model in the loop

```bash
make replay ARTIFACT=lookup_member_balance PARAMS='{"member_id":"10001"}'
make replay ARTIFACT=lookup_member_balance PARAMS='{"member_id":"10002"}'
```

**4. The error and outcome paths**

```bash
make replay PARAMS='{"member_id":"99999"}'            # MEMBER_NOT_FOUND, exit 0
make replay PARAMS='{"member_id":"10003"}'            # PERMISSION_DENIED (a real 403)
make inject FAULT=slow         && make replay         # recovered SUCCESS
make inject FAULT=interstitial && make replay         # dismissed by a declared rule
make inject FAULT=app_error    && make replay         # FAILURE, naming the frame
make replay PARAMS='{"member_id":"abc"}'              # CONTRACT_VIOLATION, 0 steps
```

**5. Escalation — hand the live browser to a person**

```bash
make escalation-demo            # scripted operator, unattended
make escalation-demo MANUAL=1   # then open http://localhost:8080 and take control
```

**6. Policy refuses what it says it refuses**

```bash
make replay ARTIFACT=tests/fixtures/reach_admin.v1.json PARAMS='{}'   # POLICY_BLOCKED
```

**7. Cross-tenant reuse**

```bash
make replay ARTIFACT=lookup_member_balance TENANT_ARGS="--tenant summit-cu" \
  PARAMS='{"member_id":"10001"}'          # SUCCESS, via a 22-line overlay

make replay ARTIFACT=lookup_member_balance TENANT_ARGS="--tenant summit-cu --no-overlay" \
  PARAMS='{"member_id":"10001"}'          # CHECKPOINT_FAILED: Summit calls that frame `main`
```

**8. Approval is earned, not asserted**

```bash
make stability ARTIFACT=lookup_member_balance N=5 WRITE=1 \
  PARAMS='{"member_id":"10001"}'          # 5/5 identical -> promoted to approved
```

**9. A drifted locator, and one bounded model call** *(needs `OPENAI_API_KEY`)*

```bash
make replay ARTIFACT=tests/fixtures/drifted_locator.v1.json \
  PARAMS='{"member_id":"10001"}'          # TARGET_NOT_FOUND
make replay ARTIFACT=tests/fixtures/drifted_locator.v1.json ASSIST=1 \
  PARAMS='{"member_id":"10001"}'          # SUCCESS, reported as drift
```

**10. An agent discovers and calls a capability by name** *(needs `OPENAI_API_KEY`)*

```bash
make catalog       # in one shell
make agent-demo    # in another
```

```
USER: What is the current savings balance for member 10001?
  -> invoking lookup_member_balance({'member_id': '10001'})
  <- {"status": "success", "outputs": {"savings_balance": "1234.56"}}
AGENT: The current savings balance for member 10001 is $1,234.56.

USER: And for member 99999?
  -> invoking lookup_member_balance({'member_id': '99999'})
  <- {"status": "business_outcome", "code": "MEMBER_NOT_FOUND", ...}
AGENT: No member exists with the number 99999 at this institution.
```

The model reports `MEMBER_NOT_FOUND` as an **answer** rather than retrying, because the
outcome is declared in the contract and therefore in the tool description it was handed.

---

## Evidence

| Directory | What it shows |
|---|---|
| `discovery-lookup-balance/` | a genuine `gpt-4.1` run: transcript, steps, screenshots, emitted artifact |
| `discovery-open-subaccount/` | the same for a 14-step flow containing an irreversible step |
| `discovery-policy-blocked/` | discovery stopped at CONFIRM with `RISKY_APPROVAL` |
| `replay-success/` | the happy path, with the post-balance screenshot quarantined |
| `replay-of-discovery/` | the artifact the model emitted, replayed with the model gone |
| `replay-business-outcome/` | `MEMBER_NOT_FOUND` returned as an answer |
| `replay-permission-denied/` | `PERMISSION_DENIED` — a declared outcome beating a real 403 |
| `replay-contract/` | `CONTRACT_VIOLATION` on `member_id: "abc"`, 0 steps, browser never opened |
| `replay-recoverable/` | a modal dismissed by a declared rule, plus an absorbed delay |
| `replay-recovered-modal/` | the same dismissal on its own, without the delay |
| `replay-hard-failure/` | HTTP 500, naming the frame that errored |
| `replay-escalation/` | the full handoff, human and machine steps interleaved |
| `replay-policy-blocked/` | the allowlist refusing `/admin/**` |
| `replay-overlay-summit/` | the base recording running against a second tenant |
| `replay-overlay-missing/` | the same tenant with routing only — `CHECKPOINT_FAILED` at `s4` |
| `replay-open-subaccount/` | the 14-step flow replayed on parameters it was not recorded with |
| `replay-assisted-fallback/` | one bounded model call rescuing a drifted locator |
| `invoke-lookup_member_balance-*/` | the same capability invoked through the catalog API |
| `stability-*/` | the replays that earned `approved` |
| `phase3-observation/` | a bare observation of the frameset, from the surface phase |

Every run writes `run.jsonl`. Each replay adds `result.json` and `manifest.json`; the discovery
runs write `transcript.jsonl`, `steps.jsonl` and the emitted `artifact.json` instead. Screenshots,
`a11y/` dumps and `observations/` appear where a run had something to capture — an outcome, a
failure, a checkpoint — so a run refused before the browser opened (`replay-contract/`,
`replay-policy-blocked/`) has none, and the stability runs record results rather than pictures.
Captures taken after a `pii`-tagged output is read are quarantined to a gitignored `restricted/`
that the manifest lists but does not commit.

### Running the tests

```bash
make test
```

No host-side Python install: unit tests, the seam tests and the live browser integration tests all
run in containers. Without `make`, that is `docker compose run --rm cua pytest -q -m "not llm"`,
preceded by the boundary check — see [Running without `make`](#running-without-make).

**233 tests in three layers**, split by what each one needs:

| Selection | Tests | Needs |
|---|---|---|
| `-m "not llm and not integration"` | 179 | nothing — no browser, no app, no key<sup>†</sup> |
| `-m integration` | 50 | the CoreBank app and a browser |
| `-m llm` | 4 | `OPENAI_API_KEY`, and spends real money |

<sup>†</sup> `make test-schema` runs these in an image with no browser installed, where 177 pass and
the 2 protocol-conformance checks skip for want of a driver — which is the point of running them
there at all. In the full runtime all 179 pass.

`make test` runs the first two (229). The `llm` tests are excluded by default and opted into
explicitly, because a suite that quietly spends money on every run is a bad suite:

```bash
docker compose run --rm cua pytest -v -m llm
```

Roughly 100k tokens and ten minutes for all four. They are the only tests that cannot be faked:

| Test | What a real model has to do |
|---|---|
| `a_real_model_run_produces_a_replayable_capability` | drive `lookup_member_balance` cold and emit an artifact that replays with the model gone |
| `unattended_discovery_stops_rather_than_opening_the_account` | walk the sub-account form and stop — checked against the bank, not against the artifact's own story |
| `the_gate_refuses_the_confirmation_even_when_the_prompt_permits_it` | be told it may confirm, and be blocked anyway by `enforcement.discovery.irreversible` |
| `a_supervised_recording_captures_the_irreversible_step` | record the irreversible step under an explicit policy change, then replay it and return a *new* account number |

Two guards keep these honest rather than merely green. A run the provider rate limited, and a run
that never got past the sign-on, both **skip with their reason stated** — because a test that
passes without having reached the screen it exists to check is worse than one that fails. The
safety assertion (*no account was opened*) is checked before either guard can skip.

---

## Design write-ups

- [REPORT.md](REPORT.md) — the design write-up *(added in the final phase)*
- [DECISIONS.md](DECISIONS.md) — running log of forks the spec left open and the trade-off taken
