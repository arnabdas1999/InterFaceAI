# ADR 001 - Synthetic legacy target

**Decision.** Build a local FastAPI app ("SynthCore") that imitates a legacy core-banking teller screen:
server-rendered nested tables, labels in adjacent cells (no `<label>`), no test IDs, an iframe account
panel, a native `confirm()`, legacy `jsessionid` in URLs, and out-of-band fault injection under
`/__admin` (never on any automation allowlist).

**Alternatives.** A public demo shop (clean DOM, terms/rate limits, no bank semantics, faults not
triggerable); a real banking sandbox (not available, and the brief says not to try).

**Consequences.** Every runtime condition in the scenario table can be triggered deterministically and
repeatably, which is what makes the error taxonomy testable. The price is that the target is ours, so
the evidence proves mechanism, not robustness against a real vendor's quirks. Two tenant brandings
(`tenant-a`, `tenant-b`) of the same product exist to exercise vocabulary-based reuse.
