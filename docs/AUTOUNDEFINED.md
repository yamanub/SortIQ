# AutoUndefined

This extension runs inside the standard SortIQ server. Start and install SortIQ using the existing [Pi setup](PI_SETUP.md) or [trainer setup](TRAINER_SETUP.md). There is no second service or fixed machine address.

## Configure a new installation

1. Open the standard web app, then **Dataset → Caliber & model**.
2. Enter your own OpenAI API key and click the save button. The server validates access to `gpt-6.1-sol` using the model-list endpoint, without generating a response. API usage is billed to the owner of that key.
3. Turn on **Automatic identification** to process unidentified crops. Leave **Automatic filing** off if each result should be confirmed in review.
4. Optionally turn on **Automatic filing** to file eligible results into an existing class. Turn either switch off independently at any time.

Both switches default to off. Every fresh installation has a new installation identifier and no key. The password input always renders empty, including when a key is already configured. Replacing a key requires entering a new one; the saved value cannot be read through the web API. Opening the page or saving a key does not submit images for identification.

## Identification and review

The worker discovers rejects, explicitly unmatched capture frames, and unidentified tray cases across caliber/model profiles. It binds each case to its source profile and waits while the sorter is running. It submits image crops to the OpenAI Responses API with `store: false`, strict structured output, and model `gpt-6.1-sol`. Identical images can reuse a completed result. Transient failures have bounded retries; authentication or quota errors pause processing. Results appear in SortIQ's reject/tray review and the dedicated **AutoUndefined review** page.

The installed local embedding model supplies a second opinion. An optional compatible community model can be placed at `var/autoundefined/community/<cartridge>/shadow_embed.tflite` together with `shadow_gallery.npz`. The model and gallery fingerprints must match. Without a separate pair, the installed profile model is used. Installing community weights is a separate action; none are bundled here.

Automatic filing requires a completed API response, a valid existing class, no manual-review flag or model disagreement, unchanged source/profile, and a stopped sorter. Unreadable or disputed results stay for confirmation. The feature does not create new classes automatically. Proposed classes can be corrected during manual review. **File under class** is the filing destination when **Confirm & file** is pressed; no additional checkbox is required. Changing the selection saves a user classification across page reloads and server restarts, without filing the image. A saved user choice is held for explicit filing and is never silently replaced by automatic API filing.

**File All**, beside the queue controls, files identified cases in the displayed caliber/model profile. Saved user selections take priority over valid, completed API suggestions. Unidentified, disputed and interrupted cases, cases in other profiles, and undone cases are skipped. A user can explicitly select a class to resolve an ambiguous result. Each image uses the same original backup, durable receipt and Undo operation as single-image filing. Individual failures are reported while other eligible images can finish; repeating the action does not duplicate completed filings.

## State, keys and updates

Queue, event log, transaction receipts and original-image backups live in `var/autoundefined/`. Keep this directory when updating an existing installation. The Pi deployment script excludes it from code synchronization.

Keys live outside the source tree at `$XDG_CONFIG_HOME/sortiq-autoundefined/<installation-id>/openai-key`, defaulting to `~/.config`. Files use mode `0600`; rotation retains one previous key beside the current key. An administrator can explicitly override the location with `SORTIQ_AUTOUNDEFINED_KEY_FILE`. Do not set that override when creating a distributable installation image. Ordinary code updates retain the installation's own key. A fresh queue database creates a new identifier and does not inherit another installation's key, even at a reused directory path.

The repository, code synchronization and SortIQ dataset/config export exclude keys and AutoUndefined runtime state. Do not distribute a whole configured home directory or runtime directory as a fresh installation image.

## Undo and recovery

Every filing through AutoUndefined saves the original A/B files, their hashes, output hashes and a receipt before changing the dataset. Use **Undo** on the review page to restore the originals and remove the exact generated outputs. Undo checks for later manual edits and refuses conflicting changes rather than overwriting them. An undone case is blocked from automatic refiling. Interrupted transactions can be recovered through the review page; retain their archives until resolved.

To revert the application code, disable both switches, stop the server, and restore the preceding source version. Keep `var/autoundefined/` and the external key directory. Code rollback alone does not undo dataset changes; use the receipts and Undo before removing this extension. The extension does not make upstream dataset operations outside its filing path reversible.

## Verification

Run `python -m unittest discover -s tests -v` in an environment with Flask, NumPy and OpenCV. Tests use synthetic crops and mocked API responses; they do not call paid APIs or operate hardware. For a web preview without starting the automatic worker, set `SORTIQ_AUTOUNDEFINED_WORKER=0` when starting the server.

The 2026-11-02 integration passed 50 tests locally and on the test Raspberry Pi. The actual upstream Flask server was also loaded from an isolated copy: Dataset, settings script, review and configuration routes returned successfully, with both switches off and no saved key. No paid model requests or hardware movement were used for this verification.


The File All update passed 63 tests on the test Raspberry Pi, plus browser checks against isolated synthetic images: changing CBC to S&B immediately before Confirm & file, persisting a manual classification over reload, filing API and user classifications together, showing the actual filed class, and undoing all three images. Original bytes and output removal were verified. No paid API request or production image was used.

This update adds only a nullable `user_stamp` database column. The previous code can read the upgraded database, and code rollback retains queue records, archives and external keys. Saved user classifications are unavailable in the previous UI; resolve or file them before reverting. For each dataset change, use Undo before reverting code.
