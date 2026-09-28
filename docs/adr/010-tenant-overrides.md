# ADR 010 - Tenant reuse: vocabulary first, reviewed override patches second

**Decision.** One base artifact per vendor product and capability. Tenants differ in three layers,
cheapest first:

1. **Vocabulary** (tenant profile): labels are stored as keys (`vocab:share_savings`), resolved per
   tenant ("Share Savings" vs "Savings").
2. **Override patches** (`config/overrides/<tenant>/…json`): locator-only deltas (`replace_candidates`,
   `prepend_candidate` for a step or extractor target). They cannot touch the contract, policy, risk,
   steps, or success condition, so a specialization can never widen what a capability does.
3. **Re-record** as a new version when the flow itself differs.

Overrides are content-hashed and applied only when an approval is bound to that hash
(`cua overrides approve`). Replay applies them at pre-flight, reports each applied `ref#hash` in the
result, and keeps the base artifact's hash as the capability identity. An override edited after
approval is refused (`artifact_invalid`) rather than silently ignored.

**Evidence.** `replay-tenant-b` (vocabulary + one override for a `<button>Find</button>` that replaced
the stock `Search` input), and `replay-tenant-b-no-override` (the same run without it fails with
`locator_not_found` and per-candidate match counts).

**Consequences.** Overrides accumulate per tenant; the drift report makes their use visible, and an
override applied broadly is a signal to promote the change into vocabulary or a new base version.
