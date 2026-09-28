# Card working copies and NAS verification

## Implementation notes

1. Reviewed card detection, review, playback, report export, copy planning and application shutdown. The earlier performance work remains in place: bounded metadata readers, asynchronous player preparation, table/search caches, and native waveform reductions with a NumPy fallback.
2. Added a disposable working directory under the local application cache (`card-work/sfm-card-*`). **Working Copy Folder…** lets the user choose another disk. The source is registered as protected before creating directories or copying anything.
3. Enumerate recordings and accompanying files, including false takes. Skip recorder/system folders, hidden directories, Apple resource files and recorder folder markers. Unexpected symbolic links, unreadable entries and changed source manifests fail preparation instead of exposing a partial copy.
4. Check available space, copy once from the card, check each local copy’s size, and preserve file dates. Progress and cancellation stay available. Review begins only after preparation succeeds; there is no fallback to editing or playing from the card.
5. Use working paths for the review model, player, reports and offload source. Keep the physical card path for ejection and history. Review edits remain pending and are applied to NAS copies after offload, as before.
6. Add application write guards to metadata, rename/move, split/combine, report, index, export, automation and deletion paths. Detect symlink aliases and reject destinations escaping the chosen NAS directory. Detected removable media and folders explicitly opened as cards remain protected for the app session.
7. Check all eligible card files against the NAS, regardless of tree ticks or the false-takes copying option. The positive notice requires matching complete file bytes, including WAV metadata and accompanying files. Size/date checks and audio fingerprints used by the existing offload planner cannot trigger this notice.
8. Use the intended destination and catalog candidates for renamed recordings. Sidecars can follow verified recordings to another directory. Four bounded background readers overlap NAS latency without recursively scanning the NAS. Successful comparisons are cached only for this card session and re-used after checking both files' device, inode, size, modification time and change time. On Windows the full comparison is repeated because creation time can be reported as change time.
9. Cancel stale work on card switches and destination changes. Release player readers before retiring the workspace; remove it only after its background jobs finish. Application shutdown cancels copying, waits for workers and removes working copies.

## What the notice means

“Already stored on NAS” means every eligible source file was verified against a file in the configured library (or selected destination when no library is configured). This is a result of the completed check, not continuous monitoring of NAS changes. Empty cards, missing files, failed reads and incomplete checks cannot produce a positive notice.

The check is deliberately conservative. NAS copies with edited metadata do not count as complete byte matches, even if the audio is identical. Unindexed files stored elsewhere, renamed sidecars and sidecars moved separately from recordings may not be found. These cases show “not confirmed fully stored”; the app does not infer that they are safe to erase.

## Storage and protection limits

- The selected local disk needs space for all eligible card files plus 64 MiB reserve. NAS verification also reads full candidate files once, so the first check can take time.
- Working copies are removed on card switch, cancellation, ejection and normal shutdown. A forced process kill or power failure can leave an `sfm-card-*` directory in the working folder; no card data is removed during cleanup.
- These guards prevent application writes. They do not change operating-system mount permissions or stop other applications. A filesystem may update access times during reads.
- Pending edits are still temporary until offloaded. A temporary card copy is not a permanent backup.

## Validation

Synthetic temporary cards and NAS directories only; no real card or library writes.

Automated coverage includes verified copying and dates, original card bytes remaining unchanged, pending edits reaching only NAS copies, cancellation, copy failure, insufficient space, symbolic links and destination traversal, missing reports and false takes, same-size/same-mtime corruption, session cache reuse, renamed recording candidates, empty/cancelled verification, UI notification, switching during preparation and cleanup.

Final checks: 212 unit tests passed. An offscreen MainWindow smoke test prepared a synthetic card, selected a recording through the asynchronous player, closed the application, and confirmed working-copy cleanup and unchanged source bytes. Windows and macOS hardware were not available for this run.

## Faster temporary copying

Removed the temporary-copy read-back pass and per-file forced disk flush after
feedback that preparation took too long. Temporary copies now make one streaming
pass without checksum calculation, check destination sizes, and compare the source
manifest before and after copying. This saves a full local reread and checksum work;
actual time saved depends on the card reader and working disk. It does not provide
the former byte-for-byte integrity check of the temporary copy.

Permanent NAS copies retain the existing Verify every copy setting and forced disk
flush. The already-stored notice still compares full files against the working copy.
Card write guards, cancellation and incomplete-copy cleanup remain active.

## Working-copy progress popup

Card loading now opens a progress window immediately. It shows activity while
listing files, then percentage, current filename, completed file count, bytes,
transfer speed and estimated time remaining while copying. Recording-detail loading
uses an activity indicator until review is ready. Cancel, Escape and closing the
popup cancel preparation; completion, errors, card switches and shutdown dismiss it.

The preparation popup uses an amber background and a prominent “Please wait” heading. Its initial size is 540 × 320 logical pixels, with a 520 × 300 minimum, spaced rows and wrapping labels so longer text can expand the layout.

## Storage wording

The interface uses “Storage” for copy actions, folders, reports and statuses.
Backup-check notices name the configured library folder (or selected destination
when no library is configured), so users can identify which storage was checked.

## Reviewing comparison differences

Storage comparison reads from the local working copy, with four background readers.
A review button lists new/not-found, different and unverified files, including
accompanying files. Select files to copy or ignore for this card session. Explicit
copying preserves existing destinations using a “card copy” filename when needed;
it does not move or delete card originals. Ignored files are excluded from normal
copy selection but still count as unverified in the whole-card backup result.
Choices reset when the card session closes. Changed metadata counts as different.
