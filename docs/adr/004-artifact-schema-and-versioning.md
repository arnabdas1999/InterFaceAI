# ADR 004 - Artifact schema and versioning

**Decision.** A capability artifact is canonical JSON with: `capability` (id, semver), `target` (vendor
product, app-profile range, entry route handle, surface type, adapter requirements), `contract` (JSON
Schema inputs/outputs with `x-sensitivity`, declared business outcomes, side-effect class), `policy`
(narrow-only), `steps` (action, locator bundle, value binding, risk, idempotency, pre/postconditions,
timeout, retry, step-scoped state mappings, transitions, re-entry flag, provenance, review flags),
`extract` (label-anchored value cells + type + normalizer), `success` (conditions incl. output-to-input
consistency), `review`, `provenance`, `compatibility`, `content_hash`, `lifecycle`.

Conditions come from a closed vocabulary (`url_matches`, `element_present/absent`, `text_in_region`,
`frame_loaded`, `dialog_present`, `http_status`, `all_of/any_of/not`). No free-form code.

**Hash and lifecycle.** `content_hash` = SHA-256 of canonical JSON excluding `content_hash` and
`lifecycle`. The lifecycle (draft -> validated -> approved -> deprecated) is append-only and each
approval records the hash it approved, so an edited artifact can never inherit an approval. Versions are
immutable on disk (the store refuses to overwrite). Major = contract/side-effect change, minor = flow
change, patch = metadata.

**Why the contract is JSON Schema.** It is directly a tool definition for a calling agent
(`cua capabilities tools`), and it is what pre-flight validates inputs against.

**Consequences.** The schema is larger than a step list, but a reviewer and an agent can each answer
"what does it do, what does it need, what does it return, what can go wrong" from the file alone
(the generated `.review.md` renders exactly that).
