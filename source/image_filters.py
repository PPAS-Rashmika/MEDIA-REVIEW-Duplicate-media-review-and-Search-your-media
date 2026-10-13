"""Local image tags from capture/download metadata and Windows text recognition.

Tags can overlap. Capture and download tags are evidence-based suggestions;
neither the original file's history nor its contents are sent anywhere. OCR
results are retained only in memory for local content search, never persisted.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

OCR_TIMEOUT_SECONDS = 8.0
OCR_THUMBNAIL_LIMIT = 1600
ZONE_MAX_BYTES = 8192
OCR_CACHE_LIMIT = 256
OCR_TEXT_LIMIT = 16384

_SCREENSHOT_PREFIX = re.compile(
    r"^(?:screen[\s_.-]*shot|screen[\s_.-]*capture|snipping[\s_.-]*tool|snip)"
    r"(?:$|[\s_.()\[\]-]|\d)", re.IGNORECASE
)
_DOWNLOAD_PREFIX = re.compile(
    r"^(?:download(?:ed)?|image[\s_.-]+download(?:ed)?)"
    r"(?:$|[\s_.()\[\]-]|\d)", re.IGNORECASE
)
_CAPTURE_SOFTWARE = re.compile(
    r"\b(?:screen[\s_.-]*shot|screen[\s_.-]*capture|snipping[\s_.-]*tool|"
    r"greenshot|snagit|lightshot)\b", re.IGNORECASE
)
_SCREENSHOT_FOLDERS = {"screenshots", "screen shots", "screen captures", "screencaptures", "snips"}
_DOWNLOAD_FOLDERS = {"downloads", "download", "downloaded"}
_LOCAL = threading.local()


def _normal_folder(value: str) -> str:
    return re.sub(r"[_.-]+", " ", value.casefold()).strip()


def _plain_regular_path(path: Path) -> bool:
    """Check every existing component without resolving a junction or link."""
    try:
        current = path.absolute()
        first = True
        while True:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
            if first and not stat.S_ISREG(info.st_mode):
                return False
            if not first and not stat.S_ISDIR(info.st_mode):
                return False
            first = False
            parent = current.parent
            if parent == current:
                return True
            current = parent
    except (OSError, ValueError):
        return False


def _read_zone_stream(path: Path) -> bytes:
    """Read the fixed ADS only; URL fields and other referenced paths are ignored."""
    if os.name != "nt" or not _plain_regular_path(path):
        return b""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    read = kernel.ReadFile
    read.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    read.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # OPEN_REPARSE_POINT prevents following the target if it was replaced by a
    # link after the component check. No bytes are written to any file or ADS.
    handle = create(str(path.absolute()) + ":Zone.Identifier", 0x80000000, 7, None,
                    3, 0x00200000 | 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        return b""
    try:
        buffer = ctypes.create_string_buffer(ZONE_MAX_BYTES)
        length = wintypes.DWORD()
        if not read(handle, buffer, ZONE_MAX_BYTES, ctypes.byref(length), None):
            return b""
        if not _plain_regular_path(path):
            return b""
        return buffer.raw[:length.value]
    finally:
        close(handle)


def _internet_zone(path: Path) -> bool:
    try:
        data = _read_zone_stream(path)
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig",
                           errors="ignore")
        in_zone = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                in_zone = line.casefold() == "[zonetransfer]"
            elif in_zone and re.fullmatch(r"ZoneId\s*=\s*[34]", line, re.IGNORECASE):
                return True
    except (OSError, ValueError, UnicodeError):
        pass
    return False


def _metadata_categories(path: Path, software: str = "") -> tuple[list[str], list[str]]:
    tags: list[str] = []
    reasons: list[str] = []
    folders = {_normal_folder(part) for part in path.parent.parts}
    if _SCREENSHOT_PREFIX.match(path.stem):
        tags.append("screenshot")
        reasons.append("Screenshot filename")
    elif folders & _SCREENSHOT_FOLDERS:
        tags.append("screenshot")
        reasons.append("Screenshot folder")
    elif _CAPTURE_SOFTWARE.search(str(software)[:1024]):
        tags.append("screenshot")
        reasons.append("Screen capture software metadata")
    if folders & _DOWNLOAD_FOLDERS:
        tags.append("downloaded")
        reasons.append("Downloads folder")
    elif _DOWNLOAD_PREFIX.match(path.stem):
        tags.append("downloaded")
        reasons.append("Downloaded filename")
    elif _internet_zone(path):
        tags.append("downloaded")
        reasons.append("Windows Internet zone metadata")
    return tags, reasons


@dataclass
class _OcrContext:
    engine: object | None = None
    bitmap: object | None = None
    pixel_format: object | None = None
    max_dimension: int = OCR_THUMBNAIL_LIMIT
    language: str = ""
    reason: str = "Windows OCR is unavailable on this computer."


def _create_ocr_context() -> _OcrContext:
    if os.name != "nt":
        return _OcrContext(reason="Text detection requires Windows OCR.")
    try:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            for language in OcrEngine.available_recognizer_languages:
                engine = OcrEngine.try_create_from_language(language)
                if engine is not None:
                    break
        if engine is None:
            return _OcrContext(reason="No Windows OCR language is installed.")
        # Creating the buffer uses an additional WinRT namespace dynamically;
        # check it now so an incomplete bundle reports OCR as unavailable.
        probe = SoftwareBitmap.create_copy_from_buffer(b"\xff\xff\xff\xff", BitmapPixelFormat.BGRA8, 1, 1)
        probe.close()
        return _OcrContext(engine=engine, bitmap=SoftwareBitmap,
                           pixel_format=BitmapPixelFormat.BGRA8,
                           max_dimension=min(OCR_THUMBNAIL_LIMIT, int(OcrEngine.max_image_dimension)),
                           language=engine.recognizer_language.language_tag, reason="")
    except (ImportError, OSError, RuntimeError, AttributeError, ValueError):
        return _OcrContext()


def _ocr_context() -> _OcrContext:
    # Each scanner worker owns its engine. PyWinRT initializes its WinRT
    # apartment automatically; it does not require a UI or a message pump.
    if not hasattr(_LOCAL, "ocr"):
        _LOCAL.ocr = _create_ocr_context()
    return _LOCAL.ocr


def ocr_capabilities() -> dict:
    context = _ocr_context()
    return {"available": context.engine is not None, "engine": "Windows OCR",
            "language": context.language, "reason": context.reason}


def _enough_text(result) -> bool:
    # Reject isolated logo words and the short fragments occasionally found in
    # natural images. Use OCR word tokens without retaining their text.
    words = characters = letters = 0
    for line in result.lines:
        for word in line.words:
            alnum = [char for char in word.text if char.isalnum()]
            if len(alnum) >= 2:
                words += 1
                characters += len(alnum)
                letters += sum(char.isalpha() for char in alnum)
            if words >= 3 and characters >= 12 and letters >= 6:
                return True
    return False


@dataclass(frozen=True)
class OcrAnalysis:
    status: str
    text: str = ""


async def _recognize_image(context: _OcrContext, bitmap) -> OcrAnalysis:
    async def recognize():
        result = await context.engine.recognize_async(bitmap)
        # Retain short names and numbers too: the image-category threshold is
        # deliberately stricter than the text available to content search.
        text = "\n".join(" ".join(word.text for word in line.words) for line in result.lines)
        text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()[:OCR_TEXT_LIMIT]
        return OcrAnalysis("detected" if _enough_text(result) else "no-text", text)

    # PyWinRT 3.2 propagates cancellation to the native operation. Keep the
    # bitmap alive until completion/cancellation, then dispose it below.
    return await asyncio.wait_for(recognize(), timeout=OCR_TIMEOUT_SECONDS)


def _image_analysis(image: Image.Image) -> OcrAnalysis:
    context = _ocr_context()
    if context.engine is None:
        return OcrAnalysis("unavailable")
    bitmap = None
    cache = key = None
    try:
        # Composite transparency against white, as a transparent black glyph
        # must remain visible to OCR. Copies keep source pixels untouched.
        thumbnail = image.copy()
        thumbnail.thumbnail((context.max_dimension, context.max_dimension), Image.Resampling.LANCZOS)
        rgba = thumbnail.convert("RGBA")
        white = Image.new("RGBA", rgba.size, "white")
        white.alpha_composite(rgba)
        buffer = white.tobytes("raw", "BGRA")
        key = (context.language, white.size, hashlib.sha256(buffer).digest())
        if not hasattr(_LOCAL, "ocr_cache"):
            _LOCAL.ocr_cache = OrderedDict()
        cache = _LOCAL.ocr_cache
        if key in cache:
            cache.move_to_end(key)
            cached = cache[key]
            return cached if isinstance(cached, OcrAnalysis) else OcrAnalysis(str(cached))
        bitmap = context.bitmap.create_copy_from_buffer(buffer, context.pixel_format, white.width, white.height)
        result = asyncio.run(_recognize_image(context, bitmap))
        analysis = result if isinstance(result, OcrAnalysis) else OcrAnalysis("detected" if result else "no-text")
        cache[key] = analysis
        if len(cache) > OCR_CACHE_LIMIT:
            cache.popitem(last=False)
        return analysis
    except Exception:
        # Corrupt/unsupported OCR inputs and a per-image timeout should not
        # suppress duplicate discovery or mislabel an image as text-free.
        analysis = OcrAnalysis("error")
        # Category tagging and content extraction share the same decoded
        # image. Reuse failed recognition too instead of waiting twice for
        # the same native timeout. A new scan owns a fresh worker/cache.
        if cache is not None and key is not None:
            cache[key] = analysis
            if len(cache) > OCR_CACHE_LIMIT:
                cache.popitem(last=False)
        return analysis
    finally:
        if bitmap is not None:
            try:
                bitmap.close()
            except Exception:
                pass


def image_text_status(image: Image.Image) -> str:
    """Return the image-category status without exposing recognized text."""
    return _image_analysis(image).status


def extract_image_text(image: Image.Image) -> tuple[str, str]:
    """Return bounded recognized text for the app's in-memory search index."""
    analysis = _image_analysis(image)
    if analysis.status in {"unavailable", "error"}:
        return "", analysis.status
    return analysis.text, "indexed" if analysis.text else "empty"


def clear_ocr_cache() -> None:
    """Drop this worker's transient recognized-text cache."""
    if hasattr(_LOCAL, "ocr_cache"):
        _LOCAL.ocr_cache.clear()


def has_text(image: Image.Image) -> bool:
    return image_text_status(image) == "detected"


def classify_image(path: Path, image: Image.Image | None = None,
                   software: str = "") -> tuple[list[str], list[str], str]:
    """Classify a decoded image or apply metadata tags to an unreadable image."""
    tags, reasons = _metadata_categories(Path(path), software)
    status = image_text_status(image) if image is not None else "error"
    if status == "detected":
        tags.append("text")
        reasons.append("Text detected locally with Windows OCR")
    return tags, reasons, status
