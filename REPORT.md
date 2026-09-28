# Design report

The model discovers, a compiler turns the verified run into a reviewable capability, and
deterministic replay is how an agent invokes it. Depth went to the artifact schema, replay and its
error taxonomy, and the safety and escalation model. Rationale lives in [docs/adr/](docs/adr/);
what each evidence folder proves is in [evidence/README.md](evidence/README.md).

## 1. Architecture

A modular monolith: one async Python process per run, with seams (surface, planner, notifier,
store) where services would plug in. Queues and services would add failure modes without testing
anything the brief asks about.

```
goal ─► DiscoveryRunner (observe → decide → policy → act → record) ─► verified trajectory
          Planner (Gemini | Claude) · SurfaceAdapter (web | legacy-web | desktop) · PolicyEngine · Redactor
     ─► Compiler ─► CapabilityArtifact (draft) ─► validate (replays) ─► approve (hash-bound)
agent/CLI ─► ReplayEngine (pre-flight → steps → success condition) ─► RunResult
          state catalog · tenant overrides · lease-gated adapter · InterventionManager + operator API
```

- **Perception is accessibility-first**, across frames; unlabeled legacy inputs are named from the
  adjacent cell. The model picks numbered controls on a masked set-of-marks screenshot and never
  writes selectors; the adapter builds a locator bundle from the live element ([ADR 002](docs/adr/002-perception-and-targeting.md)).
- **The planner is an interface** ([ADR 003](docs/adr/003-llm-planner.md)): one request per decision
  with a closed tool. The committed runs use Gemini's free tier (`gemini-3.8/3.7/3.6-flash`, because
  of per-model daily quotas); Claude is a drop-in. `done` is verified by code on the live page, and
  values are read by code, never by the model.
- **No model in replay**, enforced by a test that `replay/` cannot import the LLM layer.
- **The target is a synthetic legacy app** ([ADR 001](docs/adr/001-synthetic-legacy-target.md)), so
  every runtime condition can be triggered on demand. It proves the mechanism, not robustness against
  a real vendor's quirks.

## 2. Artifact schema

Canonical JSON validated by `schemas/capability.schema.json`, rendered for reviewers as `.review.md`
([ADR 004](docs/adr/004-artifact-schema-and-versioning.md)).

- **Contract:** `inputs`/`outputs` as JSON Schema with sensitivity tags, so a contract is directly a
  tool definition (`cua capabilities tools`); typed money; declared `business_outcomes`
  (`member_not_found`, `validation_rejected`); `side_effects`.
- **Flow:** per step, an action, a value binding (input, vocabulary key or reviewed literal; never a
  concrete value), a ranked locator bundle, risk class, idempotency, pre/postconditions from a closed
  vocabulary, timeout, retry policy, step-scoped state mappings (`search_no_results` →
  `member_not_found`) and provenance (`model`, `human`, `authored`). `success` is a set of conditions,
  including output-to-input consistency (the displayed member ID equals the input).
- **Governance:** a policy that can only narrow, compatibility (app profile, product versions),
  provenance (run, model, prompt hash), a content hash, and an append-only lifecycle (draft →
  validated → approved → deprecated) whose approvals bind to the hash. Semver: major = contract.

An agent needs a contract, a reviewer needs to see how each control is found and what can go wrong,
and replay needs conditions it can evaluate without judgment. Labels are vocabulary keys, so one
artifact serves tenants that name things differently.

## 3. Determinism & error handling

Locator candidates are tried in rank order: zero matches falls through and records
`locator_fallback_used`; more than one stops with `locator_ambiguous`, because a weaker fallback must
never choose among duplicates. Each step waits by polling an explicit postcondition, never a fixed
delay (the desktop adapter adds only a 100 ms repaint settle). Login is a deterministic profile routine, a fingerprint rejects unsupported versions, and
pre-flight validates artifact, inputs and policy before a session opens. While waiting, the engine
watches the app profile's **state catalog** ([ADR 005](docs/adr/005-state-catalog.md)):

| Class | Examples | Behaviour |
|---|---|---|
| Rejected | `input_contract_violation`, `artifact_not_approved`, `policy_denied_preflight` | Before a session opens |
| Business outcome | `member_not_found`, `validation_rejected` | A result, not an error |
| Recoverable | `slow_load_waited`, `interstitial_dismissed`, `dialog_handled`, `transient_retry` (idempotent, max 2), `session_reauthenticated` (once, nothing committed) | Named, bounded, reported |
| Hard failure | `permission_denied`, `locator_ambiguous`/`not_found`, `timeout`, `app_error`, `checkpoint_mismatch`, `policy_violation`, `unknown_state` | Stop with evidence |
| Aborted | `operator_abort`, `intervention_timeout` | A human or the SLA ended it |

Failures carry the step, expected and observed state, per-candidate match counts, recovery history,
`retryable`, `side_effect_state` and masked evidence. Unknown states are never guessed: they escalate
or fail, and are mined into reviewable catalog candidates.

## 4. Heterogeneity & multi-tenant

**Surface seam** ([ADR 009](docs/adr/009-heterogeneous-surfaces.md)). Artifacts, conditions, policy,
lease and engine speak normalized concepts (frame path, role and name, label anchor); the surface
belongs to the tenant deployment, and each locator declares which adapters can read it. Three
adapters are built: browser, legacy web (the product's `<frameset>` UI, on which the tenant-a
artifact replays unchanged) and a Windows desktop client via UI Automation, where Gemini discovered a
capability that replays through the same engine. Not built: OCR for inaccessible controls.

**Multi-tenant reuse** ([ADR 010](docs/adr/010-tenant-overrides.md)). One base artifact per product
and capability; one app profile per product (catalog, risk rules, sensitive fields, vocabulary);
tenant profiles for URL, surface, version and vocabulary. **Override patches** are locator-only,
content-hashed and approval-bound, applied at pre-flight and reported in every result. Tenant-b
(different labels and a structurally different search button) runs the base artifact with one
override; the evidence includes the failure without it.

**Drift** ([ADR 012](docs/adr/012-assisted-fallback-drift-mining.md)). The fingerprint blocks
unsupported versions; a re-captioned control succeeds through a lower-ranked locator and emits a
drift signal, and `cua drift report` alerts per capability × tenant before failure rates move.

## 5. Escalation & handoff

**Stuck** means: in discovery, a planner that gives up or loops, no screen change after three
actions, or exhausted budgets; in replay, an unknown state, a locator failure, a postcondition
timeout, or an irreversible step needing approval. The **intervention** carries the goal or
capability, step, bound action and hash, expected and observed state, a masked screenshot, who is and
who should be in control, and SLAs; it goes to the console, an outbox and a signed webhook.

**Control** ([ADR 007](docs/adr/007-control-transfer.md), [ADR 011](docs/adr/011-operator-identity-and-live-view.md))
is a **fenced lease** whose version is the token: pausing invalidates the automation's token, claims
are compare-and-set, and every adapter action must present the current token. Operator calls are
authenticated by bearer token and authorized by role and tenant scope (approvals need a supervisor).
The lease holder gets a live view and acts on the *same* page through the console, where each click,
keystroke and key press is policy-checked against its target before it is sent. The headed window is
**fenced in the page itself**: it accepts direct input only while a human holds an unexpired lease
(expiry is checked in the page), so a person cannot act before claiming, after handing back, or after
the lease lapses, nor interfere with the automation. Direct headed input is lease-fenced and
journaled, but not policy-checked per action; that is the console's job. `evidence/replay-handoff-human`
records a real person taking over a paused run this way and handing it back.

**Handback** runs resume-point resolution: continue after the furthest step whose postcondition
holds, else retry the paused step, else escalate again; an irreversible step is never skipped unless
the journal shows the human did it, and the engine always verifies success itself. Human steps from
discovery compile into the artifact as `provenance: human`, flagged for review.

## 6. Safety

- **Allowlist** of routes and action types (global ∩ tenant ∩ artifact, so artifacts only narrow),
  checked before every automated, recovery and console action, and on every request, including
  redirect hops, so even direct headed input cannot reach a denied route. Pop-ups are closed.
- **Risk** is judged per bound action on a specific control (profile rules, default-deny for
  unreviewed POST submits and unknown dialogs). **Irreversible** actions are blocked in discovery,
  validation and recovery; in replay they need a single-use approval bound to the run, step and
  action hash ([ADR 006](docs/adr/006-risk-and-irreversible-actions.md)).
- **Data** ([ADR 008](docs/adr/008-data-minimization.md)): credentials live only in the secret
  provider; model input is masked and never contains input values; events are redacted at
  construction; persisted outputs are masked. Evidence export is gated by a leak scanner and fails
  closed on files it cannot read; Playwright traces (unmasked) are never exported.

**Limits.** Masking and risk rules are only as good as the reviewed profile. Operator identity is
token-based, not SSO. The fence has a millisecond window while the automation dispatches its own
input, and a native desktop window cannot be fenced (operators act there only through the API).

## 7. Cuts

**Built beyond the core**, with tests and evidence: legacy-web and desktop adapters, tenant
overrides, operator authentication and live view, webhook routing, drift and catalog mining, and two
stretch goals (the agent tool catalog and a bounded, opt-in assisted fallback).

**Left out:** model use in replay beyond that opt-in step; automatic artifact changes on drift (fixes
arrive as unapproved proposals); distributed infrastructure; SSO; OCR for inaccessible controls.

**Next:** a review workflow promoting catalog candidates; scheduled canary replays per tenant ×
version; SSO behind a streaming gateway; an AT-SPI adapter for Linux desktops.
