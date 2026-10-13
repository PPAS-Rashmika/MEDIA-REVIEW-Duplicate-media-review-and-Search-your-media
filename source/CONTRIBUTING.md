# Contributing

Thank you for helping improve Media Review.

## Set up

Follow the [README](README.md) to create a Python 3.12 virtual environment, install `requirements-dev.txt`, and prepare FFmpeg and the offline search models. Run `python launcher.py` for the desktop interface or `python app.py` for browser development. Model preparation uses the explicit `python tools/fetch_search_models.py` command; keep downloaded assets out of Git.

## Development expectations

- Keep the app local and preserve explicit user actions for scanning, search queries, playback, removal, and restoration.
- Use generated fixtures for tests. Do not commit personal media or scan reports.
- Keep OCR text and image embeddings private to the current in-memory scan. Do not add saved indexes, model auto-downloads, telemetry, or media uploads.
- Keep library search available for unique files as well as duplicates. Label visual results as suggestions and report unavailable recognition or model assets clearly.
- Preserve recoverable removal, path validation, and the check that another copy remains before removing a duplicate.
- Keep interface text in clear English. Do not add external services or account requirements without discussing the change.
- Include a relevant test when changing matching, file handling, API behavior, or the desktop lifecycle.

Before submitting a pull request, run:

```powershell
python -m unittest discover -s tests -v
```

For interface changes, also review the desktop app at its normal and minimum window sizes. Verify that choosing a folder or changing query text, modes, filters, or views does not start a scan or query. For packaging changes, build and launch the Windows executable with its companion models, verify actual local OCR and model inference using generated fixtures, and also check missing-model behavior. Preserve third-party licenses and pinned model provenance when changing dependencies or assets.

## Pull requests

Describe the user-visible problem, the resulting behavior, and how you verified the change. Keep changes focused so they are easy to review. Include screenshots for interface changes when useful, using generated sample media.

## Issues

Include the Windows version, Python or executable version, steps to reproduce, and the expected result. Share minimal generated samples when a format causes an issue. Remove personal paths and private filenames from diagnostic output before posting it.
