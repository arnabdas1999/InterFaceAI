# ADR 008 - Data minimization and redaction

**Decision.**
- **Credentials** live only in the secret provider (`SecretValue` cannot be printed or pickled) and are
  used only by the deterministic login routine. Traces start after login.
- **Model egress** is masked before it leaves the process: values equal to a declared input become
  `⟦input:name⟧` (reference-preserving, so the model can confirm it reached the right record), other
  sensitive values `⟦masked:kind⟧`; screenshots get flat boxes over the same regions. Routes are masked
  too (`/members/12345` carries the input). The model never sees input values; code substitutes them.
- **Persistence** is redacted when each event is constructed (never a post-pass): keyed-hash tokens
  (`⟦input:member_id#3fa91c⟧`) for correlation, sensitive keys (`password`, `cookie`, `authorization`...)
  dropped, session parameters stripped from URLs. Persisted run results keep the output *shape* but mask
  sensitive leaves; the caller receives real values on stdout only.
- **Evidence export** is gated by a scanner (secrets, known synthetic names, account-number/SSN/money
  patterns, bearer tokens, session ids). It fails closed: an exported file must be a scanned text
  file or a screenshot masked at capture time, so a binary it cannot read blocks the export.
  Playwright traces hold unmasked DOM and headers and the scanner cannot read inside them, so the
  exporter never copies them (there is no flag to) and drops their reference from the exported
  result; they stay local-only under git-ignored `runs/`.

**Limits.** Masking is only as good as the sensitive-field map; unmapped PII on an unusual page could
reach the model or a screenshot. Mitigations: synthetic/sandbox tenants for discovery, provider
zero-data-retention terms, and redaction-coverage tests per app profile.
