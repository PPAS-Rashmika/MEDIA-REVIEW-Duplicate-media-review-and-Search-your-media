"""Read-only media discovery and conservative duplicate comparison."""
from __future__ import annotations

import hashlib
import math
import mimetypes
import os
import stat
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

VENDOR = Path(__file__).resolve().parent / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

from PIL import Image, ImageChops, ImageOps, ImageStat
from image_filters import classify_image, extract_image_text
import visual_search

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif",
    ".avif", ".jpe", ".jfif", ".jxl", ".apng", ".ico", ".cur", ".pcx", ".tga", ".dds",
    ".ppm", ".pgm", ".pbm", ".pnm", ".psd", ".psb", ".svg", ".svgz", ".eps", ".ai",
    ".jp2", ".j2k", ".jpf", ".jpx", ".jpm", ".mj2", ".exr", ".hdr", ".pic", ".qoi",
    ".raw", ".arw", ".srf", ".sr2", ".crw", ".cr2", ".cr3", ".nef", ".nrw", ".dng",
    ".raf", ".orf", ".rw2", ".rwl", ".pef", ".ptx", ".srw", ".3fr", ".fff", ".iiq",
    ".k25", ".kdc", ".dcr", ".erf", ".mef", ".mos", ".mrw", ".x3f", ".bay", ".cap",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".qt", ".avi", ".wmv", ".asf", ".flv",
    ".f4v", ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".m2p", ".mts", ".m2ts", ".ts",
    ".mxf", ".vob", ".ogv", ".3gp", ".3g2", ".divx", ".rm", ".rmvb", ".dv", ".mjpeg",
    ".mjpg", ".h264", ".264", ".h265", ".265", ".hevc", ".y4m", ".nut", ".amv", ".bik",
}
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".wave", ".flac", ".aac", ".m4a", ".m4b", ".m4p", ".ogg", ".oga",
    ".opus", ".wma", ".aif", ".aiff", ".aifc", ".alac", ".ape", ".ac3", ".eac3", ".dts",
    ".amr", ".awb", ".au", ".snd", ".pcm", ".caf", ".mp2", ".mpa", ".wv", ".tta", ".tak",
    ".dsf", ".dff", ".mka", ".mid", ".midi", ".kar", ".ra", ".ram", ".spx", ".8svx",
}
EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
TRASH_NAME = ".photo-review-trash"
SUPPORT_DIRECTORIES = {TRASH_NAME, ".git", "__pycache__"}


def media_metadata(path: Path) -> tuple[str, str, str]:
    extension = path.suffix.lower()
    kind = ("image" if extension in IMAGE_EXTENSIONS else "video" if extension in VIDEO_EXTENSIONS
            else "audio" if extension in AUDIO_EXTENSIONS else "other")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind == "other":
        # Small header checks let common media with missing/unknown extensions
        # receive a preview without probing every file or guessing from its name.
        try:
            with path.open("rb") as source:
                header = source.read(64)
            for magic, detected_mime, detected_format in (
                (b"\x89PNG\r\n\x1a\n", "image/png", "PNG"),
                (b"\xff\xd8\xff", "image/jpeg", "JPEG"),
                (b"GIF87a", "image/gif", "GIF"), (b"GIF89a", "image/gif", "GIF"),
                (b"II*\x00", "image/tiff", "TIFF"), (b"MM\x00*", "image/tiff", "TIFF"),
                (b"8BPS", "image/vnd.adobe.photoshop", "PSD"),
                (b"fLaC", "audio/flac", "FLAC"), (b"ID3", "audio/mpeg", "MP3"),
            ):
                if header.startswith(magic):
                    return detected_mime.split("/")[0], detected_mime, detected_format
            if header[:4] == b"RIFF" and len(header) >= 12:
                riff = {b"WAVE": ("audio", "audio/wav", "WAV"),
                        b"WEBP": ("image", "image/webp", "WEBP"),
                        b"AVI ": ("video", "video/x-msvideo", "AVI")}
                if header[8:12] in riff:
                    return riff[header[8:12]]
            if header[4:8] == b"ftyp":
                brand = header[8:12]
                if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
                    return "image", "image/heif", "HEIF"
                if brand in {b"avif", b"avis"}:
                    return "image", "image/avif", "AVIF"
                if brand in {b"M4A ", b"M4B ", b"M4P "}:
                    return "audio", "audio/mp4", "M4A"
                return "video", "video/mp4", "MP4"
        except OSError:
            pass
    return kind, mime, extension.removeprefix(".").upper() or "FILE"


def is_reparse(path: Path) -> bool:
    """Do not follow Windows junctions, reparse points, or Unix symlinks."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def file_signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Photo:
    id: str
    file: Path
    name: str
    path: str
    size: int
    signature: tuple[int, int, int, int]
    sha256: str = ""
    width: int = 0
    height: int = 0
    format: str = ""
    previewAvailable: bool = False
    issue: str = ""
    pixel_hash: str = ""
    dhash: int = 0
    ahash: int = 0
    sample: bytes = b""
    mediaType: str = "image"
    mime: str = "application/octet-stream"
    duration: float | None = None
    previewMode: str = "fallback"
    imageCategories: list[str] = field(default_factory=list)
    imageCategoryReasons: list[str] = field(default_factory=list)
    imageTextStatus: str = "not-image"
    ocrText: str = ""
    ocrSearchStatus: str = "not-image"
    visualEmbedding: tuple[float, ...] | None = None
    visualStatus: str = "not-image"

    def public(self) -> dict:
        return {key: getattr(self, key) for key in (
            "id", "name", "path", "size", "width", "height", "format", "previewAvailable", "issue",
            "mediaType", "mime", "duration", "previewMode", "imageCategories", "imageCategoryReasons", "imageTextStatus",
            "ocrSearchStatus", "visualStatus"
        )}


@dataclass
class ScanResult:
    photos: dict[str, Photo] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def oriented_image(path: Path) -> Image.Image:
    """Detach the first image frame and apply its EXIF orientation."""
    with Image.open(path) as source:
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        oriented.load()
        return oriented.copy()


def rgb_image(image: Image.Image) -> Image.Image:
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def _bits(values) -> int:
    result = 0
    for value in values:
        result = (result << 1) | bool(value)
    return result


def _inspect(path: Path, root: Path) -> Photo:
    before = file_signature(path)
    relative = str(path.relative_to(root))
    photo_id = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:24]
    photo = Photo(photo_id, path, path.name, relative, before[2], before)
    photo.mediaType, photo.mime, photo.format = media_metadata(path)
    photo.sha256 = hash_file(path)
    if photo.mediaType != "image":
        if photo.mediaType in {"video", "audio"}:
            photo.previewMode = photo.mediaType
            photo.previewAvailable = True
        else:
            photo.issue = "No visual preview for this file type. Identical file detection still works."
        if file_signature(path) != before:
            raise ValueError("File changed while being scanned. Scan again before reviewing it.")
        return photo
    try:
        with Image.open(path) as source:
            photo.format = source.format or path.suffix[1:].upper()
            animated = getattr(source, "n_frames", 1) > 1
            software = str(source.info.get("Software", ""))
            try:
                software = str(source.getexif().get(305, "") or software)
            except (OSError, ValueError, SyntaxError):
                pass
        image = oriented_image(path)
        photo.width, photo.height = image.size
        photo.previewAvailable = True
        photo.previewMode = "image"
        photo.imageCategories, photo.imageCategoryReasons, photo.imageTextStatus = classify_image(path, image, software)
        photo.ocrText, photo.ocrSearchStatus = extract_image_text(image)
        try:
            photo.visualEmbedding = visual_search.embed_image(image)
            photo.visualStatus = "indexed" if photo.visualEmbedding is not None else (
                "error" if visual_search.capabilities().get("available") else "unavailable")
        except ImportError:
            photo.visualStatus = "unavailable"
        except Exception:
            # Object search must not prevent exact duplicate detection.
            photo.visualStatus = "error"
        # Animated files are only exact-byte candidates: comparing their first
        # frame would wrongly call different animations identical.
        if not animated:
            rgba = image.convert("RGBA")
            digest = hashlib.sha256()
            digest.update(f"{rgba.width}x{rgba.height}:RGBA:".encode("ascii"))
            digest.update(rgba.tobytes())
            photo.pixel_hash = digest.hexdigest()
            rgb = rgb_image(image)
            gray = rgb.convert("L")
            dh = list(gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes())
            photo.dhash = _bits(dh[y * 9 + x] > dh[y * 9 + x + 1] for y in range(8) for x in range(8))
            ah = list(gray.resize((8, 8), Image.Resampling.LANCZOS).tobytes())
            average = sum(ah) / len(ah)
            photo.ahash = _bits(value > average for value in ah)
            photo.sample = rgb.resize((48, 48), Image.Resampling.LANCZOS).tobytes()
        else:
            photo.issue = "Animated image: preview shows first frame; only identical files are compared."
    except Exception as error:
        photo.imageCategories, photo.imageCategoryReasons, photo.imageTextStatus = classify_image(path)
        photo.ocrText, photo.ocrSearchStatus = "", "error"
        photo.visualEmbedding, photo.visualStatus = None, "error"
        photo.format = photo.format or path.suffix[1:].upper()
        if path.suffix.lower() in {".heic", ".heif"} and not HEIF_AVAILABLE:
            photo.issue = "HEIC preview needs the optional pillow-heif decoder. Identical file detection still works."
        else:
            photo.issue = f"Cannot decode image: {error}"
    if file_signature(path) != before:
        raise ValueError("File changed while being scanned. Scan again before reviewing it.")
    return photo


def discover(root: Path, recursive: bool, exclude_paths=(), include_all_files: bool = True) -> tuple[list[Path], list[dict]]:
    excluded = {os.path.normcase(str(Path(item).resolve())) for item in exclude_paths}
    paths, errors = [], []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.lower())
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.name.lower() in SUPPORT_DIRECTORIES or os.path.normcase(str(path.absolute())) in excluded or is_reparse(path):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False) and (include_all_files or path.suffix.lower() in EXTENSIONS):
                        paths.append(path)
                except OSError as error:
                    errors.append({"name": str(path.relative_to(root)), "message": str(error)})
        except OSError as error:
            errors.append({"name": str(directory), "message": str(error)})
    return sorted(paths, key=lambda path: str(path).lower()), errors


def _group(kind: str, photos: list[Photo], score: float = 100) -> dict:
    photos = sorted(photos, key=lambda photo: (-(photo.width * photo.height), -photo.size, photo.path.lower()))
    ids = [photo.id for photo in photos]
    identity = kind + ":" + ":".join(sorted(ids))
    return {"id": hashlib.sha256(identity.encode("ascii")).hexdigest()[:24], "kind": kind, "score": round(score, 1), "photos": ids}


def compare_photos(photos: dict[str, Photo], threshold: float = 94, progress=None) -> list[dict]:
    exact, pixels = defaultdict(list), defaultdict(list)
    for photo in photos.values():
        if photo.sha256:
            exact[photo.sha256].append(photo)
        if photo.pixel_hash:
            pixels[photo.pixel_hash].append(photo)
    groups = [_group("exact", bucket) for bucket in exact.values() if len(bucket) > 1]
    groups.extend(_group("pixels", bucket) for bucket in pixels.values()
                  if len(bucket) > 1 and len({photo.sha256 for photo in bucket}) > 1)
    # Compare one representative of each pixel-identical set. Similarity groups
    # are pairs: A~B and B~C do not imply that A~C is a safe duplicate.
    representatives = [max(bucket, key=lambda photo: (photo.width * photo.height, photo.size)) for bucket in pixels.values()]
    count = len(representatives)
    total = count * (count - 1) // 2
    done = 0
    hash_limit = max(2, min(10, math.ceil((100 - threshold) / 1.5)))
    for index, left in enumerate(representatives):
        for right in representatives[index + 1:]:
            done += 1
            if progress and (done % 1000 == 0 or done == total):
                progress(done, total, "compare")
            aspect_a, aspect_b = left.width / left.height, right.width / right.height
            if abs(aspect_a - aspect_b) / max(aspect_a, aspect_b) > 0.03:
                continue
            if (left.dhash ^ right.dhash).bit_count() > hash_limit or (left.ahash ^ right.ahash).bit_count() > hash_limit:
                continue
            first = Image.frombytes("RGB", (48, 48), left.sample)
            second = Image.frombytes("RGB", (48, 48), right.sample)
            stats = ImageStat.Stat(ImageChops.difference(first, second))
            rms = math.sqrt(sum(value * value for value in stats.rms) / 3)
            score = 100 * (1 - rms / 255)
            # RGB distance checks keep similarly-shaped but different photos out.
            if score >= threshold and max(stats.mean) <= max(7, (100 - threshold) * 1.6):
                groups.append(_group("similar", [left, right], score))
    return sorted(groups, key=lambda group: ({"exact": 0, "pixels": 1, "similar": 2}[group["kind"]], -group["score"], group["id"]))


def calculate_stats(photos: dict[str, Photo], groups: list[dict]) -> dict:
    equivalences = defaultdict(list)
    for photo in photos.values():
        equivalences[photo.pixel_hash or photo.sha256 or photo.id].append(photo)
    recoverable = sum(sum(photo.size for photo in bucket) - max(photo.size for photo in bucket)
                      for bucket in equivalences.values() if len(bucket) > 1)
    return {"total": len(photos), "exactGroups": sum(group["kind"] != "similar" for group in groups),
            "similarGroups": sum(group["kind"] == "similar" for group in groups), "duplicateBytes": recoverable}


def scan_folder(root, recursive: bool = True, threshold: float = 94,
                on_progress: Callable | None = None, exclude_paths=(), include_all_files: bool = True) -> ScanResult:
    root = Path(os.path.abspath(os.path.expanduser(str(root))))
    if not root.is_dir():
        raise ValueError("The media folder does not exist or is not a folder.")
    if is_reparse(root):
        raise ValueError("Choose a normal folder rather than a linked folder or junction.")
    threshold = float(threshold)
    if not 85 <= threshold <= 99:
        raise ValueError("Similarity threshold must be between 85 and 99.")
    result = ScanResult()
    if on_progress:
        on_progress(0, 0, "discover")
    if not isinstance(include_all_files, bool):
        raise ValueError("Include all file types must be true or false.")
    paths, result.errors = discover(root, recursive, exclude_paths, include_all_files)
    for index, path in enumerate(paths):
        try:
            photo = _inspect(path, root)
            result.photos[photo.id] = photo
            if photo.issue and photo.mediaType == "image":
                result.errors.append({"name": photo.path, "message": photo.issue})
        except Exception as error:
            result.errors.append({"name": str(path.relative_to(root)), "message": str(error)})
        if on_progress:
            on_progress(index + 1, len(paths), "read")
    if on_progress:
        on_progress(0, len(result.photos) * max(0, len(result.photos) - 1) // 2, "compare")
    result.groups = compare_photos(result.photos, threshold, on_progress)
    result.stats = calculate_stats(result.photos, result.groups)
    if on_progress:
        on_progress(len(paths), len(paths), "complete")
    return result
