# ADR 009 - Heterogeneous surfaces: legacy-web and desktop adapters

**Decision.** The surface is a property of the *tenant deployment* (`surface` in the tenant profile),
not of the capability. Three adapters implement the same contract (observe, resolve, bind, perform,
evaluate, read, capture evidence, lease-gated input):

| Adapter | Surface | How it perceives and targets |
|---|---|---|
| `BrowserAdapter` | web | Accessibility-first inventory across frames; adjacent-cell labels for unlabeled inputs |
| `LegacyWebAdapter` | legacy-web (framesets) | Same, but frame paths, URL checks and navigation are anchored to the tenant's application frame (`root_frame`); every frame of the shell is still masked |
| `DesktopAdapter` | desktop (Windows) | UI Automation tree; unnamed controls take the static text to their left as label (geometry); masked window screenshots; screens map to routes via the window title |

Locator candidates declare which adapter kinds interpret them (`web`, `desktop-uia`). Conditions,
steps, policy, results, the lease, and the replay engine are shared.

**Evidence.** The tenant-a artifact replays unchanged on tenant-c's classic frameset UI. A desktop
capability discovered on the WinForms client replays through the same engine with a new input, returns
the `member_not_found` outcome, and fails cleanly on an injected permission error with a sanitized UIA
tree dump as the failure signal.

**Consequences.** A web artifact cannot be replayed on the desktop client: different surfaces of the
same business flow are different capabilities (the same contract can be declared by both, so an agent
sees one tool with two implementations). Desktop capture renders the application window itself
(`PrintWindow`, scaled for DPI-unaware clients and cropped to the visible frame), never a screen
region: a screen grab of a window covered by another app would put that app's content into evidence
and model input. A test found exactly that when the window was behind a browser, so it is now a
regression test. The adapter also attaches to the window of the process it launched, never to any
window with a matching title. Pattern actions (`SetValue` with read-back, `Invoke`) work without focus;
synthesized mouse or keyboard input is refused unless the application is in the foreground.
Not built: OCR for controls with no accessible representation, non-Windows desktops (AT-SPI).
