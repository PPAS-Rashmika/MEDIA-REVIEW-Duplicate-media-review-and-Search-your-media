"""Collect installed dependency license notices into build resources."""
from __future__ import annotations

import importlib.metadata
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "vendor").is_dir():
    sys.path.insert(0, str(ROOT / "vendor"))

PACKAGES = (
    "Pillow", "pillow-heif", "pywebview", "pythonnet", "clr-loader",
    "cffi", "pycparser", "bottle", "typing-extensions", "proxy-tools",
    "imageio-ffmpeg", "PyInstaller", "altgraph", "pyinstaller-hooks-contrib",
    "winrt-runtime", "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections",
    "winrt-Windows.Globalization", "winrt-Windows.Graphics.Imaging", "winrt-Windows.Media.Ocr",
    "winrt-Windows.Storage.Streams",
    "numpy", "onnxruntime", "tokenizers", "flatbuffers", "protobuf",
    "huggingface-hub", "filelock", "fsspec", "hf-xet", "httpx2", "httpcore2",
    "click", "PyYAML", "tqdm", "anyio", "idna", "truststore", "h11", "colorama",
)


def collect_licenses(destination=None):
    destination = Path(destination or ROOT / "licenses" / "dependencies")
    destination.mkdir(parents=True, exist_ok=True)
    collected = []
    for package in PACKAGES:
        try:
            distribution = importlib.metadata.distribution(package)
        except importlib.metadata.PackageNotFoundError:
            continue
        for item in distribution.files or []:
            name = str(item).replace("\\", "/")
            if ".dist-info/" not in name or not any(word in item.name.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            relative = name.split(".dist-info/", 1)[1]
            target = destination / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            collected.append(f"{package}/{relative}")
    for name in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / name
        if source.is_file():
            target = destination / "Python" / "LICENSE.txt"
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            collected.append("Python/LICENSE.txt")
            break
    print(f"Collected {len(collected)} dependency license notices.")
    return collected


if __name__ == "__main__":
    collect_licenses()
