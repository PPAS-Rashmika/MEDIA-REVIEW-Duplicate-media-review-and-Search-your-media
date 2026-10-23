# Third-party components

The MIT license in this repository applies to the original application code. It does not replace the licenses of bundled dependencies.

| Component | Purpose | License information |
| --- | --- | --- |
| CPython | Runtime in the packaged executable | [Python license](https://docs.python.org/3/license.html) |
| Pillow | Image decoding and processing | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| pillow-heif / libheif | HEIC/HEIF decoding | [Bundled component licenses](https://github.com/bigcat88/pillow_heif/blob/master/LICENSES_bundled.txt) |
| pywebview | Native desktop webview window | [BSD 3-Clause](https://github.com/r0x0r/pywebview/blob/master/LICENSE) |
| pythonnet | Windows .NET integration | [MIT](https://github.com/pythonnet/pythonnet/blob/master/LICENSE) |
| clr-loader | .NET runtime loading | [MIT](https://github.com/pythonnet/clr-loader/blob/main/LICENSE) |
| PyWinRT | Local Windows OCR and bitmap integration | [MIT](https://github.com/pywinrt/pywinrt/blob/main/LICENSE) |
| ONNX Runtime 1.31.0 | Local CPU inference for image and query embeddings | MIT; full installed-wheel license and third-party notices in [licenses/dependencies/onnxruntime](licenses/dependencies/onnxruntime/). |
| NumPy 2.5.4 | Image preprocessing and vector operations | BSD 3-Clause and bundled component notices in [licenses/dependencies/numpy](licenses/dependencies/numpy/). |
| Hugging Face Tokenizers 0.23.3 | Local CLIP text tokenization | [Apache 2.0, pinned release source](https://github.com/huggingface/tokenizers/blob/301277072263fb3830eb66eec7e47adadf76f1dc/LICENSE); full text in [licenses/dependencies/tokenizers](licenses/dependencies/tokenizers/). |
| FlatBuffers 25.12.19 | ONNX Runtime dependency | [Apache 2.0, pinned release source](https://github.com/google/flatbuffers/blob/7e163021e59cca4f8e1e35a7c828b5c6b7915953/LICENSE); full text in [licenses/dependencies/flatbuffers](licenses/dependencies/flatbuffers/). |
| Protobuf | ONNX Runtime dependency | BSD 3-Clause; installed distribution notice in [licenses/dependencies/protobuf](licenses/dependencies/protobuf/). |
| OpenAI CLIP / Xenova ONNX export | Offline object and scene search model | MIT; full original license and model provenance in [licenses/models](licenses/models/). |
| PyInstaller | Executable packaging | [GPL with bootloader exception](https://pyinstaller.org/en/stable/license.html) |
| FFmpeg | Video/audio previews, acoustic fingerprints, sampled video matching and audio tag extraction | The supplied Windows build uses GPLv3 or later. |
| rclone 1.75.2 | Optional Google Drive, OneDrive and Dropbox checksum review and confirmed reversible file moves | MIT; see [bin/Rclone-LICENSE.txt](bin/Rclone-LICENSE.txt) and [official source](https://github.com/rclone/rclone/tree/v1.75.2). |
| 7-Zip 26.04 | Read-only RAR and 7z member decoding | LGPL and BSD components, plus the unRAR restriction; see [bin/7zip/License.txt](bin/7zip/License.txt) and [7-Zip source and license](https://www.7-zip.org/license.txt). |
| Microsoft Edge WebView2 | Installed system webview runtime | [Microsoft WebView2 terms](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |

## HEIF decoder binaries

The `pillow-heif` wrapper has a BSD license. Its Windows binary wheel also includes native components with separate terms: libheif and libde265 use LGPLv3, while the bundled x265 component uses GPLv2. The wheel's `LICENSES_bundled.txt` explicitly describes the binary wheels as GPLv2 licensed. Preserve that bundled-components notice and the corresponding license texts when distributing the decoder. The files in `licenses/` record the notices from the installed build dependencies.

## FFmpeg

The Windows FFmpeg binary is prepared from the `imageio-ffmpeg` distribution. The current supplied decoder identifies itself as FFmpeg 7.1 from Gyan's builds. The component notice is in `bin/FFmpeg-LICENSE.txt`; upstream source is available from the [FFmpeg n7.1 tree](https://github.com/FFmpeg/FFmpeg/tree/n7.1) and the build provider is [Gyan](https://www.gyan.dev/ffmpeg/builds/). Consult the [FFmpeg license and distribution guidance](https://ffmpeg.org/legal.html) when distributing executables containing this binary.

## Distribution

Text-image detection and text search use the installed Windows OCR engine through PyWinRT. Windows OCR language resources are system components and are not included in the executable. The app uses available recognition languages, reports unavailable detection, and keeps image processing local.

The object and scene search model is OpenAI CLIP ViT-B/32, using Xenova's quantized ONNX export at revision `d15189d7028b43f1d3e65039190477f6af591c2a`. The original [CLIP MIT license at its pinned source revision](https://github.com/openai/CLIP/blob/a1d071733d7111c9c014f024669f959182114e33f/LICENSE) is preserved in `licenses/models/CLIP-LICENSE.txt`. [CLIP-NOTICE.md](licenses/models/CLIP-NOTICE.md) records the original checkpoint, converted assets, and provenance. The preparation helper copies those notices beside the companion model files and records asset hashes in their manifest. Preserve both notices when redistributing the models.

The ONNX Runtime wheel provides its own `ThirdPartyNotices.txt`; it is preserved in full alongside its MIT license. NumPy's installed-wheel license includes terms for its native components, with additional shipped license texts retained in its dependency folder. Tokenizers and FlatBuffers full license texts were retrieved from their fixed official release commits because the installed wheels omit them from distribution metadata; adjacent `SOURCE.txt` files record source URLs and SHA-256 values.

Supporting dependencies retain their own notices under `licenses/dependencies`, including the Hugging Face Hub package installed with Tokenizers, packaging and desktop dependencies, and their transitive libraries. These license copies do not imply that hosted inference or Hub downloads are used at runtime; model loading uses only the prepared local assets.

A source checkout does not include downloaded decoder binaries, model weights, or vendor dependencies. Packaged distributions should include the relevant component license texts and source-access information. Dependency versions and their notices should be checked again when updating the build.
