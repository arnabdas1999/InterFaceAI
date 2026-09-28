# ADR 003 - LLM planner

**Decision.** The planner is an interface with two implementations selected by `CUA_PLANNER`:
Claude through the official Anthropic SDK (strict tool use), and any OpenAI-compatible endpoint. The
evidence run uses the second with Google Gemini's free tier, because the author chose not to pay for
API access; nothing else in the system depends on the provider. Providers with a JSON-Schema subset
get a plain ("portable") version of the same `act` schema, and every response is validated against
the same `DiscoveryDecision` model either way. The rest of this record describes the Claude path; the
OpenAI-compatible path differs only in transport.

Claude path: `claude-sonnet-5` by default (`CUA_MODEL` to change).
One stateless request per decision: cached system prompt + one strict `act` tool, then a compact action
history and the current masked observation. `tool_choice: auto` + `strict: true` (forced tool choice is
rejected by newer models); a response without a valid `act` call is an invalid decision (bounded
retries, then "stuck"). Effort `medium`; a per-run token budget is a hard stop.

The model can express: control number, declared input *name*, a non-sensitive literal, an allowed key,
a route handle, an extract request (output name/type + visible label), and a `done` claim with proposed
label/value checks. It cannot express selectors, URLs, code, or values of declared inputs.

**Verification.** A `done` claim is verified deterministically: checks evaluated on the live page,
extractors re-run by code (the model never reads values), outputs tied to inputs.

**Isolation from replay.** `interface_cua.replay` never imports `interface_cua.llm` (test-enforced) and
holds a stub that raises if touched.

**Consequences.** Stateless calls keep cost linear and prompts cache-friendly; the price is that the
model's memory is the history summary we render, which is deliberately short.
