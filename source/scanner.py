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
from scan_options import normalize_options, excluded as is_excluded, accepts_file, accepts_dimensions
from archive_scan import is_archive, list_members, materialize_member, ArchiveCancelled
from metadata_match import read_metadata, metadata_match
import audio_fingerprint
import video_match
import image_transforms

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
SUPPORT_DIRECTORIES = {TRASH_NAME, ".media-review-organize", ".git", "__pycache__"}


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


class ScanCancelled(Exception):
    """Internal cooperative cancellation, never a file read failure."""


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise ScanCancelled()


def hash_file(path: Path, should_stop: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    _check_stop(should_stop)
    with path.open("rb") as source:
        while True:
            _check_stop(should_stop)
            chunk = source.read(1024 * 1024)
            _check_stop(should_stop)
            if not chunk:
                break
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
    sourceKind: str = "local"
    archiveContainer: str = ""
    archiveMember: str = ""
    metadata: dict = field(default_factory=dict)
    audioFingerprint: tuple[int, ...] = ()
    audioDuration: float = 0
    audioStatus: str = "not-indexed"
    sourceRoot: str = ""
    protected: bool = False
    reference: bool = False
    offline: bool = False
    transformSample: bytes = b""
    videoFrames: list[dict] = field(default_factory=list)
    videoStatus: str = "not-indexed"
    audioTags: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {key: getattr(self, key) for key in (
            "id", "name", "path", "size", "width", "height", "format", "previewAvailable", "issue",
            "mediaType", "mime", "duration", "previewMode", "imageCategories", "imageCategoryReasons", "imageTextStatus",
            "ocrSearchStatus", "visualStatus", "sourceKind", "archiveContainer", "archiveMember",
            "metadata", "audioStatus", "sourceRoot", "protected", "reference", "offline",
            "videoStatus", "audioTags"
        )} | {'videoTimeline': [{'time': frame['time'], 'thumbnail': frame['thumbnail']}
                               for frame in self.videoFrames]}


@dataclass
class ScanResult:
    photos: dict[str, Photo] = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    stopped: bool = False
    folderGroups: list[dict] = field(default_factory=list)


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


def _inspect(path: Path, root: Path, should_stop: Callable[[], bool] | None = None) -> Photo:
    _check_stop(should_stop)
    before = file_signature(path)
    relative = str(path.relative_to(root))
    photo_id = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:24]
    photo = Photo(photo_id, path, path.name, relative, before[2], before)
    photo.mediaType, photo.mime, photo.format = media_metadata(path)
    _check_stop(should_stop)
    photo.sha256 = hash_file(path, should_stop) if should_stop is not None else hash_file(path)
    _check_stop(should_stop)
    if photo.mediaType != "image":
        if photo.mediaType in {"video", "audio"}:
            photo.previewMode = photo.mediaType
            photo.previewAvailable = True
        else:
            photo.issue = "No visual preview for this file type. Identical file detection still works."
        if file_signature(path) != before:
            raise ValueError("File changed while being scanned. Scan again before reviewing it.")
        _check_stop(should_stop)
        return photo
    try:
        _check_stop(should_stop)
        with Image.open(path) as source:
            photo.format = source.format or path.suffix[1:].upper()
            animated = getattr(source, "n_frames", 1) > 1
            software = str(source.info.get("Software", ""))
            try:
                software = str(source.getexif().get(305, "") or software)
            except (OSError, ValueError, SyntaxError):
                pass
        _check_stop(should_stop)
        image = oriented_image(path)
        _check_stop(should_stop)
        photo.width, photo.height = image.size
        photo.previewAvailable = True
        photo.previewMode = "image"
        photo.imageCategories, photo.imageCategoryReasons, photo.imageTextStatus = classify_image(path, image, software)
        _check_stop(should_stop)
        photo.ocrText, photo.ocrSearchStatus = extract_image_text(image)
        _check_stop(should_stop)
        try:
            photo.visualEmbedding = visual_search.embed_image(image)
            _check_stop(should_stop)
            photo.visualStatus = "indexed" if photo.visualEmbedding is not None else (
                "error" if visual_search.capabilities().get("available") else "unavailable")
        except ScanCancelled:
            raise
        except ImportError:
            photo.visualStatus = "unavailable"
        except Exception:
            # Object search must not prevent exact duplicate detection.
            photo.visualStatus = "error"
        # Animated files are only exact-byte candidates: comparing their first
        # frame would wrongly call different animations identical.
        if not animated:
            _check_stop(should_stop)
            rgba = image.convert("RGBA")
            _check_stop(should_stop)
            digest = hashlib.sha256()
            digest.update(f"{rgba.width}x{rgba.height}:RGBA:".encode("ascii"))
            digest.update(rgba.tobytes())
            _check_stop(should_stop)
            photo.pixel_hash = digest.hexdigest()
            rgb = rgb_image(image)
            gray = rgb.convert("L")
            dh = list(gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes())
            photo.dhash = _bits(dh[y * 9 + x] > dh[y * 9 + x + 1] for y in range(8) for x in range(8))
            ah = list(gray.resize((8, 8), Image.Resampling.LANCZOS).tobytes())
            average = sum(ah) / len(ah)
            photo.ahash = _bits(value > average for value in ah)
            photo.sample = rgb.resize((48, 48), Image.Resampling.LANCZOS).tobytes()
            _check_stop(should_stop)
        else:
            photo.issue = "Animated image: preview shows first frame; only identical files are compared."
    except ScanCancelled:
        raise
    except Exception as error:
        _check_stop(should_stop)
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
    _check_stop(should_stop)
    return photo


def discover(root: Path, recursive: bool, exclude_paths=(), include_all_files: bool = True,
             should_stop: Callable[[], bool] | None = None, options=None) -> tuple[list[Path], list[dict]]:
    settings = normalize_options(options)
    excluded = {os.path.normcase(str(Path(item).resolve())) for item in exclude_paths}
    paths, errors = [], []
    pending = [root]
    while pending:
        if should_stop is not None and should_stop():
            break
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    if should_stop is not None and should_stop():
                        break
                    entries.append(entry)
                entries.sort(key=lambda item: item.name.lower())
            for entry in entries:
                if should_stop is not None and should_stop():
                    break
                path = Path(entry.path)
                try:
                    if entry.name.lower() in SUPPORT_DIRECTORIES or os.path.normcase(str(path.absolute())) in excluded or is_reparse(path):
                        continue
                    if is_excluded(path.relative_to(root), settings, directory=entry.is_dir(follow_symlinks=False)):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        archive = settings['archives'] and is_archive(path)
                        if archive or (include_all_files or path.suffix.lower() in EXTENSIONS):
                            info = entry.stat(follow_symlinks=False)
                            if archive or accepts_file(path.relative_to(root), info.st_size, info.st_mtime, settings):
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


def compare_photos(photos: dict[str, Photo], threshold: float = 94, progress=None,
                   should_stop: Callable[[], bool] | None = None, options=None) -> list[dict]:
    settings = normalize_options(options)
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
        if should_stop is not None and should_stop():
            break
        for right in representatives[index + 1:]:
            if should_stop is not None and should_stop():
                break
            done += 1
            if progress and (done % 1000 == 0 or done == total):
                progress(done, total, "compare")
            if should_stop is not None and should_stop():
                break
            transformed = None
            if settings['imageTransforms'] or settings['cropMatching']:
                transformed = image_transforms.similarity(left, right, threshold, settings['imageTransforms'],
                                                         settings['cropMatching'], should_stop)
            if transformed:
                group = _group('similar', [left, right], transformed[0])
                group['reason'] = 'Visual suggestion: ' + transformed[1] + '. Review both images.'
                groups.append(group)
                continue
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
    if settings['audioMatching'] or settings['metadataMatching']:
        candidates = [p for p in photos.values() if
                      (settings['audioMatching'] and p.mediaType == 'audio' and p.audioFingerprint)
                      or (settings['metadataMatching'] and p.mediaType == 'image' and p.metadata.get('dateTaken'))]
        for index, left in enumerate(candidates):
            if should_stop is not None and should_stop():
                break
            for right in candidates[index + 1:]:
                if should_stop is not None and should_stop():
                    break
                if left.sha256 and left.sha256 == right.sha256:
                    continue
                if settings['audioMatching'] and left.mediaType == right.mediaType == 'audio':
                    score = audio_fingerprint.similarity(left, right)
                    if score is not None and score >= settings['audioThreshold']:
                        group = _group('audio', [left, right], score)
                        group['reason'] = 'Similar acoustic fingerprint. Review the recordings before removing a copy.'
                        groups.append(group)
                if settings['metadataMatching'] and metadata_match(left, right, settings):
                    group = _group('metadata', [left, right])
                    group['reason'] = 'Capture date and selected metadata criteria match. These can be different photographs.'
                    groups.append(group)
    if settings['videoMatching'] or settings['audioTagMatching']:
        candidates = [p for p in photos.values() if (settings['videoMatching'] and p.videoFrames)
                      or (settings['audioTagMatching'] and p.mediaType == 'audio' and p.audioTags)]
        for index, left in enumerate(candidates):
            if should_stop and should_stop():
                break
            for right in candidates[index + 1:]:
                if should_stop and should_stop():
                    break
                if left.sha256 and left.sha256 == right.sha256:
                    continue
                if left.mediaType == right.mediaType == 'video' and settings['videoMatching']:
                    score = video_match.similarity(left, right, should_stop)
                    if score is not None and score >= settings['videoThreshold']:
                        group = _group('video', [left, right], score)
                        group['reason'] = 'Similar frames sampled across the video. Review synchronized thumbnails before removal.'
                        groups.append(group)
                if (left.mediaType == right.mediaType == 'audio' and settings['audioTagMatching']
                        and video_match.tags_match(left, right, settings['audioTagFields'])):
                    group = _group('audio-tags', [left, right])
                    group['reason'] = 'Selected music tags match. Tags can describe different recordings.'
                    groups.append(group)
    groups = filter_comparison_scope(groups, photos, settings['comparisonScope'])
    kinds = {"exact": 0, "pixels": 1, "similar": 2, "audio": 3, "metadata": 4, "video": 5, "audio-tags": 6}
    return sorted(groups, key=lambda group: (kinds[group['kind']], -group["score"], group["id"]))


def filter_comparison_scope(groups, photos, scope='all'):
    if scope == 'all':
        return groups
    result = []
    for group in groups:
        members = [photos[member] for member in group['photos']]
        if scope == 'cross-root' and len({p.sourceRoot for p in members}) > 1:
            result.append(group)
        elif scope == 'reference' and any(p.reference for p in members) and any(not p.reference for p in members):
            result.append(group)
    return result


def calculate_stats(photos: dict[str, Photo], groups: list[dict]) -> dict:
    equivalences = defaultdict(list)
    for photo in photos.values():
        if photo.sourceKind != 'local':
            continue
        equivalences[photo.pixel_hash or photo.sha256 or photo.id].append(photo)
    recoverable = sum(sum(photo.size for photo in bucket) - max(photo.size for photo in bucket)
                      for bucket in equivalences.values() if len(bucket) > 1)
    return {"total": len(photos), "exactGroups": sum(group["kind"] in {'exact', 'pixels'} for group in groups),
            "similarGroups": sum(group["kind"] == "similar" for group in groups), "duplicateBytes": recoverable,
            "audioGroups": sum(group['kind'] == 'audio' for group in groups),
            "metadataGroups": sum(group['kind'] == 'metadata' for group in groups),
            "videoGroups": sum(group['kind'] == 'video' for group in groups),
            "audioTagGroups": sum(group['kind'] == 'audio-tags' for group in groups),
            "archiveFiles": sum(p.sourceKind == 'archive' for p in photos.values())}


def enrich_photo(photo, path, options=None, should_stop=None):
    """Add optional scan indexes to an already inspected local/member file."""
    settings = normalize_options(options)
    _check_stop(should_stop)
    if photo.mediaType == 'image':
        photo.metadata = read_metadata(path)
        if (settings['imageTransforms'] or settings['cropMatching']) and photo.pixel_hash:
            photo.transformSample = rgb_image(oriented_image(path)).resize((64, 64), Image.Resampling.LANCZOS).tobytes()
    if settings['videoMatching'] and photo.mediaType == 'video':
        try:
            photo.videoFrames, photo.duration = video_match.fingerprint(path, should_stop)
            photo.videoStatus = ('indexed' if sum(frame.get('informative', True) for frame in photo.videoFrames) >= 4
                                 else 'insufficient-detail')
        except audio_fingerprint.AudioCancelled:
            raise ScanCancelled()
        except Exception as error:
            photo.videoStatus = 'error'
            photo.issue = 'Video matching: ' + str(error)
    if settings['audioTagMatching'] and photo.mediaType == 'audio':
        try:
            duration, photo.audioTags = video_match.probe(path, should_stop)
            if duration:
                photo.duration = duration
        except audio_fingerprint.AudioCancelled:
            raise ScanCancelled()
        except Exception as error:
            photo.issue = 'Audio tags: ' + str(error)
    if settings['audioMatching'] and photo.mediaType == 'audio':
        try:
            photo.audioFingerprint, photo.audioDuration = audio_fingerprint.fingerprint(path, should_stop)
            photo.duration = photo.audioDuration
            photo.audioStatus = 'indexed' if photo.audioFingerprint else 'too-short-or-silent'
        except audio_fingerprint.AudioCancelled:
            raise ScanCancelled()
        except Exception as error:
            photo.audioStatus = 'error'
            photo.issue = 'Audio fingerprint: ' + str(error)
    _check_stop(should_stop)
    if file_signature(path) != tuple(photo.signature):
        raise ValueError('File changed while its search or matching index was being prepared. Scan again.')
    return photo


def scan_folder(root, recursive: bool = True, threshold: float = 94,
                on_progress: Callable | None = None, exclude_paths=(), include_all_files: bool = True,
                should_stop: Callable[[], bool] | None = None, options=None, cache_directory=None) -> ScanResult:
    return scan_folders([root], recursive, threshold, on_progress, exclude_paths, include_all_files,
                        should_stop, options, cache_directory)


def normalize_roots(roots):
    if not isinstance(roots, (list, tuple)) or not roots or len(roots) > 32:
        raise ValueError('Choose between one and 32 scan folders.')
    result = {}
    for value in roots:
        entry = value if isinstance(value, dict) else {'path': value}
        if not isinstance(entry.get('protected', False), bool) or not isinstance(entry.get('reference', False), bool):
            raise ValueError('Folder protection and reference flags must be true or false.')
        raw = entry.get('path')
        if not isinstance(raw, (str, Path)) or not str(raw).strip():
            raise ValueError('Choose a valid scan folder.')
        root = Path(os.path.abspath(os.path.expanduser(str(raw).strip())))
        if not root.is_dir():
            raise ValueError('The media folder does not exist or is not a folder: ' + str(root))
        if is_reparse(root):
            raise ValueError('Choose a normal folder rather than a linked folder or junction.')
        key = os.path.normcase(str(root))
        previous = result.get(key, {})
        reference = entry.get('reference', False) or previous.get('reference', False)
        result[key] = {'path': root, 'protected': reference or entry.get('protected', False)
                       or previous.get('protected', False), 'reference': reference}
    return list(result.values())


def _role_for(path, roots):
    eligible = [entry for entry in roots if path.is_relative_to(entry['path'])]
    specific = max(eligible, key=lambda entry: len(entry['path'].parts))
    return specific, any(entry['protected'] for entry in eligible), any(entry['reference'] for entry in eligible)


def duplicate_folders(photos, roots, scope='all', should_stop=None):
    """Compare complete readable directory trees, excluding incomplete inventories."""
    indexed = {os.path.normcase(str(p.file)): p for p in photos.values() if p.sourceKind == 'local'}
    trees = defaultdict(list)
    visited = set()

    def inspect(directory):
        if should_stop and should_stop():
            return None
        key = os.path.normcase(str(directory))
        if key in visited:
            return None
        visited.add(key)
        manifest, count, size, complete = [], 0, 0, True
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
            for entry in entries:
                if should_stop and should_stop():
                    return None
                if is_reparse(entry):
                    complete = False
                    continue
                if entry.is_dir():
                    if entry.name.casefold() in SUPPORT_DIRECTORIES:
                        complete = False
                        continue
                    child = inspect(entry)
                    if child is None:
                        complete = False
                        continue
                    manifest.append(('directory', entry.name, child[0]))
                    count += child[1]
                    size += child[2]
                elif entry.is_file():
                    photo = indexed.get(os.path.normcase(str(entry)))
                    if photo is None or not photo.sha256 or file_signature(entry) != tuple(photo.signature):
                        complete = False
                        continue
                    manifest.append(('file', entry.name, photo.sha256))
                    count += 1
                    size += photo.size
                else:
                    complete = False
        except OSError:
            return None
        if not complete:
            return None
        import json
        digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=True).encode()).hexdigest()
        if count:
            role, protected, reference = _role_for(directory, roots)
            trees[digest].append({'path': str(directory), 'sourceRoot': str(role['path']),
                                  'protected': protected, 'reference': reference,
                                  'fileCount': count, 'size': size})
        return digest, count, size

    # Visit enclosing roots first; nested selected folders are discovered once.
    for entry in sorted(roots, key=lambda e: len(e['path'].parts)):
        inspect(entry['path'])
    result = []
    for digest, folders in trees.items():
        if len(folders) < 2:
            continue
        if scope == 'cross-root' and len({folder['sourceRoot'] for folder in folders}) < 2:
            continue
        if scope == 'reference' and not (any(f['reference'] for f in folders) and any(not f['reference'] for f in folders)):
            continue
        result.append({'id': digest[:24], 'kind': 'folder', 'folders': folders,
                       'fileCount': folders[0]['fileCount'], 'totalSize': sum(f['size'] for f in folders)})
    return sorted(result, key=lambda group: (-group['totalSize'], group['id']))


def scan_folders(roots, recursive: bool = True, threshold: float = 94,
                 on_progress: Callable | None = None, exclude_paths=(), include_all_files: bool = True,
                 should_stop: Callable[[], bool] | None = None, options=None, cache_directory=None) -> ScanResult:
    settings = normalize_options(options)
    roots = normalize_roots(roots)
    if not isinstance(recursive, bool):
        raise ValueError('Include subfolders must be true or false.')
    if settings['comparisonScope'] == 'reference' and not any(entry['reference'] for entry in roots):
        raise ValueError('Choose at least one reference folder for reference comparisons.')
    threshold = float(threshold)
    if not 85 <= threshold <= 99:
        raise ValueError("Similarity threshold must be between 85 and 99.")
    result = ScanResult()
    if on_progress:
        on_progress(0, 0, "discover")
    if not isinstance(include_all_files, bool):
        raise ValueError("Include all file types must be true or false.")
    cache = None
    extra_excluded = list(exclude_paths)
    if settings['scanCache']:
        try:
            from scan_cache import InspectionCache
            cache = InspectionCache(cache_directory, settings)
            extra_excluded.append(Path(cache_directory) if cache_directory else
                                  Path(cache.connection.execute('PRAGMA database_list').fetchone()[2]).parent)
        except Exception:
            # A read-only disk or damaged cache must not prevent a manual scan.
            cache = None
    found = {}
    for entry in roots:
        if should_stop and should_stop():
            break
        discovered, errors = discover(entry['path'], recursive, extra_excluded, include_all_files, should_stop, settings)
        result.errors.extend(errors)
        for path in discovered:
            found[os.path.normcase(str(path))] = path
    paths = sorted(found.values(), key=lambda path: str(path).casefold())

    def retain(photo):
        if accepts_dimensions(photo, settings):
            result.photos[photo.id] = photo
            if photo.issue and (photo.mediaType == 'image' or photo.audioStatus == 'error' or photo.videoStatus == 'error'):
                result.errors.append({'name': photo.path, 'message': photo.issue})

    for index, path in enumerate(paths):
        if should_stop is not None and should_stop():
            break
        try:
            role, protected, reference = _role_for(path, roots)
            root = role['path']
            relative = str(path.relative_to(root))
            signature = file_signature(path)
            if accepts_file(relative, signature[2], signature[3] / 1e9, settings):
                photo = None
                if cache:
                    try:
                        photo = cache.get(path, signature)
                    except Exception:
                        cache.close()
                        cache = None
                if photo is not None and file_signature(path) != signature:
                    raise ValueError('File changed while its cached inspection was being checked. Scan again.')
                if photo is None:
                    photo = _inspect(path, root, should_stop) if should_stop is not None else _inspect(path, root)
                    enrich_photo(photo, path, settings, should_stop)
                    if cache:
                        try:
                            cache.put(photo)
                        except Exception:
                            cache.close()
                            cache = None
                photo.path = relative
                photo.sourceRoot, photo.protected, photo.reference = str(root), protected, reference
                retain(photo)
            if settings['archives'] and is_archive(path):
                archive_members, issues = list_members(path, should_stop)
                result.errors.extend({'name': relative + '!/' + issue['name'], 'message': issue['message']} for issue in issues)
                for member in archive_members:
                    _check_stop(should_stop)
                    member_path = relative + '!/' + member.name
                    if not include_all_files and Path(member.name).suffix.lower() not in EXTENSIONS:
                        continue
                    if not accepts_file(member_path, member.size, member.modified, settings):
                        continue
                    try:
                        with materialize_member(path, member.name, signature, should_stop) as materialized:
                            photo = _inspect(materialized, materialized.parent, should_stop) if should_stop is not None else _inspect(materialized, materialized.parent)
                            photo.name = Path(member.name.replace('\\', '/')).name
                            photo.path = member_path
                            enrich_photo(photo, materialized, settings, should_stop)
                        photo.id = hashlib.sha256((os.path.normcase(str(path)) + '!/' + member.name).encode('utf-8')).hexdigest()[:24]
                        photo.file, photo.signature, photo.size = path, signature, member.size
                        photo.sourceKind, photo.archiveContainer, photo.archiveMember = 'archive', relative, member.name
                        photo.sourceRoot, photo.protected, photo.reference = str(root), protected, reference
                        retain(photo)
                    except (ScanCancelled, ArchiveCancelled):
                        raise
                    except Exception as error:
                        result.errors.append({'name': member_path, 'message': 'Archive member: ' + str(error)})
        except (ScanCancelled, ArchiveCancelled):
            break
        except Exception as error:
            result.errors.append({"name": str(path), "message": str(error)})
        if on_progress:
            on_progress(index + 1, len(paths), "read")
    if on_progress and not (should_stop is not None and should_stop()):
        on_progress(0, len(result.photos) * max(0, len(result.photos) - 1) // 2, "compare")
    result.groups = compare_photos(result.photos, threshold, on_progress, should_stop, settings)
    if settings['folderMatching'] and not (should_stop and should_stop()):
        if on_progress:
            on_progress(0, 0, 'folders')
        result.folderGroups = duplicate_folders(result.photos, roots, settings['comparisonScope'], should_stop)
    result.stopped = should_stop is not None and should_stop()
    result.stats = calculate_stats(result.photos, result.groups)
    result.stats['cacheHits'] = cache.hits if cache else 0
    result.stats['cacheAvailable'] = cache is not None
    result.stats['folderGroups'] = len(result.folderGroups)
    if cache:
        cache.close()
    if on_progress:
        on_progress(len(result.photos) if result.stopped else len(paths), len(paths),
                    "stopped" if result.stopped else "complete")
    return result
