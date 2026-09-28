# Evidence index

Everything here was produced by `scripts/make_evidence.py` against the local synthetic target
(synthetic members, synthetic operator account). Every run folder was exported through a leak
scanner. The scanner checks for credentials, API keys, known synthetic names, full account
numbers, money and SSN patterns, and session tokens. The whole `evidence/` tree was re-scanned at
the end with zero findings. Machine-readable index: [index.json](index.json).

**How to read a run folder:**

| File | Contents |
|---|---|
| `run-result.json` | The result contract: status, codes, recoveries, drift, evidence refs. Sensitive outputs are masked with a keyed hash; the shape is kept |
| `events.jsonl` | The structured log, redacted as each event was built. Each event records the control owner and lease version |
| `screenshots/` | Masked screenshots. Discovery screenshots also carry the numbered set-of-marks the model saw |
| `dom/` | Sanitized DOM snapshots of every frame (the rich failure signal) |
| `caller-response.json` | What the calling agent received on stdout (real synthetic values), next to the masked persisted copy |

**Traces:** Playwright traces hold unmasked DOM and request headers, and the leak scanner cannot
read inside an archive, so the exporter never copies them: they stay local-only under `runs/`, and
their reference is removed from the exported `run-result.json`. The export fails closed: every file
in this folder is either a scanned text file or a screenshot masked at capture time.

## Genuine LLM discovery (the model in the loop)

| Folder | What it proves |
|---|---|
| [discovery-success/](discovery-success/) | **Gemini `gemini-3.8-flash` completes the goal on the live target.** It searched for member 12345, opened View Accounts (the native `confirm()` was accepted per the catalog), opened Share Savings inside the iframe, extracted the Current Balance, and claimed `done` with checkpoint checks. The claim was verified by code on the live page, and the extractor re-ran deterministically. **8 decisions, 16,554 input / 425 output tokens, 147 s.** `trajectory.json` is the executed-action record the compiler consumed (no transcript, no input values). `events.jsonl` shows every model decision with its token usage and latency, next to the policy decision and the executed action. |
| [discovery-open-sub-account/](discovery-open-sub-account/) | **Gemini `gemini-3.7-flash` reaches the review screen and stops without committing.** It typed the member ID, searched, and opened Open Sub-Account. It then selected the product and typed the nickname *by declared input name*, so both are parameterized, clicked Continue, and verified the review screen. **7 decisions, 18,769 input / 469 output tokens.** This run used a different Flash model only because the free tier caps each model at 20 requests per day, and the first model's quota was used up. |
| [discovery-desktop/](discovery-desktop/) | **Gemini `gemini-3.6-flash` drives a Windows desktop client through UI Automation.** It typed the member ID into an edit box with no accessible name (the adapter labelled it from the static text to its left), clicked Search, extracted the Share Savings Balance, and verified the "Member" value against the input. **5 decisions, 12,624 input / 379 output tokens, 33 s.** Screenshots are masked captures of the application window only. |

The planner configuration, prompt template hash, and model ID are recorded in each run and in the
artifact provenance.

## Artifacts

[artifacts/](artifacts/) holds each capability as JSON plus the `.review.md` a reviewer reads:
- `read-savings-balance@1.0.0` (approved)
- `desktop-read-savings-balance@1.0.0` (approved; `surface_type: desktop`, `desktop-uia` locator candidates)
- `open-sub-account@1.0.0` (discovered; ends at review)
- `open-sub-account@2.0.0` (v1 plus the reviewed authored commit step `c1`; `side_effects: irreversible`; approved)

No concrete member ID appears in any artifact.

## Validation and approval

[validation/](validation/) holds a report per capability plus every validation run.

| Capability | Mode | Runs | Result |
|---|---|---|---|
| `read-savings-balance@1.0.0` | full | 12345 (outputs identical to discovery), 67890, 99999 → `member_not_found` | passed → validated → approved |
| `open-sub-account@2.0.0` | `stop_before_irreversible` | two good inputs stop before `c1` with its target resolved; 99999 → `member_not_found` | passed → validated → approved. The ledger stayed empty: validation never commits |
| `desktop-read-savings-balance@1.0.0` (tenant-d) | full | 12345 (outputs identical to discovery), 67890, 99999 → `member_not_found` | passed → validated → approved |

## Deterministic replay (no model events in any of these logs)

| Folder | Scenario | Result |
|---|---|---|
| [replay-success/](replay-success/) | New input `67890` | `success`, `savings_balance = {"amount": "15020.00", "currency": "USD"}` (see `caller-response.json`); checkpoint verified |
| [replay-business-outcome/](replay-business-outcome/) | Unknown member `99999` | `business_outcome / member_not_found` at step `s2`. A legitimate answer, not an error |
| [replay-recovered/](replay-recovered/) | Session expiry + slow search + transient HTTP 500 + System notice modal, all in one run | `success` with recoveries `session_reauthenticated` (re-entered at `s1`), `transient_retry`, `interstitial_dismissed`, `slow_load_waited`, `dialog_handled` |
| [replay-hard-failure/](replay-hard-failure/) | Permission denied on the account panel | `failure / permission_denied`, `retryable: false`, `side_effect_state: none`, step `s3`. Expected "Click button 'View Accounts' to proceed"; observed "Access Denied \| You do not have permission to view account balances (ERR-SEC-403)". Masked screenshot + sanitized DOM attached |
| [replay-rejected/](replay-rejected/) | Malformed input `12ab` | `rejected / input_contract_violation` (`member_id` does not match `^\d{5}$`), `session_opened: false`. No browser was started and the input value is not echoed |

## Human escalation and same-session handoff

[replay-handoff-human/](replay-handoff-human/): **a real person** takes over the live session
through the operator console in a browser (the operator transcript gives the timeline).
- The unrecognized "Compliance attestation required" modal pauses the replay at `s3`.
- Two inputs to the headed window before claiming are swallowed by the input fence
  (`human_input_blocked`). The masked screenshots show its "Input locked" badge.
- The person signs in with an operator token, claims the session as `alice` (lease v3 → v4,
  `HUMAN_ACTIVE`), and on the live view ticks the attestation and clicks Acknowledge.
  Both are journaled as `human_action` with `source: remote_console`.
- The person hands back with `resume`. `intervention_resolved` shows `same_session: true`, with
  the same session ID throughout, and resume-point resolution retries `s3`.
- The replay finishes with `success`.

The same scenario, driven by a scripted operator through the same public API (repeatable, and
also used to record webhook and authorization checks):

[replay-handoff/](replay-handoff/): an unrecognized "Compliance attestation required" modal
covers View Accounts at step `s3`. The engine does not guess; it raises intervention
`intervention-*.json` of kind `unknown_state`, with expected/observed state, a masked screenshot,
and the control state.

The lease transitions in `events.jsonl` are:

`AUTOMATION_ACTIVE (v1) → PAUSING (v2) → HUMAN_PENDING, owner unassigned (v3) → HUMAN_ACTIVE, human:scripted-operator (v4) → RESUMING (v5) → AUTOMATION_ACTIVE (v6)`

- The operator ticked the attestation and clicked Acknowledge (2 `human_action` events,
  journaled by the adapter under the human's token).
- There were no automation events while a human owned the lease.
- `intervention_resolved` shows `same_session: true`, with the same session ID and browser context
  ID as `session_started`.
- Resume-point resolution chose `retry` of `s3`, and the run completed with success.
- The operator is a **scripted stand-in** that uses the same public operator API as the console,
  and is labelled as such in `operator-transcript.json`.
- `webhook-deliveries.json`: the intervention was routed to a signed webhook. It was delivered on
  the first attempt, and the receiver verified the HMAC signature and timestamp.
- `operator-auth-checks.json`: no token → 401, forged token → 401, an operator scoped to another
  tenant → 403, a plain operator approving → 403 (supervisors only), and a request body naming
  someone else → 403.

## Approval-bound irreversible step

[replay-approval/](replay-approval/): `open-sub-account@2.0.0` with `escalation_mode=escalate`.
- Automation reached the review screen, and policy returned `require_approval` for "Open Account".
- An `approval_required` intervention carried the exact bound-action hash. The supervisor approved
  by echoing that hash.
- The approval was consumed once (`events.jsonl`: "approval … consumed for c1 (single use,
  hash-bound)"), and automation performed exactly that click.
- `ledger-after.json` shows **exactly one** commit (`CNF-004821`, account masked to last 4). The
  outputs `confirmation_number` and `new_account_last4` were extracted and schema-validated.

## Stability

[stability/stability-report.json](stability/stability-report.json): 10 consecutive replays of
`read-savings-balance` for member 12345. **10/10 success, identical outputs, 0 drift signals,
2.0-6.8 s per run** (timing varies with machine load; there are no fixed sleeps).

## Heterogeneity, tenant reuse, drift

| Folder | Scenario | Result |
|---|---|---|
| [replay-tenant-b/](replay-tenant-b/) | The tenant-a artifact on **tenant-b**: same product, "Customer No." / "Savings" vocabulary, and a `<button>Find</button>` instead of the stock Search input | `success` (15020.00). `overrides` lists the one reviewed, hash-approved, locator-only patch it applied |
| [replay-tenant-b-no-override/](replay-tenant-b-no-override/) | Same, with the override removed | `failure / locator_not_found` at `s2`; observed `role_name=0; structural=0`. This is why the override exists |
| [replay-assisted-fallback/](replay-assisted-fallback/) | Same as above, with the opt-in `--assisted-fallback` | **One `gemini-3.6-flash` call** pointed at control "Find" (button, matching the recorded role). The run succeeded, and `proposed-override-s2.json` holds the new locator as an **unapproved** proposal; the artifact is unchanged |
| [replay-legacy-frameset/](replay-legacy-frameset/) | The tenant-a artifact on **tenant-c**, the product's classic `<frameset>` UI | `success`. The legacy-web adapter anchors frames, URLs and navigation to the application frame, and still masks the banner frame (operator name) |
| [replay-drift/](replay-drift/) | "View Accounts" re-captioned (`caption_drift` fault) | `success` through the structural fallback, with a `locator_fallback_used` drift signal at `s3` |
| [replay-unknown-state/](replay-unknown-state/) | Unknown modal with `fail_fast` | `failure / unknown_state` ("control obscured by an unknown overlay") with masked evidence; the state was mined into [catalog-candidates/](catalog-candidates/) |
| [replay-desktop/](replay-desktop/) | The desktop capability on tenant-d with a new input | `success` (15020.00) through UI Automation. Failures on this surface carry a sanitized UIA tree dump (`*.uia.json`) |
| [catalog-candidates/](catalog-candidates/) | Unknown states met at run time | One candidate, "Compliance attestation required": route `/members/:member_id`, 2 occurrences, buttons and checkbox captured. Suggestion: keep it as an escalation, since it needs a human decision. Status `needs_review` |
| [drift/](drift/) | `cua drift report` over every run in this folder | Per capability × tenant health. It alerts on tenant-a (fallback locator in `replay-drift`) and tenant-b (assisted fallback, plus the deliberate no-override failure). The failure rates include the deliberate failure scenarios above |
