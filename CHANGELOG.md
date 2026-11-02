# Changelog

## 2026-11-02 — AutoUndefined integration on SortIQ v1.6.0

- Added native automatic identification of unidentified crops through the OpenAI image Responses API and local/community-model second opinions.
- Added independent identification and automatic-filing switches plus write-only key replacement below Caliber & model in the standard Dataset tab.
- Fresh installations start with both switches off and no API key; keys are stored outside the repository per installation.
- Added reject/tray suggestions, dedicated review, bounded retries, duplicate-image reuse, source/profile checks, and sorting-state checks.
- Added original-image archives, hash-checked filing receipts, Undo, and interrupted-transaction recovery.
- Retained upstream v1.6.0 server, touchscreen and firmware features. No firmware modification is required by this extension.
- Excluded runtime state and keys from source deployment and export; added workflow and installation-privacy tests.
