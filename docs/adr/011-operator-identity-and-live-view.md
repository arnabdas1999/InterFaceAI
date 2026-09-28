# ADR 011 - Operator identity, authorization, notification, and the live view

**Identity.** Operators, roles (`operator`, `supervisor`), and tenant scopes are declared in
`config/operators.json`. Bearer tokens are random and only their SHA-256 hashes are stored (git-ignored
state). Identity always comes from the token; a request body naming another operator is rejected.
Production maps SSO groups onto the same roles and scopes; the per-call checks do not change.

**Authorization.** Every operator API call is authenticated. Viewing, claiming, acting, and handing
back need the `operator` role and the intervention's tenant in scope; approving or denying an
irreversible action needs `supervisor`. Listing only returns interventions the caller may serve.

**Visibility.** Before claiming: a masked snapshot (triage). While holding the lease: a live CDP
screencast of the real screen, streamed only to the lease owner and never persisted. The person
accountable for the session must see what they operate; everyone else sees masked context. The
desktop surface falls back to snapshots.

**Two ways to act, both fenced.** Through the console (or `cua operator act`), every click, keystroke
and key press is lease-checked and policy-checked against its target before it is sent: the click's
element, or the focused control for typing and keys (Enter in a form gets the form-target checks).
The headed browser window is fenced *in the page*: an init script in every frame swallows pointer,
keyboard, paste and drop input unless a human holds an unexpired lease (the manager pushes the lease
expiry on every change and heartbeat; the page compares it to the clock itself, so a lapsed lease
closes the window immediately) or the automation is dispatching its own lease-checked action. A
person therefore cannot act before claiming, after handing back, after the lease lapses, or while
automation runs; blocked attempts are logged as `human_input_blocked`. Direct headed input is
attributed and journaled but not policy-checked per action (the request-level allowlist still
applies to every navigation it causes); the console is the policy-checked path. Residual gap: the
fence opens for the few milliseconds the automation dispatches its own input. A native desktop
window cannot be fenced from outside, so desktop operators act only through the API.

**Routing.** Interventions fan out to console, a JSONL outbox, and a signed webhook
(`X-CUA-Signature: sha256=HMAC(key, timestamp.body)`, `X-CUA-Timestamp` for replay protection),
with bounded retries. Routing runs off the event loop and a failed channel never blocks escalation.
The webhook carries a routing summary only - no screenshot or page content.

**Evidence.** `replay-handoff/operator-auth-checks.json` (401/403 cases) and
`replay-handoff/webhook-deliveries.json` (receiver-verified signatures). Tests: input to the headed
window is swallowed before a claim and after the lease lapses and accepted only in between; remote
key presses are denied before execution against the focused control.
