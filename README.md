# Computer-Use Automation System

An LLM drives a hostile legacy UI once to accomplish a natural-language goal. The successful run
is recorded as a typed, versioned **capability artifact**. That artifact is then replayed
**deterministically, with no model in the decision loop**, and returns a structured result. When
the executor cannot safely proceed it escalates to a human who takes control of the *same live
browser session*, fixes the situation, and hands control back so the run resumes.

> **Build status:** Phases 1–4 of 8 complete — the target application, the artifact schema, the
> surface abstraction and deterministic replay. Subsequent phases add the discovery agent,
> escalation, policy, and the write-up.

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

A new surface implements six things: `observe`, `find`, `act`, `snapshot`, `pause`, `resume`. It
does **not** implement the locator ladder — `walk_ladder` is shared policy, because "try the
primary, then fallbacks in order, and treat an ambiguous match as a miss" is a rule about
robustness, not about browsers. A `DesktopSurface` supplies perception and actuation; determinism
is inherited.

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

### Running the tests

```bash
make test
```

No host-side Python install: unit tests, the seam tests and the live browser integration tests all
run in containers.

---

## Design write-ups

- [REPORT.md](REPORT.md) — the design write-up *(added in the final phase)*
- [DECISIONS.md](DECISIONS.md) — running log of forks the spec left open and the trade-off taken
