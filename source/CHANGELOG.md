# Changelog

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
