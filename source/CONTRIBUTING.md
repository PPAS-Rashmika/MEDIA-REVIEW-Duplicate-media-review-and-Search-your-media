# Contributing

Thank you for helping improve Media Review.

## Set up

Follow the [README](README.md) to create a Python 3.12 virtual environment, install `requirements-dev.txt`, and prepare FFmpeg and the offline search models. Run `python launcher.py` for the desktop interface or `python app.py` for browser development. Model preparation uses the explicit `python tools/fetch_search_models.py` command; keep downloaded assets out of Git.

## Development expectations

- Keep local analysis local and preserve explicit user actions for scanning, search queries, playback, removal, restoration and optional cloud access.
- Use generated fixtures for tests. Do not commit personal media or scan reports.
- Keep OCR text and image embeddings local. The enabled scan cache deliberately persists completed file analysis, including content indexes, and explicit saved scans store separate snapshots; document this behavior and never imply that closing the app erases these files. Disabling caching for a run must prevent its cache reads/writes. Do not upload local indexes, add telemetry or download missing models automatically.
- Keep library search available for unique files as well as duplicates. Label visual results as suggestions and report unavailable recognition or model assets clearly.
- Preserve recoverable removal, path validation, and the check that another copy remains before removing a duplicate.
- Keep organization previews read-only. Moves and undo require explicit confirmation, cannot overwrite files, and must maintain validated recovery records and current library paths.
- Cover organization collisions, changed files, partial failures, linked paths, restart recovery and API authorization with generated fixtures.
- Keep interface text in clear English. Do not add external services or account requirements without discussing the change.
- Include a relevant test when changing matching, file handling, API behavior, or the desktop lifecycle.
- Scan cancellation must stop backend work, preserve only complete records, distinguish stopped scans from successful completion, and leave the next explicit scan usable. Cover discovery, chunked hashing, image processing and comparison with synchronized generated fixtures.
- Pause only the worker at safe checkpoints, never the state API; Stop and close must also release a paused worker. Validate saved sessions without executing serialized data. Keep archive members read-only and label audio, metadata and weak cloud-checksum matches as suggestions.
- Preserve separate root ownership and protected/reference roles in scans, saved results and every mutation path. Offline catalogs cannot authorize removing the only available copy. Interrupted continuation is an explicit discovery/comparison refresh that can reuse completed cache records, not restoration of an exact worker position.
- Bound transformed-image and sampled-video analysis and describe their limits. Similarity, tags and metadata are suggestions; hardlink and direct Recycle Bin tools require verified exact local copies.
- Show source/destination plans and obtain explicit confirmation before file tools or cloud moves. Keep durable undo for supported local operations and reversible cloud move history; never overwrite an existing file during execution or restore. Cloud trash remains an in-account recovery folder and does not free cloud storage.

Before submitting a pull request, run:

```powershell
python -m unittest discover -s tests -v
Get-ChildItem tests/test_*.cjs | ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw "UI checks failed" } }
```

For interface changes, also review the desktop app at its normal and minimum window sizes. Verify that choosing a folder or changing query text, modes, filters, or views does not start a scan or query. For packaging changes, build and launch the Windows executable with its companion models, verify actual local OCR and model inference using generated fixtures, and also check missing-model behavior. Preserve third-party licenses and pinned model provenance when changing dependencies or assets.

## Pull requests

Describe the user-visible problem, the resulting behavior, and how you verified the change. Keep changes focused so they are easy to review. Include screenshots for interface changes when useful, using generated sample media.

## Issues

Include the Windows version, Python or executable version, steps to reproduce, and the expected result. Share minimal generated samples when a format causes an issue. Remove personal paths and private filenames from diagnostic output before posting it.
