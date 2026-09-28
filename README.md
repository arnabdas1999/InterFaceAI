# Computer-use automation: discover once, replay deterministically

An LLM operates a legacy back-office web app to accomplish a natural-language goal. The verified
run is compiled into a typed, versioned, reviewable capability artifact. Deterministic replay then
executes that artifact with new inputs and no model in the loop, returning typed outputs, a
business outcome, or a debuggable failure. Throughout, it enforces an allowlist, keeps irreversible
actions behind a human approval, keeps sensitive data out of the model and the logs, and can hand
the live browser session to a human and take it back.

The target is **SynthCore**, a local synthetic stand-in for a legacy core-banking teller app. It has
nested tables, unlabeled inputs, an iframe account panel, a native `confirm()`, and faults you can
inject deterministically. Design write-up: [REPORT.md](REPORT.md). Decision records: [docs/adr/](docs/adr/). What each
evidence folder proves: [evidence/README.md](evidence/README.md).

## Setup

Tested on Windows 11 (PowerShell) and Git Bash. Any POSIX shell works the same way. You need
Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                   # creates .venv from uv.lock
uv run playwright install chromium
cp .env.example .env                      # PowerShell: Copy-Item .env.example .env
```

`.env` is git-ignored. Only **`cua discover`** needs a model key. Replay, validation, the handoff,
the tests, and the evidence replays run without one. Pick a planner in `.env`:

- **Gemini, free tier (the default in `.env.example`):** get a key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY`. It
  uses Gemini's OpenAI-compatible endpoint. Free-tier data may be used by Google to improve its
  products; here everything sent is synthetic and masked.
- **Claude:** set `CUA_PLANNER=anthropic` and `ANTHROPIC_API_KEY`.
- **Any other OpenAI-compatible endpoint (Ollama, Groq, OpenRouter):** set
  `CUA_PLANNER=openai_compat`, `CUA_OPENAI_BASE_URL`, `CUA_OPENAI_API_KEY` and `CUA_MODEL`.

`uv run cua llm check` shows which planner and model discovery will use, and lists the models
your key can see. The other values in `.env.example` work as-is: `CUA_SECRET_SYNTHCORE_OPERATOR_*`
is a synthetic operator account for the local target.

| Variable | Used by | Purpose |
|---|---|---|
| `CUA_PLANNER` | `discover` | `gemini` (default in the template), `anthropic`, or `openai_compat` |
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` / `CUA_OPENAI_*` | `discover` | Model access for the chosen planner |
| `CUA_MODEL` | `discover` | Planner model (defaults: `gemini-3.8-flash`, `claude-sonnet-5`) |
| `CUA_MAX_RUN_TOKENS` | `discover` | Hard token budget per discovery run (default 400k) |
| `CUA_SECRET_SYNTHCORE_OPERATOR_USERNAME` / `_PASSWORD` | login routine | Synthetic credentials, read only by the secret provider |
| `CUA_HMAC_KEY` | logs | Key for correlation hashes in logs (auto-generated under `.cua/` if unset) |
| `CUA_OPERATOR_PORT` | handoff | Port of the live run's operator API (default 8766) |
| `CUA_OPERATOR_API_TOKEN` | `cua operator ...` | The operator's bearer token (`cua operator token create <id>`) |
| `CUA_WEBHOOK_URL`, `CUA_SECRET_WEBHOOK_SIGNING_KEY` | handoff | Optional signed webhook routing for interventions |
| `CUA_DESKTOP_FAULT` | desktop | Out-of-band fault for the desktop client (e.g. `permission_denied`) |

## Demo path

Terminal A runs the synthetic target:

```bash
uv run cua target serve                    # http://127.0.0.1:8765 (operator1 / synthetic-only-pass)
```

Terminal B runs the flow:

```bash
# 1. Genuine LLM discovery -> verified -> compiled draft artifact under capabilities/
uv run cua discover --request requests/read-savings-balance.json --example member_id=12345

# 2. Automatic validation replays (discovery input, a second good input, a negative input) -> "validated"
uv run cua validate read-savings-balance --version 1.0.0 --good member_id=12345 --good member_id=67890 --negative member_id=99999:member_not_found

# 3. Human sign-off, bound to the content hash -> "approved"
uv run cua approve read-savings-balance --version 1.0.0 --approver you
uv run cua capabilities show read-savings-balance --version 1.0.0 --review    # what a reviewer reads

# 4. Deterministic replay with a new input (no model) -> typed money output
uv run cua replay read-savings-balance --input member_id=67890
```

The repository already contains approved artifacts from the committed genuine run, so step 4
works right after `uv sync`. If you re-run step 1, it records the next free version (for
example `1.1.0`); use that version in steps 2 and 3.

### Outcomes, recoveries, failures

Faults are armed out-of-band. The automation can never reach `/__admin`.

```bash
uv run cua replay read-savings-balance --input member_id=99999        # business_outcome member_not_found (exit 10)
uv run cua replay read-savings-balance --input member_id=12ab         # rejected pre-flight, no browser opened (exit 20)

uv run cua target faults set slow_search
uv run cua target faults set system_notice
uv run cua target faults set http_500 --param route=lookup
uv run cua target faults set session_expire --param skip=2
uv run cua replay read-savings-balance --input member_id=24680        # success; recoveries: re-auth, slow load, retry, interstitial

uv run cua target faults set permission_denied
uv run cua replay read-savings-balance --input member_id=12345        # failure permission_denied, non-retryable (exit 30)
```

Other faults: `duplicate_control` (ambiguous locator), `unknown_modal` (escalation), `popup`,
`offsite_redirect`, and `http_500 --count -1` (persistent error). `uv run cua target reset`
restores seed data and clears faults.

### Human handoff on the same live session

```bash
# terminal B: the run pauses and prints an intervention id + console URL
uv run cua target faults set unknown_modal
uv run cua replay read-savings-balance --input member_id=12345 --escalate --headed

# terminal C: the operator (operators are declared in config/operators.json; identity comes from the token)
uv run cua operator token create alice              # prints a token once; only its hash is stored (.cua/)
export CUA_OPERATOR_API_TOKEN=<token>               # PowerShell: $env:CUA_OPERATOR_API_TOKEN="<token>"
uv run cua operator status                          # who is in control now, and who should be
uv run cua operator claim <iv-id>                   # returns a lease token
#   ...act in the headed browser window (it accepts input only while your lease is live), or open
#   http://127.0.0.1:8766/console/<iv-id>, sign in with the API token and claim: the console switches
#   from a masked snapshot to a LIVE view of the real screen (streamed only to the lease holder);
#   click on it to act. Or: cua operator controls / cua operator act ...
uv run cua operator resolve <iv-id> resume --token <lease-token> --note "attested"
```

Set `CUA_WEBHOOK_URL` and `CUA_SECRET_WEBHOOK_SIGNING_KEY` to also route interventions to a signed
webhook. Each request carries a timestamp and an HMAC-SHA256 signature, which the receiver checks with
`interface_cua.handoff.notifier.verify_signature`.

The run resumes on the same session after the furthest step whose postcondition holds, then
verifies success itself.

### Irreversible step with approval

```bash
uv run cua replay open-sub-account --input member_id=67890 --input product=share_certificate --input "nickname=CD Ladder" --escalate
uv run cua operator token create supervisor-1   # approving needs the supervisor role; export it as CUA_OPERATOR_API_TOKEN
uv run cua operator resolve <iv-id> approve     # single use, bound to the exact action hash
uv run cua target ledger                                           # exactly one commit
```

Without `--escalate`, this capability is rejected pre-flight: it needs a human approval.

### Other tenants, other surfaces, drift

Start the other deployments in extra terminals: `uv run cua target serve --port 8775 --tenant tenant-b`
(same product, different vocabulary, and a structurally different search button) and
`uv run cua target serve --port 8785 --tenant tenant-c` (classic frameset UI).

```bash
uv run cua replay read-savings-balance --tenant tenant-b --input member_id=67890   # vocabulary + reviewed override patch
uv run cua overrides list --tenant tenant-b                                        # locator-only, hash-approved
uv run cua replay read-savings-balance --tenant tenant-c --input member_id=67890   # legacy-web adapter anchors to the content frame
uv run cua replay read-savings-balance --tenant tenant-b --input member_id=67890 --assisted-fallback
#   opt-in: if a recorded control is missing, one bounded, policy-checked model suggestion; the fix is
#   saved as an UNAPPROVED override proposal in the run folder (needs a model key)

uv run cua target faults set caption_drift
uv run cua replay read-savings-balance --input member_id=12345     # succeeds via a fallback locator, reports drift
uv run cua drift report --out runs/drift-report.md                  # per capability x tenant; alerts on fallback use
uv run cua catalog candidates                                       # unknown states met at run time, with suggested catalog entries
```

**Desktop (Windows).** Tenant `tenant-d` is a Windows Forms client driven through UI Automation. It
launches itself; there is no server to start.

```bash
uv run cua discover --request requests/desktop-read-savings-balance.json --example member_id=12345   # genuine model run
uv run cua validate desktop-read-savings-balance --version 1.0.0 --tenant tenant-d --good member_id=12345 --good member_id=67890 --negative member_id=99999:member_not_found
uv run cua approve desktop-read-savings-balance --version 1.0.0 --approver you
uv run cua replay desktop-read-savings-balance --tenant tenant-d --input member_id=67890
```

### Everything at once

```bash
uv run python scripts/make_evidence.py                  # genuine discovery (needs a model key) + every replay scenario -> evidence/
uv run python scripts/make_evidence.py --skip-discovery # reuse committed artifacts; no key needed
```

## Running without live services

- `uv run pytest` covers 59 unit tests and 52 integration tests (3 of them Windows-only). The
  integration tests drive the real browser against the local target. Discovery tests use a scripted planner, so no API key or
  network is needed.
- Replay never calls a model. A test enforces this: `interface_cua.replay` cannot import
  `interface_cua.llm`.
- `uv run cua discover ... --fake` runs the discovery loop with the scripted planner for the
  balance goal. It is a smoke test only and is never used as evidence.

## Where things go

| Path | Contents |
|---|---|
| `capabilities/<id>/<version>.json` + `.review.md` | Artifacts (immutable content; append-only lifecycle) |
| `runs/<run_id>/` (git-ignored) | `events.jsonl`, `run-result.json`, masked screenshots, sanitized DOM, `trajectory.json`, local-only `trace.zip` |
| `evidence/` | Sanitized copies exported through the leak scanner |
| `apps/synthcore/profile.json` | App profile: state catalog, risk rules, sensitive-field map, login routine, fingerprint |
| `config/` | Global policy and tenant profiles |
| `schemas/` | JSON Schemas generated from the models (`scripts/gen_schemas.py`) |

**CLI exit codes:**
- `0` success
- `10` business_outcome
- `20` rejected
- `30` failure
- `40` aborted
- `2` usage error

The JSON result is printed to stdout, and the caller receives real output values. The persisted
`runs/<id>/run-result.json` masks sensitive outputs.

## Development

```bash
uv run pytest                 # unit + integration (about 4 minutes)
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy                   # strict on domain/, policy/, replay/
uv run cua capabilities tools # approved capabilities as agent tool definitions (contract = JSON Schema)
```

## Troubleshooting

- **Port 8765 or 8766 in use:** stop the other server, or set `CUA_OPERATOR_PORT`. Tenant ports
  live in `config/tenants/*.json`.
- **Headed mode:** `--headed` opens a visible Chromium window. The handoff also works headless
  through the console.
- **OneDrive or other synced folders:** keep the repo outside them, or exclude `.venv/` and
  `runs/`. File locks break Playwright and SQLite.
- **Windows console encoding:** the CLI forces UTF-8 output. Masked values print as `⟦…⟧`.
