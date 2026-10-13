"""Local-only duplicate-media review server. Files never leave this computer."""
from __future__ import annotations

import argparse
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
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

APP_DIRECTORY = Path(__file__).resolve().parent
VENDOR = APP_DIRECTORY / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

from PIL import Image
from scanner import (ScanResult, _inspect, calculate_stats, compare_photos, file_signature, is_reparse,
                     media_metadata, oriented_image, rgb_image, scan_folder)
from storage import StorageError, TrashStore
from version import __version__
from image_filters import clear_ocr_cache
import visual_search


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
    def __init__(self, root=None):
        # Remember only a picker hint: opening the app must not read or scan a
        # previous folder, including its recovery journal.
        self.picker_root = Path(os.path.abspath(str(root or default_root())))
        self.root = None
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
        self.media_lock = threading.Lock()
        self.decoder_slots = threading.BoundedSemaphore(2)
        self.media_directory = None
        self.media_cache = OrderedDict()
        self.review_revision = 0
        self.bulk_plans = OrderedDict()

    def state(self) -> dict:
        with self.lock:
            try:
                trash_count = len(self.store.items()) if self.store else 0
            except StorageError:
                trash_count = 0
            return {"root": str(self.root) if self.root else "", "hasScanned": self.has_scanned,
                    "scanning": self.scanning, "progress": dict(self.progress), "previewSeconds": PREVIEW_SECONDS,
                    "photos": [photo.public() for photo in self.result.photos.values()],
                    "groups": [dict(group, photos=list(group["photos"])) for group in self.result.groups],
                    "errors": list(self.result.errors), "stats": dict(self.result.stats),
                    "trashCount": trash_count, "token": self.token, "revision": self.review_revision}

    def _progress(self, done, total, phase):
        with self.lock:
            self.progress = {"done": done, "total": total, "phase": phase}

    def start_scan(self, path=None, recursive=True, threshold=94, include_all_files=True) -> dict:
        if not isinstance(recursive, bool):
            raise ReviewError("Include subfolders must be true or false.")
        if not isinstance(include_all_files, bool):
            raise ReviewError("Include all file types must be true or false.")
        try:
            threshold = float(threshold)
        except (TypeError, ValueError) as error:
            raise ReviewError("Choose a similarity threshold between 85 and 99.") from error
        if not 85 <= threshold <= 99:
            raise ReviewError("Choose a similarity threshold between 85 and 99.")
        selected = path or self.root
        if not selected or not str(selected).strip():
            raise ReviewError("Choose a folder, then click Scan.")
        target = Path(os.path.abspath(os.path.expanduser(str(selected).strip())))
        if not target.is_dir():
            raise ReviewError("The selected media folder does not exist.")
        if is_reparse(target):
            raise ReviewError("Choose a normal folder rather than a linked folder or junction.")
        with self.lock:
            if self.scanning:
                raise ReviewError("A scan is already running. Wait for it to finish.", 409)
            # Journal validation happens before replacing the current review.
            try:
                store = TrashStore(target)
            except StorageError as error:
                raise ReviewError(str(error), 409) from error
            self._invalidate_bulk_plans()
            self.root, self.store = target, store
            self.picker_root = target
            self.recursive, self.threshold, self.include_all_files = recursive, threshold, include_all_files
            self.has_scanned = True
            self.scanning = True
            self.result = ScanResult(stats={"total": 0, "exactGroups": 0, "similarGroups": 0, "duplicateBytes": 0})
            self.progress = {"done": 0, "total": 0, "phase": "discover"}
            self.preview_cache.clear()
            visual_search.clear_caches()
            self.scan_thread = threading.Thread(target=self._scan, args=(target, recursive, threshold, include_all_files), daemon=True, name="media-scan")
            self.scan_thread.start()
            return self.state()

    def _scan(self, target, recursive, threshold, include_all_files):
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
            result = scan_folder(target, recursive, threshold, self._progress, exclude_paths=excluded,
                                 include_all_files=include_all_files)
            with self.lock:
                self.result = result
                self.progress["phase"] = "complete"
        except Exception as error:
            with self.lock:
                self.result.errors.append({"name": str(target), "message": str(error)})
                self.progress["phase"] = "error"
        finally:
            clear_ocr_cache()
            with self.lock:
                self.scanning = False

    def _require_idle(self):
        if self.scanning:
            raise ReviewError("Wait for the scan to finish before searching, reviewing, or restoring files.", 409)
        if self.store is None:
            raise ReviewError("Choose a folder and click Scan before searching, reviewing, or restoring files.", 409)

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
            keeper_by_group = {}
            for group_id in group_ids:
                group = by_id.get(group_id)
                if group is None or len(group["photos"]) < 2:
                    raise ReviewError("A comparison group changed. Refresh the review.", 409)
                if any(member not in self.result.photos for member in group["photos"]):
                    raise ReviewError("This comparison changed. Refresh the review.", 409)
                keeper_id = keepers.get(group_id)
                if group_id in keepers:
                    if not isinstance(keeper_id, str) or keeper_id not in group["photos"]:
                        raise ReviewError("Choose a file to keep from the same comparison group.", 409)
                else:
                    keeper_id = max(group["photos"], key=lambda member: (
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
                if any(member not in selected_members for member in candidate_ids):
                    raise ReviewError("Filtered copies must belong to the selected comparison groups.", 409)
                candidates = tuple(candidate_ids)
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
            removals = {}
            for group in groups:
                for member in group["photos"]:
                    if member not in protected and (eligible is None or member in eligible):
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
                                       "snapshot": self._bulk_snapshot(groups, candidates), "groupIds": tuple(group_ids),
                                       "candidateIds": candidates,
                                       "removals": removals, "protected": tuple(protected)}
            while len(self.bulk_plans) > BULK_PLAN_LIMIT:
                self.bulk_plans.popitem(last=False)
            return {"planId": plan_id, "count": len(files), "totalBytes": sum(file["size"] for file in files),
                    "groupCount": len(groups), "keepCount": len(kept), "files": files, "kept": kept}

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
                    self.store.verify_photo(self.result.photos[member])
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
            self.result.groups = [dict(group, photos=remaining) for group in self.result.groups
                                  if len(remaining := [member for member in group["photos"]
                                                       if member not in removed_ids]) > 1]
            self.result.stats = calculate_stats(self.result.photos, self.result.groups)
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
            deleted = self.store.move(photo, keeper)
            self._invalidate_bulk_plans()
            del self.result.photos[photo_id]
            self.result.groups = [dict(item, photos=[member for member in item["photos"] if member != photo_id])
                                  for item in self.result.groups if len([member for member in item["photos"] if member != photo_id]) > 1]
            self.result.stats = calculate_stats(self.result.photos, self.result.groups)
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
                photo = _inspect(Path(restored["path"]), self.root)
                self.result.photos[photo.id] = photo
                self.result.groups = compare_photos(self.result.photos, self.threshold)
                self.result.stats = calculate_stats(self.result.photos, self.result.groups)
            except Exception as error:
                self.result.errors.append({"name": restored["name"], "message": "Restored successfully; preview could not be refreshed: " + str(error)})
            self.preview_cache.clear()
            return {"ok": True, "restored": restored, "state": self.state()}

    def trash(self) -> dict:
        with self.lock:
            return {"items": self.store.items() if self.store else []}

    def _photo_path(self, photo_id) -> tuple[Path, str]:
        with self.lock:
            photo = self.result.photos.get(photo_id)
            if photo is None:
                raise ReviewError("This file is not in the current scan.", 404)
            if self.store is None:
                raise ReviewError("The media folder is unavailable.", 409)
            path = self.store._original(photo.path)
            if not path.is_file():
                raise ReviewError("The file no longer exists. Scan again.", 404)
            if file_signature(path) != photo.signature:
                raise ReviewError("The file changed after scanning. Scan again before comparing it.", 409)
            return path, photo.sha256

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
            path, signature = self._photo_path(photo_id)
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
        with self.lock:
            path, digest = self._photo_path(photo_id)
            photo = self.result.photos[photo_id]
            kind, mime = photo.mediaType, photo.mime
            source_signature = photo.signature
        if kind not in {"video", "audio"}:
            raise ReviewError("This file does not have a video or audio playback preview.", 415)
        if ffmpeg_binary() is None:
            native = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".wave": "audio/wav",
                      ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
                      ".flac": "audio/flac", ".mp4": "video/mp4", ".m4v": "video/mp4",
                      ".webm": "video/webm", ".ogv": "video/ogg", ".m4a": "audio/mp4"}
            native_mime = native.get(path.suffix.lower())
            if native_mime:
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
            for photo in self.result.photos.values():
                photo.ocrText = ""
                photo.visualEmbedding = None
        visual_search.clear_caches()
        clear_ocr_cache()
        with self.media_lock:
            if self.media_directory is not None:
                self.media_directory.cleanup()
                self.media_directory = None
                self.media_cache.clear()

    def pick_folder(self) -> dict:
        with self.lock:
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
            maximum_length = 1024 * 1024 if path == "/api/bulk-delete-plan" else 32768
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
                                               data.get("includeAllFiles", True)))
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
