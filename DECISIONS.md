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
