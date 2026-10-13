# Media Review 1.3

Open **Media Review.exe** in this folder. Choose a media folder and click **Scan folder**.

Select **Search library** to find files, including photos without duplicates. Enter a phrase and click **Search**:

- **Text in images** finds recognized names, words and numbers inside photos.
- **Objects & scenes** finds visual suggestions such as `car`, `dog` or `beach`. English descriptions work best.
- **Filenames** searches names and paths across all scanned file types.
- **All** combines these searches.

Use the search file type and image category filters, then click Search again. Preview a result or select **Show in folder** to locate the original file.

Recognition depends on image quality and installed Windows OCR languages. Object matches are suggestions. Image content search covers decoded photos and the first frame of animated images; video, audio and other files can be searched by their filenames.

Scan and search processing stay on this computer. OCR text and image embeddings are held in memory for the current scan and cleared on a new scan or when the app closes. The app starts no scan or model download automatically.

Duplicate review, Screenshot/Downloaded/Text/Other filters and recoverable bulk removal remain available. Compare previews and confirm the removal list. Copies moved into the app's recovery trash can be restored from **Recently deleted**.

Keep these items together:

- **Media Review.exe** — the Windows application, including its Python runtime.
- **models/** — verified offline object-search assets and model license notices.
- **source/** — editable project, tests, documentation and licenses.
- **media-review-source.zip** — source prepared for GitHub; excludes model weights and personal media.

Windows 10 or 11 (64-bit) and Microsoft Edge WebView2 Runtime are required. Source setup and build instructions are in [source/README.md](source/README.md).
