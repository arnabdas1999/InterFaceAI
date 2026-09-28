# ADR 012 - Assisted fallback, drift reporting, and catalog mining

**Assisted fallback (opt-in).** When a recorded control is missing (`locator_not_found`, never
ambiguity), replay may ask a model once per run which control on the masked screen performs exactly
the recorded step. Guards: never for irreversible steps; the suggestion must be the same action and
match the step's expected role; the normal policy check and the step's postcondition still apply; the
value always comes from the artifact binding. The new locator is written as an **unapproved** override
proposal; the artifact is never modified. Replay itself still imports no model client - the advisor is
injected through a protocol by the caller.

*Why bounded and not open-ended:* the point of replay is that no model decides anything in production.
One recorded, reviewable suggestion turns a hard failure into a success plus a review task; an
open-ended agent in the loop would make every run's behavior unreviewable.

**Drift report.** Aggregates replay/validation results per capability x tenant: status counts,
failure codes, business outcomes, recoveries, overrides applied, and drift signals. Fallback-locator use
(`locator_fallback_used`) and assisted fallbacks raise alerts before failure rates move.

**Catalog mining.** When a run stops on an unknown state, the page's blocking overlay (or heading) is
summarized, redacted, and stored as a catalog candidate with a suggested detector and handler; repeats
accumulate on the same candidate. Overlays with an attestation checkbox are marked as needing a human
decision rather than auto-dismissal. Promotion into the app profile is a reviewed engineering change.
