# ADR 006 - Risk classification and irreversible actions

**Decision.** Risk is judged per bound action on a specific control: app-profile rules (captions such as
"Open Account"/"Close Account", commit routes), plus default-deny (a POST submit to a route not
reviewed as safe, or accepting a dialog not catalogued as safe, is irreversible). A model's declared
intent and an artifact's declared risk can only raise the class.

| Mode | Irreversible action |
|---|---|
| Discovery | Denied. Discovery learns flows; it never performs financial side effects. |
| Validation | Denied; validation stops before the first irreversible step (`stop_before_irreversible`). |
| Recovery handlers | Denied. |
| Replay, `escalate` | Pauses for a single-use approval bound to run + session + step + bound-action hash (expires in 5 min). |
| Replay, `fail_fast` | Rejected pre-flight (`policy_denied_preflight`, approval required): nothing runs. |

**Where irreversible steps come from.** Never from the model acting. They are authored patches
(`apps/synthcore/authored/*.json`), reviewed, compiled into a new major version with
`provenance: authored`, `review_required`, and a risk the compiler recomputes (an author cannot
under-declare).

**Alternatives.** Block irreversible actions outright (makes write capabilities useless); flag only
(allows unattended side effects in a regulated system).

**Consequences.** Retries never apply to non-idempotent steps; every failure reports `side_effect_state`.
Pattern-based rules can misclassify a mislabeled control; default-deny plus per-artifact human review is
the mitigation.
