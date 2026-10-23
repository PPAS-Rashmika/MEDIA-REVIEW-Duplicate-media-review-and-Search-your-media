# Media Review 3.0

Run **Media Review.exe** to open the Windows desktop application. Keep the **models** folder beside the executable. Windows 10/11, 64-bit, and Microsoft Edge WebView2 Runtime are required. Do not move the executable into the source folder.

The app opens without scanning. Choose a primary folder, optionally add separate folders or drives under **Folders & comparison setup**, review settings, and click **Scan folder**. Choose **Protected originals** or **Reference library** for locations that the app must not change.

Version 3.0 adds multiple scan locations, keep rules, completed-file caching, explicit interrupted-scan continuation, rotated/flipped/cropped photo suggestions, similar-video sampling, duplicate-folder recognition, music-tag matching, offline catalogs, CSV reports, batch renaming, empty-folder cleanup, hard links, Windows Recycle Bin options and app-trash emptying. Video timelines align sampled positions; photo comparison supports linked zoom and pan.

**File tools** previews every proposed local change. Normal **Organize files** applies to the primary scan folder. Supported local operations have recovery or undo; the preview explains the selected action. Permanent app-trash emptying has no app restore. Hard links share content until undone.

Removed files in **Recently deleted** remain in a recovery folder and continue using disk space. Use the File tools trash controls when you are ready to finish cleaning. Windows Recycle Bin also retains space until emptied.

In **Cloud files**, connect Google Drive, OneDrive or Dropbox yourself, then explicitly scan. Confirmed cloud moves, type/year organization and restoration stay inside the connected account. Older read-only accounts need reconnecting before management is available. Google Drive requires your Desktop OAuth client ID and secret. Cloud trash is an in-account **.MediaReviewTrash** recovery folder; it does not free cloud storage. Real provider sign-in and moves depend on your account and were not exercised by the generated-fixture tests.

**Pause / Resume** continues the current worker while the app is open. After an interrupted run, **Resume unfinished scan** refreshes file discovery and comparison, reusing available completed-file cache records. It does not resume the exact old comparison position. Nothing resumes automatically.

**Reuse completed file analysis** is enabled by default and stores local hashes and available OCR/search/media analysis under your Windows profile. Disabling it does not erase older cache records. **Saved scans** stores separately named snapshots; **Open catalog** lets you review a disconnected drive without changing its files. Offline catalog files cannot authorize deleting the only available local copy.

Archive members remain read-only. Similar-image, video, audio, tag and metadata matches need review before removal. Video matching uses eight distributed samples and close-duration checks; it does not find arbitrary overlapping clips. Audio/video playback previews cover the first 120 seconds.

The **source** folder contains GitHub-ready application source without runtime executables, downloaded models, caches or test media. Read **source/README.md** for setup, feature details and limits, and **source/THIRD_PARTY_NOTICES.md** before redistributing the executable or model files.

