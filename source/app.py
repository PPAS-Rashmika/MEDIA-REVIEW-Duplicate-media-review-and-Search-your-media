"""Local-only duplicate-media review server. Files never leave this computer."""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from contextlib import contextmanager
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

APP_DIRECTORY = Path(__file__).resolve().parent
VENDOR = APP_DIRECTORY / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

from PIL import Image
from scanner import (EXTENSIONS, ScanResult, _inspect, enrich_photo, calculate_stats, compare_photos, file_signature, is_reparse,
                     media_metadata, oriented_image, rgb_image, scan_folder)
from storage import StorageError, TrashStore
from organizer import OrganizeError, Organizer, photo_id as organized_photo_id
from version import __version__
from image_filters import clear_ocr_cache
import visual_search
from scan_options import normalize_options
from session_store import SessionStore, SessionError
from cloud_scan import CloudService
from workspace import MultiRootTrash, InterruptedScan, normalize_roots
from file_tools import FileTools


class ReviewError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIRECTORY.parent


PREVIEW_SECONDS = 120
BULK_PLAN_SECONDS = 600
BULK_PLAN_LIMIT = 8
SEARCH_QUERY_LIMIT = 256
SEARCH_PAGE_LIMIT = 100
VISUAL_SEARCH_MINIMUM = 0.20
VISUAL_SEARCH_LIMIT = 1000


def _search_text(value: str) -> str:
    """Normalize whitespace without interpreting user text as a pattern."""
    return " ".join(value.split())


def _literal_match(query: str, value: str) -> int:
    """Prefer a phrase, then require every whitespace-separated query word."""
    folded = _search_text(value).casefold()
    needle = query.casefold()
    if needle in folded:
        return 2
    return 1 if all(word in folded for word in needle.split()) else 0


def _search_snippet(text: str, query: str) -> str:
    plain = _search_text(text)
    if len(plain) <= 180:
        return plain
    folded = plain.casefold()
    position = folded.find(query.casefold())
    if position < 0:
        position = min((folded.find(word) for word in query.casefold().split()
                        if word in folded), default=0)
    start = max(0, position - 40)
    end = min(len(plain), start + 176)
    return ("… " if start else "") + plain[start:end] + (" …" if end < len(plain) else "")


def _valid_embedding(value) -> bool:
    return (isinstance(value, (tuple, list)) and len(value) == 512 and
            all(isinstance(number, (int, float)) and not isinstance(number, bool) and
                math.isfinite(number) for number in value))


def ffmpeg_binary() -> Path | None:
    candidates = [APP_DIRECTORY / "bin" / "ffmpeg.exe", APP_DIRECTORY / "bin" / "ffmpeg"]
    if getattr(sys, "frozen", False):
        candidates.extend((Path(sys.executable).parent / "bin" / "ffmpeg.exe",
                           Path(sys.executable).parent / "photo-review" / "bin" / "ffmpeg.exe"))
    for candidate in candidates:
        if candidate.is_file() and not is_reparse(candidate):
            return candidate
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def run_ffmpeg(arguments, timeout=60):
    binary = ffmpeg_binary()
    if binary is None:
        raise ReviewError("This media preview needs the bundled FFmpeg decoder. Identical file detection still works.", 415)
    try:
        result = subprocess.run(
            [str(binary), "-hide_banner", "-loglevel", "error", "-nostdin", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise ReviewError("Preview preparation took too long. This file is still available for identical-file review.", 415) from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()[-600:]
        raise ReviewError("This format or codec could not be previewed. Identical file detection still works."
                          + (" " + detail if detail else ""), 415)
    return result


class ReviewApp:
    def __init__(self, root=None, session_directory=None):
        # Remember only a picker hint: opening the app must not read or scan a
        # previous folder, including its recovery journal.
        self.picker_root = Path(os.path.abspath(str(root or default_root())))
        self.root = None
        self.roots = []
        self.catalog_ids = []
        self.catalog_only = False
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.scanning = False
        self.has_scanned = False
        self.progress = {"done": 0, "total": 0, "phase": "ready"}
        self.result = ScanResult(stats={"total": 0, "exactGroups": 0, "similarGroups": 0, "duplicateBytes": 0})
        self.store = None
        self.recursive = True
        self.threshold = 94
        self.include_all_files = True
        self.preview_cache = OrderedDict()
        self.scan_thread = None
        self.scan_stop_event = None
        self.scan_pause_event = None
        self.scan_resume_event = None
        self.scan_paused = False
        self.scan_pause_phase = "read"
        self.scan_options = normalize_options(None)
        self.session_store = SessionStore(session_directory)
        self.scan_cache_directory = self.session_store.directory / 'scan-cache'
        self.interrupted_store = InterruptedScan(self.session_store.directory)
        self.interrupted_info = None
        self.keeper_choices = {}
        self.tools = FileTools(self)
        self.session_busy = False
        self.session_info = None
        self.session_load = None
        self.cloud = CloudService()
        self.scan_generation = 0
        self.media_lock = threading.Lock()
        self.decoder_slots = threading.BoundedSemaphore(2)
        self.media_directory = None
        self.media_cache = OrderedDict()
        self.review_revision = 0
        self.bulk_plans = OrderedDict()
        self.organizer = None
        self.organize_plans = OrderedDict()
        self.organization_thread = None
        self.organization_recompare = False
        self.closing = False
        self.organization = {"running": False, "kind": None, "phase": "ready", "done": 0,
                             "total": 0, "moved": 0, "message": "", "lastResult": None}

    def _organization_state(self):
        return dict(self.organization, history=self.organizer.history() if self.organizer else [])

    def _refresh_stats(self):
        self.result.folderGroups = []
        self.result.stats = dict(self.result.stats, **calculate_stats(self.result.photos, self.result.groups))
        self.result.stats['folderGroups'] = len(getattr(self.result, 'folderGroups', []))

    def state(self) -> dict:
        with self.lock:
            try:
                trash_count = len(self.store.items()) if self.store else 0
            except StorageError:
                trash_count = 0
            return {"root": str(self.root) if self.root else "", "roots": list(self.roots),
                    "catalogIds": list(self.catalog_ids), "catalogOnly": self.catalog_only,
                    "keeperChoices": dict(self.keeper_choices), "interruptedScan": self.interrupted_info,
                    "folderGroups": list(getattr(self.result, 'folderGroups', [])), "hasScanned": self.has_scanned,
                    "scanning": self.scanning,
                    "stopRequested": bool(self.scanning and self.scan_stop_event is not None and self.scan_stop_event.is_set()),
                    "scanStopped": self.result.stopped,
                    "pauseRequested": bool(self.scanning and self.scan_pause_event is not None and self.scan_pause_event.is_set()),
                    "scanPaused": bool(self.scanning and self.scan_paused),
                    "scanOptions": dict(self.scan_options),
                    "recursive": self.recursive, "includeAllFiles": self.include_all_files,
                    "threshold": self.threshold,
                    "sessionBusy": self.session_busy, "session": self.session_info,
                    "sessionLoad": self.session_load,
                    "progress": dict(self.progress), "previewSeconds": PREVIEW_SECONDS,
                    "photos": [photo.public() for photo in self.result.photos.values()],
                    "groups": [dict(group, photos=list(group["photos"])) for group in self.result.groups],
                    "errors": list(self.result.errors), "stats": dict(self.result.stats),
                    "trashCount": trash_count, "token": self.token, "revision": self.review_revision,
                    "organization": self._organization_state()}

    def _progress(self, done, total, phase, generation=None, stop_event=None):
        with self.lock:
            if self.closing or (generation is not None and generation != self.scan_generation):
                return
            # A decoder finishing after Stop must not turn the status back to
            # Reading/Comparing, or claim completion before partial results exist.
            if stop_event is not None and stop_event.is_set():
                phase = "stopping"
            elif self.scan_pause_event is not None and self.scan_pause_event.is_set():
                phase = "paused" if self.scan_paused else "pausing"
            self.progress = {"done": done, "total": total, "phase": phase}

    def stop_scan(self) -> dict:
        """Request cancellation of the active run without starting any work."""
        with self.lock:
            accepted = bool(not self.closing and self.scanning and self.scan_stop_event is not None
                            and not self.scan_stop_event.is_set())
            if accepted:
                self.scan_stop_event.set()
                if self.scan_resume_event is not None:
                    self.scan_resume_event.set()
                self.progress = dict(self.progress, phase="stopping")
            return {"ok": True, "accepted": accepted, "state": self.state()}

    def pause_scan(self) -> dict:
        with self.lock:
            accepted = bool(not self.closing and self.scanning and self.scan_pause_event is not None
                            and not self.scan_pause_event.is_set() and not self.scan_stop_event.is_set())
            if accepted:
                self.scan_resume_event.clear()
                self.scan_pause_phase = self.progress.get("phase", "read")
                self.scan_pause_event.set()
                self.progress = dict(self.progress, phase="pausing")
            return {"ok": True, "accepted": accepted, "state": self.state()}

    def resume_scan(self) -> dict:
        with self.lock:
            accepted = bool(not self.closing and self.scanning and self.scan_pause_event is not None
                            and self.scan_pause_event.is_set() and not self.scan_stop_event.is_set())
            if accepted:
                self.scan_pause_event.clear()
                self.scan_paused = False
                self.scan_resume_event.set()
                self.progress = dict(self.progress, phase=self.scan_pause_phase)
            return {"ok": True, "accepted": accepted, "state": self.state()}

    def _scan_checkpoint(self, generation, stop_event, pause_event, resume_event):
        """Block only the worker, without holding the app lock while paused."""
        if stop_event.is_set():
            return True
        while pause_event.is_set() and not stop_event.is_set():
            with self.lock:
                if self.closing or generation != self.scan_generation:
                    return True
                self.scan_paused = True
                self.progress = dict(self.progress, phase="paused")
            resume_event.wait(0.25)
        with self.lock:
            if generation == self.scan_generation:
                self.scan_paused = False
        return stop_event.is_set() or self.closing

    def start_scan(self, path=None, recursive=True, threshold=94, include_all_files=True, options=None,
                   roots=None, catalog_ids=None) -> dict:
        if not isinstance(recursive, bool):
            raise ReviewError("Include subfolders must be true or false.")
        if not isinstance(include_all_files, bool):
            raise ReviewError("Include all file types must be true or false.")
        options = normalize_options(options)
        try:
            threshold = float(threshold)
        except (TypeError, ValueError) as error:
            raise ReviewError("Choose a similarity threshold between 85 and 99.") from error
        if not 85 <= threshold <= 99:
            raise ReviewError("Choose a similarity threshold between 85 and 99.")
        selected_roots = normalize_roots(roots, path or self.root)
        target = Path(selected_roots[0]['path'])
        catalog_ids = [] if catalog_ids is None else catalog_ids
        if not isinstance(catalog_ids, list) or len(catalog_ids) > 32 or any(not isinstance(x, str) for x in catalog_ids):
            raise ReviewError('Choose up to 32 saved drive catalogs.')
        for catalog_id in catalog_ids:
            self.session_store._path(catalog_id)
        with self.lock:
            if self.closing or self.organization["running"] or self.session_busy:
                raise ReviewError("Wait for file organization to finish before scanning another folder.", 409)
            if self.scanning or (self.scan_thread is not None and self.scan_thread.is_alive()):
                raise ReviewError("A scan is already running. Wait for it to finish.", 409)
            # Journal validation happens before replacing the current review.
            try:
                store = (TrashStore(target) if len(selected_roots) == 1 and not selected_roots[0]['protected']
                         else MultiRootTrash(selected_roots))
                organizer = Organizer(target)
            except (StorageError, OrganizeError) as error:
                raise ReviewError(str(error), 409) from error
            self.interrupted_store.write(selected_roots, {'recursive': recursive, 'threshold': threshold,
                'includeAllFiles': include_all_files, 'options': options, 'catalogIds': catalog_ids})
            self._invalidate_bulk_plans()
            self.root, self.store, self.organizer = target, store, organizer
            self.roots = selected_roots
            self.catalog_ids = list(dict.fromkeys(catalog_ids))
            self.catalog_only = False
            self.keeper_choices = {}
            self.organization = {"running": False, "kind": None, "phase": "ready", "done": 0,
                                 "total": 0, "moved": 0, "message": "", "lastResult": None}
            self.picker_root = target
            self.recursive, self.threshold, self.include_all_files = recursive, threshold, include_all_files
            self.scan_options = options
            self.session_info = None
            self.session_load = None
            self.has_scanned = True
            self.scanning = True
            self.scan_generation += 1
            generation = self.scan_generation
            stop_event = self.scan_stop_event = threading.Event()
            self.scan_pause_event = threading.Event()
            self.scan_resume_event = threading.Event()
            self.scan_resume_event.set()
            self.scan_paused = False
            self.result = ScanResult(stats={"total": 0, "exactGroups": 0, "similarGroups": 0, "duplicateBytes": 0})
            self.progress = {"done": 0, "total": 0, "phase": "discover"}
            self.preview_cache.clear()
            visual_search.clear_caches()
            self.interrupted_info = {'roots': self.roots, 'available': True}
            self.scan_thread = threading.Thread(target=self._scan,
                                                args=(target, recursive, threshold, include_all_files, generation, stop_event),
                                                daemon=True, name="media-scan")
            try:
                self.scan_thread.start()
            except RuntimeError as error:
                self.scanning = False
                self.progress = dict(self.progress, phase='error')
                raise ReviewError('The scan could not start. Try again.', 409) from error
            return self.state()

    def _scan(self, target, recursive, threshold, include_all_files, generation, stop_event):
        result = None
        failure = None
        try:
            excluded = [APP_DIRECTORY]
            excluded.extend(APP_DIRECTORY / name for name in ("vendor", "build", "dist", "bin", "static", "tests", "cache"))
            excluded.append(APP_DIRECTORY.parent / "models")
            if self.media_directory is not None:
                excluded.append(Path(self.media_directory.name))
            if getattr(sys, "frozen", False):
                executable = Path(sys.executable).resolve()
                excluded.extend((executable, executable.parent / "_internal", executable.parent / "models"))
                for name in ("photo-review", "source"):
                    support = executable.parent / name
                    if (support / "app.py").is_file():
                        excluded.append(support)
            progress = lambda done, total, phase: self._progress(done, total, phase, generation, stop_event)
            checkpoint = lambda: self._scan_checkpoint(generation, stop_event, self.scan_pause_event,
                                                       self.scan_resume_event)
            excluded.append(self.session_store.directory)
            engine_options = dict(self.scan_options, comparisonScope='all') if self.catalog_ids else self.scan_options
            if len(self.roots) == 1 and not self.roots[0]['protected'] and not self.catalog_ids:
                result = scan_folder(target, recursive, threshold, progress, exclude_paths=excluded,
                                     include_all_files=include_all_files, should_stop=checkpoint,
                                     options=engine_options, cache_directory=self.scan_cache_directory)
            else:
                from scanner import scan_folders
                result = scan_folders(self.roots, recursive, threshold, progress, exclude_paths=excluded,
                                      include_all_files=include_all_files, should_stop=checkpoint,
                                      options=engine_options, cache_directory=self.scan_cache_directory)
            if self.catalog_ids and not stop_event.is_set():
                for catalog_id in self.catalog_ids:
                    _, catalog, _, _, _ = self.session_store.load(catalog_id, offline=True)
                    for identity, photo in catalog.photos.items():
                        result.photos.setdefault(identity, photo)
                result.groups = compare_photos(result.photos, threshold, progress, checkpoint, options=self.scan_options)
                result.stats.update(calculate_stats(result.photos, result.groups))
        except Exception as error:
            failure = error
        finally:
            clear_ocr_cache()
            if stop_event.is_set():
                visual_search.clear_caches()
            with self.lock:
                if not self.closing and generation == self.scan_generation:
                    if result is not None:
                        result.stopped = result.stopped or stop_event.is_set() or failure is not None
                        if failure is not None:
                            result.errors.append({'name': 'Workspace scan', 'message': str(failure)})
                        self.result = result
                        self.progress = dict(self.progress,
                                             phase='error' if failure is not None else "stopped" if result.stopped else "complete")
                        if result.stopped:
                            self.progress["done"] = len(result.photos)
                        else:
                            try:
                                self.interrupted_store.clear()
                            except OSError:
                                result.errors.append({'name': 'Scan recovery', 'message': 'The completed scan checkpoint could not be cleared.'})
                            self.interrupted_info = None
                    elif failure is not None:
                        self.result.errors.append({"name": str(target), "message": str(failure)})
                        self.progress = dict(self.progress, phase="error")
                    self.scanning = False
                    self.scan_paused = False

    def _require_idle(self):
        if self.session_busy:
            raise ReviewError("Wait for the saved scan to finish loading or saving.", 409)
        if self.closing or self.organization["running"]:
            raise ReviewError("Wait for file organization to finish before starting another action.", 409)
        if self.scanning or (self.scan_thread is not None and self.scan_thread.is_alive()):
            raise ReviewError("Wait for the scan to finish before searching, reviewing, or restoring files.", 409)
        if self.store is None and not self.catalog_only:
            raise ReviewError("Choose a folder and click Scan before searching, reviewing, or restoring files.", 409)

    def interrupted_scan(self):
        body = self.interrupted_store.read()
        self.interrupted_info = {'roots': body['roots'], 'available': True} if body else None
        return {'ok': True, 'checkpoint': self.interrupted_info}

    def resume_interrupted(self):
        body = self.interrupted_store.read()
        if body is None:
            raise ReviewError('There is no unfinished scan to resume.')
        settings = body['settings']
        options = dict(settings.get('options') or {}, scanCache=True)
        return self.start_scan(recursive=settings.get('recursive', True), threshold=settings.get('threshold', 94),
            include_all_files=settings.get('includeAllFiles', True), options=options,
            roots=body['roots'], catalog_ids=settings.get('catalogIds', []))

    def sessions(self):
        return {"ok": True, "sessions": self.session_store.list()}

    def save_session(self, name):
        with self.lock:
            self._require_idle()
            if not self.has_scanned:
                raise ReviewError("Scan a folder before saving its results.", 409)
            root = self.root
            result = copy.deepcopy(self.result)
            settings = {"recursive": self.recursive, "threshold": self.threshold,
                        "includeAllFiles": self.include_all_files, "options": copy.deepcopy(self.scan_options),
                        "roots": copy.deepcopy(self.roots)}
            known = {os.path.normcase(item['path']) for item in settings['roots']}
            for photo in result.photos.values():
                source = getattr(photo, 'sourceRoot', '')
                if source and os.path.normcase(source) not in known:
                    settings['roots'].append({'path': source, 'protected': True, 'reference': False})
                    known.add(os.path.normcase(source))
            settings['roots'] = normalize_roots(settings['roots'], root, require_online=False, allow_nested=True)
            self.session_busy = True
        try:
            summary = self.session_store.save(name, root, result, settings)
            with self.lock:
                self.session_info = summary
            return {"ok": True, "session": summary, "sessions": self.session_store.list()}
        finally:
            with self.lock:
                self.session_busy = False

    def load_session(self, session_id, offline=False):
        if type(offline) is not bool:
            raise ReviewError('Offline catalog mode must be true or false.')
        with self.lock:
            if self.closing or self.session_busy or self.organization["running"]:
                raise ReviewError("Wait for the current action to finish before opening a saved scan.", 409)
            if self.scanning or (self.scan_thread is not None and self.scan_thread.is_alive()):
                raise ReviewError("Stop the active scan before opening saved results.", 409)
            self.session_busy = True
        try:
            root, result, settings, summary, skipped = self.session_store.load(session_id, offline=offline)
            recursive = settings.get("recursive", True)
            include_all = settings.get("includeAllFiles", True)
            threshold = settings.get("threshold", 94)
            if (type(recursive) is not bool or type(include_all) is not bool or
                    type(threshold) not in {int, float} or not 85 <= threshold <= 99):
                raise SessionError("The saved scan has invalid scan settings.")
            options = normalize_options(settings.get("options"))
            # Derive comparison membership from the validated records rather
            # than trusting an edited or out-of-date saved group list.
            result.groups = compare_photos(result.photos, threshold, options=options)
            result.stats = calculate_stats(result.photos, result.groups)
            roots = normalize_roots(settings.get('roots'), root, require_online=False, allow_nested=True)
            if offline:
                roots = [dict(item, protected=True) for item in roots]
            for photo in result.photos.values():
                role = next(item for item in roots if os.path.normcase(item['path']) == os.path.normcase(photo.sourceRoot))
                photo.protected = offline or role['protected']
                photo.reference = role['reference']
            store = (None if offline else TrashStore(root) if len(roots) == 1 and not roots[0]['protected'] else MultiRootTrash(roots))
            organizer = None if offline else Organizer(root)
            with self.lock:
                if self.closing:
                    raise ReviewError("Media Review is closing.", 409)
                self._invalidate_bulk_plans()
                self.root, self.picker_root = root, root
                self.roots = roots
                self.catalog_ids = []
                self.catalog_only = offline
                self.keeper_choices = {}
                self.store, self.organizer = store, organizer
                self.result = result
                self.recursive, self.include_all_files, self.threshold = recursive, include_all, threshold
                self.scan_options = options
                self.has_scanned = True
                self.session_info = summary
                self.session_load = {"restoredFiles": len(result.photos), "skippedFiles": skipped,
                                     "skipped": skipped,
                                     "message": 'Offline catalog opened. Files are read-only.' if offline else "Saved results reopened. Files added after this scan are not included."}
                self.progress = {"done": len(result.photos), "total": len(result.photos),
                                 "phase": "stopped" if result.stopped else "complete"}
                self.preview_cache.clear()
                visual_search.clear_caches()
                clear_ocr_cache()
                self.session_busy = False
                return self.state()
        finally:
            with self.lock:
                self.session_busy = False

    def search(self, query, mode="all", media_type="all", category="all", offset=0, limit=50) -> dict:
        """Search the current, read-only library index after an explicit request.

        OCR and embeddings stay in memory. Every result is a currently scanned
        file, including unique files that have no duplicate comparison group.
        """
        if not isinstance(query, str) or not 0 < len(query) <= SEARCH_QUERY_LIMIT:
            raise ReviewError("Enter a search phrase of 1 to 256 characters.")
        query = _search_text(query)
        if not query:
            raise ReviewError("Enter a name, word, object, or scene to search for.")
        if not isinstance(mode, str) or mode not in {"all", "text", "visual", "filename"}:
            raise ReviewError("Choose All content, Text in images, Objects and scenes, or File names.")
        if not isinstance(media_type, str) or media_type not in {"all", "image", "video", "audio", "other"}:
            raise ReviewError("Choose a supported media filter.")
        if not isinstance(category, str) or category not in {"all", "screenshot", "downloaded", "text", "other"}:
            raise ReviewError("Choose a supported image category.")
        if (not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 1_000_000 or
                not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= SEARCH_PAGE_LIMIT):
            raise ReviewError("Choose a nonnegative result offset and a page size between 1 and 100.")
        with self.lock:
            self._require_idle()
            revision = self.review_revision
            photos = list(self.result.photos.values())
            text_indexed = sum(photo.mediaType == "image" and
                               getattr(photo, "ocrSearchStatus", "") in {"indexed", "empty"}
                               for photo in photos)
            visual_indexed = sum(photo.mediaType == "image" and
                                 _valid_embedding(getattr(photo, "visualEmbedding", None))
                                 for photo in photos)
            filtered = [photo for photo in photos if
                        (media_type == "all" or photo.mediaType == media_type) and
                        (category == "all" or (photo.mediaType == "image" and
                         (not photo.imageCategories if category == "other" else
                          category in photo.imageCategories)))]

        capability = visual_search.capabilities()
        visual_available = bool(capability.get("available"))
        visual_reason = str(capability.get("reason", ""))
        query_embedding = None
        notes = []
        # Capabilities checks do not load a model. Inference only happens after
        # Search is submitted and there are eligible indexed image candidates.
        if mode in {"all", "visual"} and visual_available and any(
                photo.mediaType == "image" and _valid_embedding(getattr(photo, "visualEmbedding", None)) for photo in filtered):
            try:
                query_embedding = visual_search.embed_text(query)
                if not _valid_embedding(query_embedding):
                    raise ValueError("No usable visual query embedding.")
            except Exception:
                visual_available = False
                visual_reason = "Objects and scenes search could not be prepared. Text and file name search remain available."
        if mode in {"all", "visual"} and not visual_available:
            notes.append(visual_reason or "Objects and scenes search is unavailable on this computer.")
        if mode in {"all", "text"}:
            incomplete_ocr = sum(photo.mediaType == "image" and
                                 getattr(photo, "ocrSearchStatus", "unavailable") in {"unavailable", "error"}
                                 for photo in photos)
            if incomplete_ocr:
                notes.append(f"Text recognition was unavailable or incomplete for {incomplete_ocr} image(s).")
        if mode in {"all", "visual"} and visual_available and visual_indexed < sum(
                photo.mediaType == "image" for photo in photos):
            notes.append("Some images could not be indexed for objects and scenes search.")

        literal_results, visual_results = [], []
        for photo in filtered:
            text = getattr(photo, "ocrText", "") if photo.mediaType == "image" else ""
            text_match = _literal_match(query, text) if text and mode in {"all", "text"} else 0
            filename_match = _literal_match(query, photo.name + " " + photo.path) if mode in {"all", "filename"} else 0
            embedding = getattr(photo, "visualEmbedding", None)
            score = None
            if photo.mediaType == "image" and query_embedding is not None and _valid_embedding(embedding):
                similarity = sum(left * right for left, right in zip(query_embedding, embedding))
                if math.isfinite(similarity) and similarity >= VISUAL_SEARCH_MINIMUM:
                    score = min(1.0, similarity)
            matches = (["text"] if text_match else []) + (["filename"] if filename_match else [])
            if score is not None:
                matches.append("visual")
            if not matches:
                continue
            item = dict(photo.public(), matches=matches,
                        snippet=_search_snippet(text, query) if text_match else "")
            if score is not None:
                item["visualScore"] = round(score, 6)
            key = (photo.path.casefold(), photo.id)
            if text_match or filename_match:
                literal_results.append(((-text_match, -filename_match, -(score or 0), *key), item))
            else:
                visual_results.append(((-score, *key), item))
        literal_results.sort(key=lambda match: match[0])
        visual_results.sort(key=lambda match: match[0])
        visual_truncated = len(visual_results) > VISUAL_SEARCH_LIMIT
        ordered = [item for _, item in literal_results] + [item for _, item in visual_results[:VISUAL_SEARCH_LIMIT]]
        if visual_truncated:
            notes.append("Showing the 1,000 closest visual suggestions. Use a more specific phrase to narrow the results.")
        with self.lock:
            self._require_idle()
            if revision != self.review_revision:
                raise ReviewError("The media library changed while searching. Search again.", 409)
        total = len(ordered)
        return {"ok": True, "query": query, "mode": mode, "total": total, "offset": offset, "limit": limit,
                "hasMore": offset + limit < total, "results": ordered[offset:offset + limit],
                "visualAvailable": visual_available, "visualReason": visual_reason,
                "textIndexedImages": text_indexed, "visualIndexedImages": visual_indexed,
                "revision": revision, "notes": notes}

    def show_file(self, photo_id) -> dict:
        """Reveal a validated current file, without opening or executing it."""
        if not isinstance(photo_id, str) or not photo_id:
            raise ReviewError("Choose a current search result to show in its folder.")
        with self.lock:
            self._require_idle()
            path, _ = self._photo_path(photo_id)
            if os.name != "nt":
                raise ReviewError("Show in folder is available in the Windows desktop app.", 415)
            # A fixed executable and argument list avoid shell interpretation
            # of filenames. The original path is never accepted from the client.
            windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            subprocess.Popen([str(windows_root / "explorer.exe"), "/select,", str(path)],
                             close_fds=True)
            return {"ok": True, "id": photo_id}

    def _invalidate_bulk_plans(self):
        self.review_revision += 1
        self.bulk_plans.clear()
        self.organize_plans.clear()

    def _organization_progress(self, done, total, phase, moved=None):
        with self.lock:
            self.organization.update(done=done, total=total, phase=phase)
            if moved is not None:
                self.organization["moved"] = moved

    def organize_plan(self, mode, year_basis="taken"):
        """Preview all current files without creating directories or changing files."""
        if not isinstance(mode, str) or mode not in {"type", "year"}:
            raise ReviewError("Choose file type or year organization.")
        if not isinstance(year_basis, str) or year_basis not in {"taken", "modified"}:
            raise ReviewError("Choose date taken or date modified for year organization.")
        with self.lock:
            self._require_idle()
            organizer = self.organizer
            if organizer is None:
                raise ReviewError("Scan this folder again before organizing files.", 409)
            photos = [photo for photo in self.result.photos.values()
                      if getattr(photo, "sourceKind", "local") == "local" and photo.file.is_relative_to(self.root)
                      and not getattr(photo, 'protected', False) and not getattr(photo, 'offline', False)]
            revision = self.review_revision
            self.organization.update(running=True, kind=None, phase="planning", done=0,
                                     total=len(photos), moved=0, message="Preparing organization preview.")
        try:
            plan = organizer.create_plan(photos, mode, year_basis, self._organization_progress)
            with self.lock:
                if revision != self.review_revision or organizer is not self.organizer:
                    raise ReviewError("The library changed. Prepare a new organization preview.", 409)
                now = time.monotonic()
                for key in list(self.organize_plans):
                    if self.organize_plans[key]["expires"] <= now:
                        del self.organize_plans[key]
                plan_id = secrets.token_urlsafe(32)
                self.organize_plans[plan_id] = {"plan": plan, "organizer": organizer,
                                               "revision": revision, "expires": now + BULK_PLAN_SECONDS}
                while len(self.organize_plans) > BULK_PLAN_LIMIT:
                    self.organize_plans.popitem(last=False)
                return dict({key: value for key, value in plan.items() if not key.startswith("_")},
                            planId=plan_id, revision=revision, expiresIn=BULK_PLAN_SECONDS)
        except (OrganizeError, OSError) as error:
            raise ReviewError(str(error), 409) from error
        finally:
            with self.lock:
                self.organization.update(running=False, kind=None, phase="ready", message="")

    def _organization_moved(self, source, destination):
        """Retarget existing indexes without rescanning or decoding moved content."""
        try:
            signature = file_signature(destination)
        except OSError:
            signature = None  # Keep its index even if another program removed it after the move.
        with self.lock:
            source_id = organized_photo_id(source)
            photo = self.result.photos.get(source_id)
            source_root = Path(getattr(photo, 'sourceRoot', '') or next((item['path'] for item in self.roots if destination.is_relative_to(Path(item['path']))), str(self.root)))
            if photo is None:
                should_index = ((self.recursive or destination.parent == source_root) and
                                (self.include_all_files or destination.suffix.lower() in EXTENSIONS))
            else:
                should_index = False
        if photo is None:
            if not should_index:
                return
            # Undo after a new nonrecursive scan can bring files back into its
            # visible scope. Inspect only these restored files, never rescan the
            # folder. A preview failure cannot change the actual move outcome.
            try:
                restored = enrich_photo(_inspect(destination, source_root), destination, self.scan_options)
                with self.lock:
                    restored.sourceRoot = str(source_root)
                    self.result.photos[restored.id] = restored
                    self.organization_recompare = True
            except Exception as error:
                with self.lock:
                    self.result.errors.append({"name": str(destination.relative_to(source_root)),
                                               "message": "Moved successfully; library index could not be refreshed: " + str(error)})
            return
        with self.lock:
            photo.file, photo.name = destination, destination.name
            photo.sourceRoot = str(source_root)
            photo.path = str(destination.relative_to(source_root))
            photo.signature = signature or photo.signature
            photo.id = organized_photo_id(destination)
            self.keeper_choices = {group_id: photo.id if member == source_id else member for group_id,member in self.keeper_choices.items()}
            del self.result.photos[source_id]
            self.result.photos[photo.id] = photo
            self.result.groups = [dict(group, photos=[photo.id if member == source_id else member
                                                      for member in group["photos"]])
                                  for group in self.result.groups]
            # Moving an archive moves its members with it; keep their virtual
            # locations and comparison IDs aligned without extracting or scanning.
            archive_ids = {}
            for child in list(self.result.photos.values()):
                if getattr(child, "sourceKind", "local") == "archive" and child.file == source:
                    old_id = child.id
                    child.file = destination
                    child.archiveContainer = str(destination.relative_to(source_root))
                    child.path = child.archiveContainer + '!/' + child.archiveMember
                    child.signature = signature or child.signature
                    child.id = hashlib.sha256((os.path.normcase(str(destination)) + '!/' +
                                               child.archiveMember).encode('utf-8')).hexdigest()[:24]
                    del self.result.photos[old_id]
                    self.result.photos[child.id] = child
                    archive_ids[old_id] = child.id
            if archive_ids:
                self.result.groups = [dict(group, photos=[archive_ids.get(member, member)
                                                         for member in group["photos"]])
                                      for group in self.result.groups]
            for issue in self.result.errors:
                if issue.get("name") == str(source.relative_to(source_root)):
                    issue["name"] = photo.path
                elif str(issue.get("name", "")).startswith(str(source.relative_to(source_root)) + '!/'):
                    issue['name'] = str(destination.relative_to(source_root)) + str(issue['name'])[len(str(source.relative_to(source_root))):]
            self.preview_cache.clear()
            # Cache keys include old IDs. Generated previews can be regenerated;
            # cached image/text embeddings stay attached to their Photo instance.
        with self.media_lock:
            self.media_cache.clear()

    def _run_organization(self, organizer, kind, operation_id, plan=None):
        result = None
        try:
            if kind == "move":
                result = organizer.execute(plan, operation_id, progress=self._organization_progress,
                                           moved=self._organization_moved)
            else:
                result = organizer.undo(operation_id, progress=self._organization_progress,
                                        moved=self._organization_moved)
        except Exception as error:
            result = {"ok": False, "operationId": operation_id, "kind": kind,
                      "mode": plan["mode"] if plan else "", "movedCount": self.organization["moved"],
                      "skippedCount": 0, "failures": [{"name": "Organization", "path": "", "error": str(error)}],
                      "message": str(error)}
        finally:
            if self.organization_recompare:
                try:
                    with self.lock:
                        photos = dict(self.result.photos)
                        self.organization.update(phase="refreshing", message="Refreshing restored files in this library.")
                    groups = compare_photos(photos, self.threshold, options=self.scan_options)
                    with self.lock:
                        self.result.groups = groups
                except Exception as error:
                    with self.lock:
                        self.result.errors.append({"name": "Library", "message": "Files were moved; comparison refresh failed: " + str(error)})
                finally:
                    self.organization_recompare = False
            with self.lock:
                self._refresh_stats()
                self._invalidate_bulk_plans()
                self.preview_cache.clear()
                self.organization.update(running=False, phase="complete" if result["ok"] else "error",
                                         message=result["message"], moved=result["movedCount"], lastResult=result)

    def organize_execute(self, plan_id):
        with self.lock:
            self._require_idle()
            if not isinstance(plan_id, str) or not plan_id:
                raise ReviewError("Prepare an organization preview before confirming.", 409)
            entry = self.organize_plans.get(plan_id)
            if (entry is None or entry["expires"] <= time.monotonic() or
                    entry["revision"] != self.review_revision or entry["organizer"] is not self.organizer):
                self.organize_plans.pop(plan_id, None)
                raise ReviewError("The organization preview expired or changed. Prepare a new preview.", 409)
            if not entry["plan"]["moveCount"]:
                raise ReviewError("There are no files to move in this preview.", 409)
            self._invalidate_bulk_plans()
            self.organization_recompare = False
            operation_id = uuid.uuid4().hex
            self.organization.update(running=True, kind="move", phase="checking", done=0,
                                     total=len(entry["plan"]["_snapshots"]), moved=0,
                                     message="Checking every file before moving any files.", lastResult=None)
            self.organization_thread = threading.Thread(target=self._run_organization,
                args=(self.organizer, "move", operation_id, entry["plan"]), daemon=True, name="file-organization")
            try:
                self.organization_thread.start()
            except RuntimeError as error:
                self.organization.update(running=False, phase="error", message="File organization could not start.")
                raise ReviewError("File organization could not start. Prepare a new preview.", 409) from error
            return {"ok": True, "operationId": operation_id, "organization": self._organization_state()}

    def organize_undo(self, operation_id):
        with self.lock:
            self._require_idle()
            if self.catalog_only or (self.roots and self.roots[0]['protected']):
                raise ReviewError('The primary folder is protected or offline. Its files cannot be changed.', 409)
            if (not isinstance(operation_id, str) or self.organizer is None or
                    not any(item["id"] == operation_id and item["undoAvailable"]
                            for item in self.organizer.history())):
                raise ReviewError("This organization operation is not available to undo.", 409)
            self._invalidate_bulk_plans()
            self.organization_recompare = False
            self.organization.update(running=True, kind="undo", phase="checking", done=0, total=0,
                                     moved=0, message="Checking organized files before restoring their locations.",
                                     lastResult=None)
            self.organization_thread = threading.Thread(target=self._run_organization,
                args=(self.organizer, "undo", operation_id), daemon=True, name="file-organization")
            try:
                self.organization_thread.start()
            except RuntimeError as error:
                self.organization.update(running=False, phase="error", message="Organization undo could not start.")
                raise ReviewError("Organization undo could not start. Try again.", 409) from error
            return {"ok": True, "operationId": operation_id, "organization": self._organization_state()}

    def _bulk_snapshot(self, groups, candidate_ids=None):
        """Bind a confirmation to the exact in-memory review it describes."""
        members = dict.fromkeys(member for group in groups for member in group["photos"])
        try:
            photos = tuple((member, self.result.photos[member].file, self.result.photos[member].path,
                            self.result.photos[member].size, self.result.photos[member].signature,
                            self.result.photos[member].sha256, self.result.photos[member].width,
                            self.result.photos[member].height,
                            tuple(getattr(self.result.photos[member], "imageCategories", ()))) for member in members)
        except KeyError as error:
            raise ReviewError("This comparison changed. Refresh the review.", 409) from error
        return (self.review_revision, self.root, tuple((group["id"], tuple(group["photos"]))
                                                     for group in groups), photos, candidate_ids)

    def bulk_delete_plan(self, group_ids, keepers=None, candidate_ids=None) -> dict:
        """Describe removals without reading or changing any original file."""
        with self.lock:
            self._require_idle()
            if (not isinstance(group_ids, list) or not group_ids or
                    any(not isinstance(group_id, str) or not group_id for group_id in group_ids)):
                raise ReviewError("Choose the current comparison groups to review.", 409)
            if len(set(group_ids)) != len(group_ids):
                raise ReviewError("Each comparison group must appear only once.", 409)
            if keepers is None:
                keepers = {}
            if not isinstance(keepers, dict) or any(group_id not in group_ids for group_id in keepers):
                raise ReviewError("Keeper choices must belong to the selected groups.", 409)
            by_id = {group["id"]: group for group in self.result.groups}
            groups = []
            skipped_archive_groups = 0
            keeper_by_group = {}
            for group_id in group_ids:
                group = by_id.get(group_id)
                if group is None or len(group["photos"]) < 2:
                    raise ReviewError("A comparison group changed. Refresh the review.", 409)
                if any(member not in self.result.photos for member in group["photos"]):
                    raise ReviewError("This comparison changed. Refresh the review.", 409)
                if any(getattr(self.result.photos[member], "sourceKind", "local") != "local"
                       for member in group["photos"]):
                    skipped_archive_groups += 1
                    continue
                keeper_id = keepers.get(group_id)
                if group_id in keepers:
                    if not isinstance(keeper_id, str) or keeper_id not in group["photos"]:
                        raise ReviewError("Choose a file to keep from the same comparison group.", 409)
                else:
                    keeper_id = self.keeper_choices.get(group_id) or max(group["photos"], key=lambda member: (
                        self.result.photos[member].width * self.result.photos[member].height,
                        self.result.photos[member].size))
                groups.append(group)
                keeper_by_group[group_id] = keeper_id

            # Image filters can match only one member of a duplicate group.
            # The explicit candidate list limits what can move while allowing a
            # nonmatching original to remain visible as the protected keeper.
            candidates = None
            if candidate_ids is not None:
                if (not isinstance(candidate_ids, list) or
                        any(not isinstance(member, str) or not member for member in candidate_ids)):
                    raise ReviewError("Filtered copies must be a list of current file IDs.", 409)
                if len(set(candidate_ids)) != len(candidate_ids):
                    raise ReviewError("Each filtered copy must appear only once.", 409)
                selected_members = {member for group in groups for member in group["photos"]}
                all_selected_members = {member for group_id in group_ids for member in by_id[group_id]["photos"]}
                if any(member not in all_selected_members for member in candidate_ids):
                    raise ReviewError("Filtered copies must belong to the selected comparison groups.", 409)
                candidates = tuple(member for member in candidate_ids if member in selected_members)
            eligible = None if candidates is None else set(candidates)
            if eligible is not None:
                for group in groups:
                    if group["id"] in keepers:
                        continue
                    outside_filter = [member for member in group["photos"] if member not in eligible]
                    if outside_filter:
                        keeper_by_group[group["id"]] = max(outside_filter, key=lambda member: (
                            self.result.photos[member].width * self.result.photos[member].height,
                            self.result.photos[member].size))

            # Similarity relationships can overlap. Protect every selected keeper
            # across the entire batch, even when another group calls it a copy.
            protected = dict.fromkeys(keeper_by_group.values())
            for group in groups:
                for member in group['photos']:
                    photo = self.result.photos[member]
                    if getattr(photo, 'protected', False) or getattr(photo, 'reference', False) or getattr(photo, 'offline', False):
                        protected[member] = None
            # An offline copy cannot serve as the only verified keeper of a live file.
            for group in groups:
                keeper = self.result.photos[keeper_by_group[group['id']]]
                if getattr(keeper, 'offline', False):
                    for member in group['photos']:
                        protected[member] = None
            removals = {}
            for group in groups:
                for member in group["photos"]:
                    if (member not in protected and (eligible is None or member in eligible)
                            and getattr(self.result.photos[member], "sourceKind", "local") == "local"):
                        removals.setdefault(member, keeper_by_group[group["id"]])
            files = []
            for member, keeper_id in removals.items():
                photo, keeper = self.result.photos[member], self.result.photos[keeper_id]
                files.append({"id": member, "name": photo.name, "path": photo.path, "size": photo.size,
                              "keepName": keeper.name, "keepPath": keeper.path})
            kept = [{"id": member, "name": self.result.photos[member].name,
                     "path": self.result.photos[member].path, "size": self.result.photos[member].size}
                    for member in protected]
            now = time.monotonic()
            for plan_id in list(self.bulk_plans):
                if self.bulk_plans[plan_id]["expires"] <= now:
                    del self.bulk_plans[plan_id]
            plan_id = secrets.token_urlsafe(32)
            self.bulk_plans[plan_id] = {"expires": now + BULK_PLAN_SECONDS, "store": self.store,
                                       "snapshot": self._bulk_snapshot(groups, candidates), "groupIds": tuple(group["id"] for group in groups),
                                       "candidateIds": candidates,
                                       "removals": removals, "protected": tuple(protected)}
            while len(self.bulk_plans) > BULK_PLAN_LIMIT:
                self.bulk_plans.popitem(last=False)
            response = {"planId": plan_id, "count": len(files), "totalBytes": sum(file["size"] for file in files),
                        "groupCount": len(groups), "keepCount": len(kept), "files": files, "kept": kept}
            if skipped_archive_groups:
                response["skippedArchiveGroups"] = skipped_archive_groups
            return response

    def bulk_delete(self, plan_id, confirmed=False) -> dict:
        with self.lock:
            self._require_idle()
            if confirmed is not True:
                raise ReviewError("Review the file list and confirm before moving copies to trash.", 409)
            if not isinstance(plan_id, str) or not plan_id:
                raise ReviewError("Prepare a new removal plan before confirming.", 409)
            plan = self.bulk_plans.get(plan_id)
            if plan is None or plan["expires"] <= time.monotonic() or plan["store"] is not self.store:
                self.bulk_plans.pop(plan_id, None)
                raise ReviewError("This removal plan expired or the review changed. Review a new plan.", 409)
            by_id = {group["id"]: group for group in self.result.groups}
            groups = [by_id[group_id] for group_id in plan["groupIds"] if group_id in by_id]
            if (len(groups) != len(plan["groupIds"]) or
                    self._bulk_snapshot(groups, plan["candidateIds"]) != plan["snapshot"] or
                    (plan["candidateIds"] is not None and
                     any(member not in plan["candidateIds"] for member in plan["removals"]))):
                del self.bulk_plans[plan_id]
                raise ReviewError("This comparison changed. Review a new removal plan.", 409)
            if not plan["removals"]:
                raise ReviewError("There are no removable copies after protecting the selected keepers.", 409)

            # Complete the preflight before the first move, so a missing or
            # edited copy/keeper cannot produce a partially stale confirmation.
            try:
                for member in dict.fromkeys(member for group in groups for member in group["photos"]):
                    photo = self.result.photos[member]
                    if not getattr(photo, 'offline', False):
                        self.store.verify_photo(photo)
            except (StorageError, OSError) as error:
                del self.bulk_plans[plan_id]
                raise ReviewError(str(error), 409) from error

            # A confirmation is single-use. Mutations remain serialized with
            # scanning, individual moves, and restores under the review lock.
            self._invalidate_bulk_plans()
            deleted, failures = [], []
            for member, keeper_id in plan["removals"].items():
                photo = self.result.photos[member]
                keeper = self.result.photos[keeper_id]
                try:
                    record = self.store.move(photo, keeper)
                except (StorageError, OSError) as error:
                    failures.append({"id": member, "name": photo.name, "path": photo.path, "error": str(error)})
                    continue
                deleted.append(dict(record, sourceId=member))
                del self.result.photos[member]
            removed_ids = {record["sourceId"] for record in deleted}
            moved_sources = {photo[1] for photo in plan["snapshot"][3] if photo[0] in removed_ids}
            archive_children = {photo.id for photo in self.result.photos.values()
                                if getattr(photo, "sourceKind", "local") == "archive" and photo.file in moved_sources}
            for child_id in archive_children:
                del self.result.photos[child_id]
            removed_ids.update(archive_children)
            self.result.groups = [dict(group, photos=remaining) for group in self.result.groups
                                  if len(remaining := [member for member in group["photos"]
                                                       if member not in removed_ids]) > 1]
            self._refresh_stats()
            self.preview_cache.clear()
            state = self.state()
            return {"ok": True, "deletedCount": len(deleted), "deleted": deleted, "failures": failures,
                    "trashCount": state["trashCount"], "state": state}

    def delete(self, photo_id, group_id, keeper_id) -> dict:
        with self.lock:
            self._require_idle()
            group = next((item for item in self.result.groups if item["id"] == group_id), None)
            if group is None or photo_id not in group["photos"] or keeper_id not in group["photos"]:
                raise ReviewError("Choose both files from the same current comparison group.", 409)
            if photo_id == keeper_id or len(group["photos"]) < 2:
                raise ReviewError("At least one different file must be kept.", 409)
            photo = self.result.photos.get(photo_id)
            keeper = self.result.photos.get(keeper_id)
            if photo is None or keeper is None:
                raise ReviewError("This comparison changed. Refresh the review.", 409)
            if getattr(photo, 'protected', False) or getattr(photo, 'reference', False) or getattr(photo, 'offline', False):
                raise ReviewError('Protected and offline catalog files cannot be removed.', 409)
            if getattr(keeper, 'offline', False):
                raise ReviewError('Reconnect and scan the keeper drive before removing another copy.', 409)
            if getattr(photo, "sourceKind", "local") != "local":
                raise ReviewError("Archive entries are read-only. Extract this member before moving or removing it.", 409)
            if getattr(keeper, "sourceKind", "local") != "local":
                raise ReviewError("Keep a file outside the archive before removing a local copy.", 409)
            deleted = self.store.move(photo, keeper)
            self._invalidate_bulk_plans()
            del self.result.photos[photo_id]
            removed_ids = {photo_id}
            for child in list(self.result.photos.values()):
                if getattr(child, "sourceKind", "local") == "archive" and child.file == photo.file:
                    removed_ids.add(child.id)
                    del self.result.photos[child.id]
            self.result.groups = [dict(item, photos=[member for member in item["photos"] if member not in removed_ids])
                                  for item in self.result.groups if len([member for member in item["photos"] if member not in removed_ids]) > 1]
            self._refresh_stats()
            self.preview_cache.clear()
            return {"ok": True, "deleted": deleted, "trashId": deleted["id"], "trashCount": len(self.store.items()), "state": self.state()}

    def restore(self, record_id) -> dict:
        with self.lock:
            self._require_idle()
            restored = self.store.restore(record_id)
            self._invalidate_bulk_plans()
            # Rebuild relationships from this review in memory. Restoring one
            # file must never start a new directory scan.
            try:
                source_root = Path(restored.get('sourceRoot', self.root))
                photo = enrich_photo(_inspect(Path(restored["path"]), source_root), Path(restored["path"]), self.scan_options)
                photo.sourceRoot = str(source_root)
                self.result.photos[photo.id] = photo
                self.result.groups = compare_photos(self.result.photos, self.threshold, options=self.scan_options)
                self._refresh_stats()
                from archive_scan import is_archive
                if self.scan_options.get("archives") and is_archive(photo.file):
                    self.result.errors.append({"name": photo.path,
                        "message": "Archive restored. Scan the folder again to include its members in the current library."})
            except Exception as error:
                self.result.errors.append({"name": restored["name"], "message": "Restored successfully; preview could not be refreshed: " + str(error)})
            self.preview_cache.clear()
            return {"ok": True, "restored": restored, "state": self.state()}

    def trash(self) -> dict:
        with self.lock:
            return {"items": self.store.items() if self.store else []}

    def _photo_path(self, photo_id) -> tuple[Path, str]:
        with self.lock:
            if self.organization["running"]:
                raise ReviewError("Wait for file organization to finish before previewing files.", 409)
            photo = self.result.photos.get(photo_id)
            if photo is None:
                raise ReviewError("This file is not in the current scan.", 404)
            if getattr(photo, 'offline', False):
                raise ReviewError('Offline catalog entry. Reconnect the drive and scan it to preview this file.', 409)
            if self.store is None:
                raise ReviewError("The media folder is unavailable.", 409)
            relative = photo.archiveContainer if getattr(photo, "sourceKind", "local") == "archive" else photo.path
            store = self.store.for_photo(photo) if isinstance(self.store, MultiRootTrash) else self.store
            path = store._original(relative)
            if not path.is_file():
                raise ReviewError("The file no longer exists. Scan again.", 404)
            if file_signature(path) != photo.signature:
                raise ReviewError("The file changed after scanning. Scan again before comparing it.", 409)
            return path, photo.sha256

    @contextmanager
    def _materialized(self, photo_id):
        path, digest = self._photo_path(photo_id)
        with self.lock:
            photo = self.result.photos[photo_id]
            archive = getattr(photo, "sourceKind", "local") == "archive"
            member = getattr(photo, "archiveMember", "")
            signature = photo.signature
        if archive:
            from archive_scan import materialize_member
            with materialize_member(path, member, expected_signature=signature) as extracted:
                yield extracted, digest
            self._photo_path(photo_id)
        else:
            yield path, digest

    def photo(self, photo_id, size="preview", trashed=False) -> tuple[bytes, str]:
        if size not in {"thumb", "preview", "full"}:
            raise ReviewError("Preview size must be thumb, preview, or full.")
        if trashed:
            with self.lock:
                if self.store is None:
                    raise ReviewError("Photo recovery is unavailable.", 404)
                path = self.store.preview_path(photo_id)
                signature = str(file_signature(path))
        else:
            with self._materialized(photo_id) as (path, signature):
                return self._render_photo(path, signature, size)
        return self._render_photo(path, signature, size)

    def _render_photo(self, path, signature, size):
        media_type, _, media_format = media_metadata(path)
        before = file_signature(path)
        key = (str(path), signature, size)
        with self.lock:
            if key in self.preview_cache:
                result = self.preview_cache.pop(key)
                self.preview_cache[key] = result
                return result
        try:
            if media_type == "video":
                if not self.decoder_slots.acquire(timeout=20):
                    raise ReviewError("Video thumbnail preparation is busy. Playback and identical-file review remain available.", 415)
                try:
                    preview = run_ffmpeg(["-protocol_whitelist", "file,pipe", "-i", str(path),
                                          "-map", "0:v:0", "-frames:v", "1",
                                          "-vf", "scale=640:420:force_original_aspect_ratio=decrease",
                                          "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], timeout=20)
                finally:
                    self.decoder_slots.release()
                with Image.open(io.BytesIO(preview.stdout)) as frame:
                    frame.load()
                    image = rgb_image(frame.copy())
            elif media_type == "image":
                image = rgb_image(oriented_image(path))
            else:
                raise ValueError("No still preview for this file type.")
            if size != "full":
                limit = 360 if size == "thumb" else 1800
                image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, "JPEG", quality=90, optimize=True)
            result = (output.getvalue(), "image/jpeg")
        except Exception:
            title = html.escape(path.name[:100])
            label = {"image": "Image preview unavailable", "video": "Video thumbnail unavailable",
                     "audio": "Audio file", "other": "File preview unavailable"}.get(media_type, "Preview unavailable")
            result = ((f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="420" viewBox="0 0 640 420">'
                       '<rect width="640" height="420" fill="#f1f3f6"/><text x="320" y="188" text-anchor="middle" '
                       f'font-family="sans-serif" font-size="21" fill="#596374">{label}</text>'
                       f'<text x="320" y="214" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#596374">{html.escape(media_format)}</text>'
                       f'<text x="320" y="248" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#596374">{title}</text></svg>').encode("utf-8"), "image/svg+xml")
        if file_signature(path) != before:
            raise ReviewError("The file changed while preparing its preview. Scan again.", 409)
        with self.lock:
            self.preview_cache[key] = result
            while len(self.preview_cache) > 64:
                self.preview_cache.popitem(last=False)
        return result

    def media(self, photo_id) -> tuple[Path, str, tuple]:
        """Prepare a local playback copy only when its play control is used."""
        with self._materialized(photo_id) as (path, digest):
            return self._prepare_media(photo_id, path, digest)

    def _prepare_media(self, photo_id, path, digest):
        with self.lock:
            photo = self.result.photos[photo_id]
            kind, mime = photo.mediaType, photo.mime
            source_signature = file_signature(path)
        if kind not in {"video", "audio"}:
            raise ReviewError("This file does not have a video or audio playback preview.", 415)
        if ffmpeg_binary() is None:
            native = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".wave": "audio/wav",
                      ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
                      ".flac": "audio/flac", ".mp4": "video/mp4", ".m4v": "video/mp4",
                      ".webm": "video/webm", ".ogv": "video/ogg", ".m4a": "audio/mp4"}
            native_mime = native.get(path.suffix.lower())
            if native_mime:
                if getattr(photo, "sourceKind", "local") == "archive":
                    with self.media_lock:
                        if self.media_directory is None:
                            self.media_directory = tempfile.TemporaryDirectory(prefix="media-review-preview-")
                        destination = Path(self.media_directory.name) / (digest + path.suffix.lower())
                        if not destination.exists():
                            shutil.copyfile(path, destination)
                        return destination, native_mime, file_signature(destination)
                return path, native_mime, source_signature
            raise ReviewError("This playback format needs the bundled FFmpeg decoder. Identical file detection still works.", 415)
        key = digest + "-" + kind
        # One conversion at a time prevents several preview cards from loading
        # large files concurrently. Duplicate bytes reuse the same preview.
        with self.media_lock:
            self._photo_path(photo_id)
            cached = self.media_cache.get(key)
            if cached and cached.is_file():
                self.media_cache.move_to_end(key)
                return cached, "video/mp4" if kind == "video" else "audio/mpeg", file_signature(cached)
            if self.media_directory is None:
                self.media_directory = tempfile.TemporaryDirectory(prefix="media-review-preview-")
            suffix = ".mp4" if kind == "video" else ".mp3"
            destination = Path(self.media_directory.name) / (key + suffix)
            temporary = destination.with_name(destination.stem + "-preparing" + suffix)
            common = ["-protocol_whitelist", "file,pipe", "-i", str(path), "-t", str(PREVIEW_SECONDS), "-sn", "-dn"]
            if kind == "video":
                arguments = common + [
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-threads", "2", "-movflags", "+faststart", "-y", str(temporary)]
            else:
                arguments = common + ["-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k",
                                      "-ac", "2", "-threads", "2", "-y", str(temporary)]
            try:
                if not self.decoder_slots.acquire(timeout=20):
                    raise ReviewError("Preview preparation is busy. Try playing this file again in a moment.", 415)
                try:
                    run_ffmpeg(arguments, timeout=90)
                finally:
                    self.decoder_slots.release()
                self._photo_path(photo_id)
                if file_signature(path) != source_signature:
                    raise ReviewError("The file changed while preparing its playback preview. Scan again.", 409)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise ReviewError("The decoder could not produce a playback preview for this file.", 415)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self.media_cache[key] = destination
            # Bound cache growth; previews are disposable and never originals.
            while len(self.media_cache) > 12:
                _, old = self.media_cache.popitem(last=False)
                try:
                    old.unlink()
                except OSError:
                    pass
            return destination, "video/mp4" if kind == "video" else "audio/mpeg", file_signature(destination)

    def close(self):
        with self.lock:
            self.closing = True
            worker = self.organization_thread
            scan_worker = self.scan_thread
            if self.scan_stop_event is not None:
                self.scan_stop_event.set()
            if self.scan_resume_event is not None:
                self.scan_resume_event.set()
        if scan_worker is not None and scan_worker is not threading.current_thread() and scan_worker.is_alive():
            scan_worker.join()  # The current native decode finishes; no next file is read.
        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join()  # Finish durable moves before normal application shutdown.
        with self.lock:
            self.scanning = False
            for photo in self.result.photos.values():
                photo.ocrText = ""
                photo.visualEmbedding = None
                if hasattr(photo, "audioFingerprint"):
                    photo.audioFingerprint = ()
        self.cloud.close()
        visual_search.clear_caches()
        clear_ocr_cache()
        with self.media_lock:
            if self.media_directory is not None:
                self.media_directory.cleanup()
                self.media_directory = None
                self.media_cache.clear()

    def pick_folder(self) -> dict:
        with self.lock:
            if self.closing or self.organization["running"]:
                raise ReviewError("Wait for file organization to finish before choosing another folder.", 409)
            if self.scanning:
                raise ReviewError("Wait for the scan to finish before choosing another folder.", 409)
        try:
            import tkinter
            from tkinter import filedialog
            window = tkinter.Tk()
            window.withdraw()
            window.attributes("-topmost", True)
            try:
                selected = filedialog.askdirectory(title="Choose your media folder", initialdir=str(self.root or self.picker_root), parent=window, mustexist=True)
            finally:
                window.destroy()
            return {"path": selected, "cancelled": not bool(selected)}
        except Exception as error:
            raise ReviewError(f"Folder picker is unavailable. Paste a folder path instead. {error}") from error


class ReviewHandler(BaseHTTPRequestHandler):
    server_version = f"MediaReview/{__version__}"

    @property
    def app(self):
        return self.server.app

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check_host(self):
        if self.headers.get("Host", "").lower() not in self._allowed_hosts():
            raise ReviewError("This app only accepts requests to its local address.", 403)

    def _check_mutation(self):
        self._check_host()
        host = self.headers.get("Host", "").lower()
        if self.headers.get("Origin", "") != "http://" + host:
            raise ReviewError("Open this app in its local review window before making changes.", 403)
        token = self.headers.get("X-Review-Token", "")
        if not secrets.compare_digest(token, self.app.token):
            raise ReviewError("Your review session token is missing or expired. Refresh the page.", 403)
        if self.headers.get_content_type() != "application/json":
            raise ReviewError("Changes must be submitted as JSON.", 415)

    def _headers(self, status, length, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status, body, content_type):
        self._headers(status, len(body), content_type)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _stream_media(self, photo_id):
        path, mime, signature = self.app.media(photo_id)
        if is_reparse(path):
            raise ReviewError("The playback file became a link. Scan again.", 409)
        with path.open("rb") as source:
            info = os.fstat(source.fileno())
            opened = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if opened != signature:
                raise ReviewError("The playback file changed before it could be opened. Scan again.", 409)
            size = info.st_size
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                valid = match is not None and size > 0
                if valid:
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                        valid = start < size and start <= end
                    elif last and int(last) > 0:
                        start = max(0, size - int(last))
                    else:
                        valid = False
                if not valid:
                    self._headers(416, 0, mime, {"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"})
                    return
                status = 206
            length = max(0, end - start + 1)
            headers = {"Accept-Ranges": "bytes"}
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            self._headers(status, length, mime, headers)
            if self.command == "HEAD":
                return
            source.seek(start)
            remaining = length
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _handle_error(self, error):
        if isinstance(error, ReviewError):
            self._json({"ok": False, "error": str(error)}, error.status)
        elif isinstance(error, (StorageError, ValueError, OSError)):
            self._json({"ok": False, "error": str(error)}, 409 if isinstance(error, StorageError) else 400)
        else:
            traceback.print_exc()
            self._json({"ok": False, "error": "The request could not be completed. Your remaining photos have not been moved."}, 500)

    def do_GET(self):
        try:
            self._check_host()
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/state":
                self._json(self.app.state())
            elif path == "/api/cloud/state":
                self._json(self.app.cloud.state())
            elif path == '/api/cloud/history':
                self._json(self.app.cloud.action_history())
            elif path == '/api/scan/interrupted':
                self._json(self.app.interrupted_scan())
            elif path == '/api/report.csv':
                self._send(200, self.app.tools.report_csv(), 'text/csv; charset=utf-8')
            elif path == "/api/trash":
                self._json(self.app.trash())
            elif path.startswith("/api/media/"):
                self._stream_media(path.removeprefix("/api/media/"))
            elif path.startswith("/api/photo/"):
                photo_id = path.removeprefix("/api/photo/")
                size = parse_qs(parsed.query).get("size", ["preview"])[0]
                body, content_type = self.app.photo(photo_id, size)
                self._send(200, body, content_type)
            elif path.startswith("/api/trash/") and path.endswith("/preview"):
                photo_id = path.removeprefix("/api/trash/").removesuffix("/preview")
                body, content_type = self.app.photo(photo_id, "thumb", trashed=True)
                self._send(200, body, content_type)
            else:
                static_files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                                "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                                "/static/app.js": ("app.js", "text/javascript; charset=utf-8"), "/static/styles.css": ("styles.css", "text/css; charset=utf-8")}
                for name in ("advanced.js", "cloud.js", "advanced.css", "cloud.css", 'workspace.js', 'workspace.css', 'file_tools.js', 'file_tools.css'):
                    mime = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                    static_files['/' + name] = (name, mime)
                    static_files['/static/' + name] = (name, mime)
                if path not in static_files:
                    raise ReviewError("This page was not found.", 404)
                name, content_type = static_files[path]
                filename = APP_DIRECTORY / "static" / name
                if not filename.is_file():
                    raise ReviewError("The app interface is missing. Reinstall or extract the complete app folder.", 404)
                self._send(200, filename.read_bytes(), content_type)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._handle_error(error)

    def do_POST(self):
        try:
            self._check_mutation()
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ReviewError("Invalid request length.") from error
            maximum_length = 1024 * 1024 if path in {"/api/bulk-delete-plan", '/api/tools/plan'} else 262144
            if not 0 < length <= maximum_length:
                raise ReviewError("The request is empty or too large.", 413)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReviewError("The request contains invalid JSON.") from error
            if not isinstance(data, dict):
                raise ReviewError("The request must be a JSON object.")
            if path == "/api/scan":
                self._json(self.app.start_scan(data.get("path"), data.get("recursive", True), data.get("threshold", 94),
                                               data.get("includeAllFiles", True), data.get("options"),
                                               data.get('roots'), data.get('catalogIds')))
            elif path == '/api/scan/interrupted-resume':
                self._json(self.app.resume_interrupted())
            elif path == "/api/scan/stop":
                self._json(self.app.stop_scan())
            elif path == "/api/scan/pause":
                self._json(self.app.pause_scan())
            elif path == "/api/scan/resume":
                self._json(self.app.resume_scan())
            elif path == "/api/sessions/list":
                self._json(self.app.sessions())
            elif path == "/api/sessions/save":
                self._json(self.app.save_session(data.get("name")))
            elif path == "/api/sessions/load":
                self._json(self.app.load_session(data.get("id"), data.get('offline', False)))
            elif path == '/api/tools/plan':
                self._json(self.app.tools.plan(data.get('kind'), **{k:v for k,v in data.items() if k != 'kind'}))
            elif path == '/api/tools/execute':
                self._json(self.app.tools.execute(data.get('planId'), data.get('confirmed', False)))
            elif path == '/api/tools/history':
                self._json({'ok': True, 'history': self.app.tools.history()})
            elif path == '/api/tools/undo':
                self._json(self.app.tools.undo(data.get('operationId'), data.get('confirmed', False)))
            elif path == '/api/tools/keepers':
                with self.app.lock:
                    choices = self.app.tools.choose_keepers(data.get('rule', 'quality'), data.get('preferredFolder', ''), data.get('formats'))
                    self.app._invalidate_bulk_plans()
                    self.app.keeper_choices = choices
                    self._json({'ok': True, 'keepers': choices, 'state': self.app.state()})
            elif path == "/api/cloud/connect":
                self._json(self.app.cloud.connect(data.get("provider"), data.get("label", ""),
                                                  data.get("clientId", ""), data.get("clientSecret", "")))
            elif path == "/api/cloud/answer":
                self._json(self.app.cloud.answer(data.get("answer")))
            elif path == "/api/cloud/cancel":
                self._json(self.app.cloud.cancel_connection())
            elif path == "/api/cloud/folders":
                self._json(self.app.cloud.folders(data.get("accountId"), data.get("path", "")))
            elif path == "/api/cloud/scan":
                self._json(self.app.cloud.start_scan(data.get("accountId"), data.get("path", ""),
                                                     data.get("recursive", True)))
            elif path == "/api/cloud/stop":
                self._json(self.app.cloud.stop_scan())
            elif path == "/api/cloud/open":
                self._json(self.app.cloud.open_file(data.get("id")))
            elif path == '/api/cloud/action-plan':
                self._json(self.app.cloud.action_plan(data.get('ids'), data.get('action', 'trash'), data.get('destination', '')))
            elif path == '/api/cloud/action-execute':
                self._json(self.app.cloud.execute_action(data.get('planId'), data.get('confirmed', False)))
            elif path == '/api/cloud/action-restore':
                self._json(self.app.cloud.restore_action(data.get('operationId'), data.get('confirmed', False)))
            elif path == "/api/delete":
                self._json(self.app.delete(data.get("id"), data.get("groupId"), data.get("keepId")))
            elif path == "/api/bulk-delete-plan":
                if "candidateIds" in data and not isinstance(data["candidateIds"], list):
                    raise ReviewError("Filtered copies must be a list of current file IDs.", 409)
                self._json(self.app.bulk_delete_plan(data.get("groupIds"), data.get("keepers"), data.get("candidateIds")))
            elif path == "/api/bulk-delete":
                self._json(self.app.bulk_delete(data.get("planId"), data.get("confirmed", False)))
            elif path == "/api/restore":
                self._json(self.app.restore(data.get("id")))
            elif path == "/api/pick-folder":
                self._json(self.app.pick_folder())
            elif path == "/api/search":
                self._json(self.app.search(data.get("query"), data.get("mode", "all"),
                                           data.get("mediaType", "all"), data.get("category", "all"),
                                           data.get("offset", 0), data.get("limit", 50)))
            elif path == "/api/show-file":
                self._json(self.app.show_file(data.get("id")))
            elif path == "/api/organize/plan":
                self._json(self.app.organize_plan(data.get("mode"), data.get("yearBasis", "taken")))
            elif path == "/api/organize/execute":
                self._json(self.app.organize_execute(data.get("planId")))
            elif path == "/api/organize/undo":
                self._json(self.app.organize_undo(data.get("operationId")))
            else:
                raise ReviewError("This action was not found.", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._handle_error(error)

    def log_message(self, format, *args):
        # Photo names and paths do not need to be written to server logs.
        pass

    do_HEAD = do_GET


def create_server(root=None, port=8765, app=None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), ReviewHandler)
    server.daemon_threads = True
    server.app = app or ReviewApp(root)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Privately compare duplicate media in your browser.")
    parser.add_argument("--root", default=str(default_root()), help="Initial folder picker location; scanning always requires the Scan button")
    parser.add_argument("--port", default=8765, type=int, help="Local port (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args(argv)
    server = create_server(args.root, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Photo Review is ready at {url}")
    print("Choose a folder, then click Scan. Files stay on your computer; removed files are recoverable in .photo-review-trash.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.close()


if __name__ == "__main__":
    main()
