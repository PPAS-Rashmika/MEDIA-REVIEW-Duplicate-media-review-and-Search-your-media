"""Build the standalone Windows desktop application."""
import argparse
from pathlib import Path
import os
import sys

APP_DIR = Path(__file__).resolve().parent
if (APP_DIR / "vendor").is_dir():
    sys.path.insert(0, str(APP_DIR / "vendor"))
os.environ["PYINSTALLER_CONFIG_DIR"] = str(APP_DIR / "build" / "cache")

import PyInstaller.__main__
from tools.collect_licenses import collect_licenses

parser = argparse.ArgumentParser(description="Build Media Review for Windows.")
parser.add_argument("--dist-dir", type=Path, default=APP_DIR / "dist",
                    help="Output directory (default: dist/)")
args = parser.parse_args()
if sys.platform != "win32":
    parser.error("Build the Windows executable on Windows with 64-bit Python 3.12.")
if not (APP_DIR / "bin" / "ffmpeg.exe").is_file():
    parser.error("FFmpeg is missing. Run: python tools/fetch_ffmpeg.py")
collect_licenses(APP_DIR / "licenses" / "dependencies")

PyInstaller.__main__.run([
    str(APP_DIR / "launcher.py"),
    "--name", "Media Review", "--onefile", "--windowed", "--noconfirm",
    "--distpath", str(args.dist_dir.resolve()),
    "--workpath", str(APP_DIR / "build"),
    "--specpath", str(APP_DIR / "build"),
    "--paths", str(APP_DIR), "--paths", str(APP_DIR / "vendor"),
    "--add-data", str(APP_DIR / "static") + ":static",
    "--add-data", str(APP_DIR / "app.ico") + ":.",
    "--add-binary", str(APP_DIR / "bin" / "ffmpeg.exe") + ":bin",
    "--add-data", str(APP_DIR / "bin" / "FFmpeg-LICENSE.txt") + ":bin",
    "--add-data", str(APP_DIR / "licenses") + ":licenses",
    "--add-data", str(APP_DIR / "LICENSE") + ":licenses",
    "--add-data", str(APP_DIR / "THIRD_PARTY_NOTICES.md") + ":licenses",
    "--icon", str(APP_DIR / "app.ico"),
    "--version-file", str(APP_DIR / "assets" / "windows-version-info.txt"),
    "--collect-all", "pillow_heif",
    "--collect-data", "webview",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.win32",
    "--hidden-import", "clr",
    "--collect-all", "winrt",
    "--collect-submodules", "winrt",
    "--collect-all", "onnxruntime",
    "--collect-all", "tokenizers",
    "--collect-all", "numpy",
    "--exclude-module", "webview.platforms.qt",
    "--exclude-module", "webview.platforms.cef",
    "--exclude-module", "PyQt5", "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2", "--exclude-module", "PySide6",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pandas", "--exclude-module", "scipy",
    "--exclude-module", "IPython", "--exclude-module", "pytest",
    "--exclude-module", "cv2", "--exclude-module", "torch",
])
