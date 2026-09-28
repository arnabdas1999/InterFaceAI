# ADR 007 - Control transfer (handoff) model

**Decision.** The process that owns the browser serves the operator API on the same event loop, so no
Playwright object crosses a process boundary. Control is a lease whose version is a fencing token:

```
AUTOMATION_ACTIVE -> PAUSING -> HUMAN_PENDING -> HUMAN_ACTIVE -> RESUMING -> AUTOMATION_ACTIVE
HUMAN_PENDING -> ABORTED (claim SLA) ; HUMAN_ACTIVE -> HUMAN_PENDING (heartbeats lost) ; -> ABORTED (operator)
```

Every transition is compare-and-set on the version and bumps it; every surface action presents the
current token and the owner identity; the adapter rejects stale tokens. `expected_owner` records who
*should* be in control; `GET /runs/current/control` answers "who is in control now".

The human operates the **same** page through either the headed window (fenced in the page so it only
accepts input while the human's lease is live, and journaled by an init script in every frame) or the
console's remote-control surface (live view + click/type/press forwarded through the adapter with the
human's token, each policy-checked against its target first). See ADR 011 for the fence. Resolutions: `resume`, `resume_at_step`,
`complete` (the engine still verifies success and extracts outputs itself), `abort`, `approve`/`deny`.

**Resume-point resolution.** Re-observe, re-check the session, find the furthest step from the paused
one whose postcondition holds and resume after it; else retry the paused step if its precondition holds;
else open a new intervention. Never skip an irreversible step unless the journal shows the human did it.

**Consequences.** "Automation never acts during human ownership" is enforced, not conventional. The
local console trusts whoever can reach 127.0.0.1; production needs operator authN/Z and a CDP-screencast
console, with the same lease underneath.
