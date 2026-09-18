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
