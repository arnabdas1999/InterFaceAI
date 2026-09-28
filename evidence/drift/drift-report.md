# Replay drift report

23 replay/validation runs.

| Capability | Tenant | Runs | Success | Failure rate | Recoveries | Drift signals | Overrides | Alerts |
|---|---|---|---|---|---|---|---|---|
| `desktop-read-savings-balance@1.0.0` | tenant-d | 4 | 3 | 0% | - | - | 0 | - |
| `open-sub-account@2.0.0` | tenant-a | 4 | 3 | 0% | - | - | 0 | - |
| `read-savings-balance@1.0.0` | tenant-a | 11 | 6 | 20% | dialog_handledx7, session_reauthenticatedx1, transient_retryx1, interstitial_dismissedx1, slow_load_waitedx1 | s3:locator_fallback_usedx1 | 0 | locator drift: fallback candidates in use - review and re-validate the locators (new minor version) |
| `read-savings-balance@1.0.0` | tenant-b | 3 | 2 | 33% | assisted_fallbackx1, dialog_handledx2 | s2:assisted_fallbackx1 | 1 | locator drift: fallback candidates in use - review and re-validate the locators (new minor version); failure rate 33% above 20% |
| `read-savings-balance@1.0.0` | tenant-c | 1 | 1 | 0% | dialog_handledx1 | - | 0 | - |
