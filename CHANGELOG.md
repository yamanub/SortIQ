## 2026-11-02 — File All and class selection

- Add File All next to queue controls for API-identified and user-classified cases in the displayed profile, with per-case failure reporting, backups and Undo.
- Remove the redundant review checkbox. Confirm & file uses the current File under class selection, even when it differs from the API suggestion.
- Persist user classifications across reloads/restarts; user selections take precedence for bulk filing and block automatic filing under a different API class.
- Display the actual filed class. Keep ambiguous/unidentified cases, other profiles and undone cases out of bulk filing.
- Add backward-compatible nullable database column; preserve external keys, queue records and transaction archives during rollback.
- Verification: 62 synthetic/mocked tests passed on pisortiq and browser filing/undo checks passed in an isolated Pi installation. No paid API calls, production image changes or motor operations.

# Changelog

## 2026-11-02 — AutoUndefined integration on SortIQ v1.6.0

- Added native automatic identification of unidentified crops through the OpenAI image Responses API and local/community-model second opinions.
- Added independent identification and automatic-filing switches plus write-only key replacement below Caliber & model in the standard Dataset tab.
- Fresh installations start with both switches off and no API key; keys are stored outside the repository per installation.
- Added reject/tray suggestions, dedicated review, bounded retries, duplicate-image reuse, source/profile checks, and sorting-state checks.
- Added original-image archives, hash-checked filing receipts, Undo, and interrupted-transaction recovery.
- Retained upstream v1.6.0 server, touchscreen and firmware features. No firmware modification is required by this extension.
- Excluded runtime state and keys from source deployment and export; added workflow and installation-privacy tests.
