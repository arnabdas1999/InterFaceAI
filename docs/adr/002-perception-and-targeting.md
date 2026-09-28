# ADR 002 - Perception and targeting

**Decision.** The model sees a masked screenshot with numbered set-of-marks plus a control inventory
built across all frames (role + accessible name; for unlabeled legacy inputs, the text of the adjacent
table cell). It chooses controls by ephemeral number only. At action time the adapter records a locator
bundle from the live element and keeps only candidates that resolve uniquely to that same element
(candidate agreement). Candidates are ranked by a fixed rubric: role+name 0.9, label-anchor 0.8-0.85,
stable attribute 0.7, exact text 0.6, structural path 0.3, coordinates 0.1 (review required).

**Replay rule.** Try candidates in rank order; zero matches -> next candidate (and emit a
`locator_fallback_used` drift signal if it succeeds); more than one match -> stop with
`locator_ambiguous` (a weaker fallback must not silently choose among duplicates of the strongest
identity signal).

**Alternatives.** Provider coordinate-based computer use (does not compile into semantic, replayable
locators); model-written CSS/XPath (brittle, and the model would need raw DOM with sensitive values).

**Consequences.** Works on markup with no clean DOM; the same bundle carries `desktop-uia` or `visual`
candidates for other adapters. Structural fallbacks are brittle by nature and are flagged for review
when they are the only option.
