"""Prepare FFmpeg from the pinned imageio-ffmpeg package's platform wheel.

This script makes no network requests. Install requirements-dev.txt first;
pip obtains the executable from the package's PyPI wheel.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "vendor").is_dir():
    sys.path.insert(0, str(ROOT / "vendor"))


def prepare_ffmpeg():
    try:
        import imageio_ffmpeg
    except ImportError as error:
        raise SystemExit("Install dependencies first: python -m pip install -r requirements-dev.txt") from error
    if imageio_ffmpeg.__version__ != "0.6.0":
        raise SystemExit("Use the pinned imageio-ffmpeg==0.6.0 package from requirements-dev.txt.")
    wheel_binary_dir = Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"
    candidates = [path for path in wheel_binary_dir.glob("ffmpeg-*") if path.is_file()]
    if len(candidates) != 1:
        raise SystemExit("The platform FFmpeg wheel is missing its executable. Reinstall imageio-ffmpeg.")
    source = candidates[0]
    options = dict(capture_output=True, text=True, check=True, timeout=15,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = subprocess.run([str(source), "-version"], **options).stdout.splitlines()[0]
    notice = subprocess.run([str(source), "-L"], **options)
    binary_dir = ROOT / "bin"
    binary_dir.mkdir(exist_ok=True)
    target = binary_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not target.is_file() or hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copy2(source, temporary)
        temporary.replace(target)
    (binary_dir / "FFmpeg-LICENSE.txt").write_text(notice.stdout + notice.stderr, encoding="utf-8")
    print(f"Prepared bin/{target.name}: {version}")
    return target


if __name__ == "__main__":
    prepare_ffmpeg()
