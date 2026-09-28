# Architecture decision records

Short records of the decisions `REPORT.md` summarizes. Each states the decision, the alternatives
considered, and the consequence we accept.

| # | Decision |
|---|---|
| [001](001-synthetic-legacy-target.md) | Local synthetic legacy target ("SynthCore") instead of a public site |
| [002](002-perception-and-targeting.md) | Accessibility-first perception, handle-based actions, recorded locator bundles |
| [003](003-llm-planner.md) | Planner behind an interface (Gemini free tier or Claude), stateless per decision, never in replay |
| [004](004-artifact-schema-and-versioning.md) | Artifact = contract + steps + conditions; hash-bound lifecycle; semver rules |
| [005](005-state-catalog.md) | Outcome/recovery knowledge lives in a reviewed per-product state catalog |
| [006](006-risk-and-irreversible-actions.md) | Target-level risk; irreversible = blocked in discovery, approval-bound in replay, authored not learned |
| [007](007-control-transfer.md) | In-process operator API + fenced control lease + resume-point resolution |
| [008](008-data-minimization.md) | Redact at construction, reference-preserving masking for the model, no unmasked artifacts committed |
| [009](009-heterogeneous-surfaces.md) | Legacy-web (frameset) and desktop (UI Automation) adapters behind the same contract |
| [010](010-tenant-overrides.md) | Tenant reuse: vocabulary first, reviewed hash-approved locator overrides second |
| [011](011-operator-identity-and-live-view.md) | Operator authN/Z, live view for the lease owner only, signed webhook routing |
| [012](012-assisted-fallback-drift-mining.md) | Bounded opt-in assisted fallback, drift report, catalog mining |
