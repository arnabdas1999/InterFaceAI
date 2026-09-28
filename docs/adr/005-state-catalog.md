# ADR 005 - State catalog per vendor product

**Decision.** Knowledge of business outcomes, recoverable conditions, and hard failures lives in a
reviewed app profile per vendor product (`apps/synthcore/profile.json`), not in the model and not in
each capability. Each state has a deterministic detector, a classification, a scope (global, or
step-scoped with `applies_after_submit_on` routes), and at most one bounded handler (`dismiss`,
`dialog`, `reauthenticate`, `retry_step`). Capabilities map in-scope states to their own outcome codes
(`search_no_results` -> `member_not_found`).

**Why.** A single happy-path discovery run cannot observe "member not found", a permission error, or a
session timeout, so that knowledge cannot come from the recording. It is also shared by every capability
and tenant on the product, so it is authored and reviewed once.

**Consequences.** Unknown states are never guessed: they escalate (or fail with `unknown_state`).
Growing the catalog is an engineering task (seeded by negative-input exploration runs in production).
