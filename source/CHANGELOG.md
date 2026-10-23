# Changelog

## 3.0.0

- Scan up to 32 separate folders or drives in one workspace, with protected originals, reference libraries and cross-folder comparison modes.
- Reuse completed local file analysis in a persistent, settings-aware cache; explicitly continue an interrupted scan after reopening the app by refreshing discovery and comparison.
- Add optional rotated/flipped image comparison and bounded crop matching.
- Add optional similar-video suggestions using eight distributed frame samples and close-duration checks, plus synchronized sampled timelines and linked image zoom/pan.
- Recognize complete duplicate folder structures for read-only review, and suggest music matches using selected audio tags.
- Open saved scans as read-only offline drive catalogs and compare them with current local folders without authorizing removal of the only available copy.
- Add File tools with configurable keep rules, CSV duplicate reports, planned batch renaming and empty-folder cleanup.
- Add verified same-filesystem hardlink replacement with undo that creates independent copies again.
- Add Windows Recycle Bin removal for exact copies, app-trash transfer to the Recycle Bin, and explicitly confirmed permanent app-trash emptying.
- Add optional confirmed Google Drive, OneDrive and Dropbox in-account moves, type/year organization, recoverable cloud trash and restoration history. Older read-only connections require reconnection for file management.
- Preserve explicit scan controls, content search, saved results, read-only archives, local organization, recovery and the single-window English interface.

## 2.0.0

- Pause and resume local scans without restarting the worker; Stop and closing also release paused workers.
- Explicitly save and reopen validated local scan sessions with their settings and search indexes.
- Add advanced extension, folder-pattern, file-size, modified-date and image-dimension filters.
- Add offline acoustic fingerprint suggestions across supported audio encodings.
- Compare, search and preview bounded read-only archive members.
- Suggest photo matches using capture time, camera and optional GPS distance.
- Add a separate optional Google Drive, OneDrive and Dropbox checksum workspace with explicit browser authorization.
- Integrate new settings, suggestion filters, saved scans and cloud navigation into the desktop interface.


## 1.5.0

- Added Stop scan with a Stopping state and cooperative cancellation during discovery, file hashing, image processing and similarity comparison.
- Keep completed file records and available matches, with a clear incomplete-scan notice.
- Allow a fresh manual scan after cancellation without carrying over the previous stop request.
- Preserve existing local search, duplicate review, file organization and undo features.

## 1.4.0

- Organize every scanned file into extension folders or year folders, including files without duplicates.
- Choose image date taken with modified-date fallback, or modified dates for all files.
- Review folder totals and file destinations before confirming a move.
- Avoid filename overwrites with numbered suffixes, and skip files already organized.
- Show background progress and keep duplicate review and content search aligned with moved paths.
- Store local organization history for undo, including recovery after reopening the app.
- Label scan notices and issues without calling every informational notice a read failure.

## 1.3.0

- Search library across every scanned file, including photos without duplicates.
- Local OCR text search for words, names, and numbers inside decoded images, including short text.
- Offline CLIP object and scene search using English descriptions, with approximate visual suggestions.
- All, Text in images, Objects & scenes, and Filenames search modes, with file type and image category filters.
- Search result previews, explicit paging, matching text snippets, and Show in folder.
- OCR text and image embeddings kept only in memory for the current scan, cleared on a new scan or app close.
- Verified companion model files and an explicit preparation helper; no automatic model downloads.
- Separate release folder containing the executable, models, and GitHub-ready source files.

## 1.2.0

- Combined Screenshot, Downloaded, Text images, and Other images filters.
- Local Windows OCR detects text inside decoded images; recognized words are not stored.
- Suggested screenshot/download labels use file, folder, and source metadata.
- Category-filtered bulk removal only targets matching copies and protects other files.
- Separate release folder with the executable and GitHub source files.

## 1.1.0

- Bulk removal from the visible All, Exact, or Similar results, including file type and filename filters.
- Confirmation with the exact removal list, total size, and protected keeper files.
- Shared keepers protected across overlapping groups, with each removed copy moved once.
- Changed-file checks before bulk moves and recovery history for successful moves, including partial failures.

## 1.0.0

- Single-window Windows desktop interface with English labels.
- Manual folder selection and scanning, with optional subfolder inclusion.
- Exact duplicate detection for all file types.
- Decoded image matching and adjustable image similarity suggestions.
- Side-by-side image, video, and audio review with previews prepared on demand.
- Recoverable removal with confirmation, Undo, and restoration history.
- Windows packaging, generated-fixture tests, and GitHub project documentation.
