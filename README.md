# Computer-Use Automation System

An LLM drives a hostile legacy UI once to accomplish a natural-language goal. The successful run
is recorded as a typed, versioned **capability artifact**. That artifact is then replayed
**deterministically, with no model in the decision loop**, and returns a structured result. When
the executor cannot safely proceed it escalates to a human who takes control of the *same live
browser session*, fixes the situation, and hands control back so the run resumes.

> **Build status:** Phase 1 of 8 complete — the target application. Subsequent phases add the
> artifact schema, the surface abstraction, deterministic replay, the discovery agent, escalation,
> policy, and the write-up.

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

## Design write-ups

- [REPORT.md](REPORT.md) — the design write-up *(added in the final phase)*
- [DECISIONS.md](DECISIONS.md) — running log of forks the spec left open and the trade-off taken
