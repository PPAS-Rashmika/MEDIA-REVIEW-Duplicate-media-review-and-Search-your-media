"""Explicitly download the pinned, hash-verified offline CLIP search assets.

Run this setup helper yourself before launching the app. The app never calls it
and never downloads models, sends media, or contacts an inference service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from visual_search import MODEL_FILES, MODEL_NAME, MODEL_REVISION  # noqa: E402

CLIP_LICENSE_SHA256 = "987e63b32f6c89ff5160e429458a872ff048e6860b590a3912e938f9da8f14db"
CLIP_LICENSE_URL = "https://raw.githubusercontent.com/openai/CLIP/a1d071733d7111c9c014f024669f959182114e33f/LICENSE"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(destination: Path, relative: str, size: int, expected: str) -> None:
    target = destination / relative
    if target.is_symlink():
        raise ValueError(f"Refusing to replace a symbolic link: {relative}")
    if target.is_file() and target.stat().st_size == size and sha256_file(target) == expected:
        print(f"Verified {relative}", flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(destination.resolve()):
        raise ValueError("The model destination must stay inside the selected folder.")
    request = urllib.request.Request(
        f"https://huggingface.co/{MODEL_NAME}/resolve/{MODEL_REVISION}/{relative}",
        headers={"User-Agent": "MediaReview-OfflineModelSetup/1.3", "Accept-Encoding": "identity"},
    )
    for attempt in range(1, 4):
        temporary = None
        try:
            digest = hashlib.sha256()
            received = 0
            reported = 0
            print(f"Downloading {relative} ({size / 1_000_000:.1f} MB), attempt {attempt}", flush=True)
            with urllib.request.urlopen(request, timeout=60) as response:
                if not response.url.startswith("https://"):
                    raise ValueError("Model downloads require an HTTPS connection.")
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=target.parent, prefix=target.name + ".", suffix=".part", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if received > size:
                            raise ValueError(f"Unexpected size for {relative}.")
                        digest.update(chunk)
                        stream.write(chunk)
                        if received - reported >= 8 * 1024 * 1024:
                            reported = received
                            print(f"  {received / 1_000_000:.1f} / {size / 1_000_000:.1f} MB", flush=True)
            if received != size or digest.hexdigest() != expected:
                raise ValueError(f"Integrity verification failed for {relative}.")
            temporary.replace(target)
            temporary = None
            print(f"Verified {relative}", flush=True)
            return
        except (OSError, urllib.error.URLError, ValueError) as error:
            if attempt == 3:
                raise RuntimeError(f"Could not prepare {relative}: {error}") from error
            print(f"Retrying after a download error: {error}", flush=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def prepare_license(destination: Path) -> None:
    """Distribute the verified original MIT license with every prepared model set."""
    source = SOURCE / "licenses" / "models" / "CLIP-LICENSE.txt"
    if source.is_symlink() or not source.is_file() or sha256_file(source) != CLIP_LICENSE_SHA256:
        raise ValueError("The original CLIP license is missing or modified. Restore licenses/models/CLIP-LICENSE.txt from the complete source package.")
    target = destination / "CLIP-LICENSE.txt"
    if target.is_symlink():
        raise ValueError("Refusing to replace a linked model license.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=destination, prefix="CLIP-LICENSE.", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(source.read_bytes())
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    notice = SOURCE / "licenses" / "models" / "CLIP-NOTICE.md"
    if notice.is_file() and not notice.is_symlink():
        notice_target = destination / "CLIP-NOTICE.md"
        if notice_target.is_symlink():
            raise ValueError("Refusing to replace a linked model notice.")
        notice_target.write_bytes(notice.read_bytes())


def prepare_models(destination: Path | None = None) -> Path:
    # Release projects keep models beside the executable; a standalone GitHub
    # checkout keeps them directly inside its source folder.
    default_base = SOURCE.parent if SOURCE.name.lower() == "source" else SOURCE
    destination = (destination or default_base / "models" / "clip-vit-base-patch32").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    prepare_license(destination)
    for relative, (size, expected) in MODEL_FILES.items():
        download_file(destination, relative, size, expected)
    manifest = {
        "model": MODEL_NAME,
        "revision": MODEL_REVISION,
        "files": {name: {"size": size, "sha256": digest} for name, (size, digest) in MODEL_FILES.items()},
        "purpose": "Local image and English text embeddings for object and scene search.",
        "source": f"https://huggingface.co/{MODEL_NAME}/tree/{MODEL_REVISION}",
        "originalModel": "https://huggingface.co/openai/clip-vit-base-patch32",
        "license": {"file": "CLIP-LICENSE.txt", "spdx": "MIT", "sha256": CLIP_LICENSE_SHA256, "source": CLIP_LICENSE_URL},
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Offline visual search models are ready. Keep the models folder beside Media Review.exe.", flush=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, help="Override the model-specific asset folder.")
    options = parser.parse_args()
    try:
        prepare_models(options.destination)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
