"""Explicit, journalled organization of an already scanned local library.

Planning never creates directories or moves files. Every move has a durable
intent first; recovery only reconciles intents, and never moves files itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from scanner import SUPPORT_DIRECTORIES, file_signature, hash_file, is_reparse

JOURNAL_DIRECTORY = ".media-review-organize"
TYPE_DIRECTORY = "Organized by type"
YEAR_DIRECTORY = "Organized by year"
_ROOTS = {"type": TYPE_DIRECTORY, "year": YEAR_DIRECTORY}
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {"prepared", "pending", "moved", "failed", "undo-pending", "restored", "conflict"}


class OrganizeError(ValueError):
    pass


def photo_id(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:24]


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _normal_ancestors(path: Path, include_leaf=True):
    """lstat every ancestor, including ancestors above the selected root."""
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    if not include_leaf:
        parts = parts[:-1]
    for part in parts:
        current /= part
        if _exists(current) and is_reparse(current):
            raise OrganizeError("A file or folder is a link or junction. No file was moved through it.")
        if current != path and _exists(current) and not current.is_dir():
            raise OrganizeError("An organization folder is occupied by a file.")


def _move_without_replace(source: Path, destination: Path):
    """Windows rename is atomic and refuses an existing destination.

    The POSIX fallback creates a hard link exclusively before unlinking the
    source. This also preserves metadata and refuses to clobber a racing file.
    Both names remain recoverable if unlink fails.
    """
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination, follow_symlinks=False)
        os.unlink(source)


def _extension_group(name: str) -> str:
    extension = Path(name).suffix[1:]
    if not extension:
        return "No extension"
    # Windows permits unusual extension names. Keep the folder safe and short.
    group = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", extension.upper()).strip(" .")[:64]
    if not group:
        group = "Other extension"
    if group.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)],
                         *[f"LPT{i}" for i in range(1, 10)]}:
        group = "_" + group
    return group


def _taken_year(path: Path):
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            values = []
            try:
                values.append(exif.get_ifd(34665).get(36867))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
            values.append(exif.get(36867))
            for value in values:
                if isinstance(value, bytes):
                    value = value.decode("ascii", "strict")
                if not isinstance(value, str):
                    continue
                value = value.strip().rstrip("\x00")
                if re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", value):
                    parsed = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                    return f"{parsed.year:04d}"
    except (OSError, ValueError, TypeError, SyntaxError, UnicodeError, Image.DecompressionBombError):
        pass
    return None


def _year_group(snapshot: dict, basis: str):
    if basis == "taken" and snapshot["mediaType"] == "image":
        year = _taken_year(snapshot["file"])
        if year:
            return year, "taken"
    try:
        return f"{datetime.fromtimestamp(snapshot['signature'][3] / 1_000_000_000).year:04d}", "modified"
    except (ValueError, OverflowError, OSError):
        return "Unknown year", "modified"


class Organizer:
    def __init__(self, root):
        self.root = Path(os.path.abspath(str(root)))
        self.directory = self.root / JOURNAL_DIRECTORY
        self.journal = self.directory / "journal.json"
        self.lock = threading.RLock()
        self.operations = []
        self.generation = None
        self._record_locations = {}
        self._journal_unhealthy = False
        self._incomplete_tail = False
        _normal_ancestors(self.root)
        self._load()

    def _path(self, relative, *, destination=False, mode=None):
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise OrganizeError("Invalid path in organization plan or journal.")
        parts = relative.replace("\\", "/").split("/")
        if any(not part or part in {".", ".."} or ":" in part or part.endswith((" ", "."))
               for part in parts):
            raise OrganizeError("Invalid path in organization plan or journal.")
        if any(part.casefold() in {name.casefold() for name in SUPPORT_DIRECTORIES | {JOURNAL_DIRECTORY}}
               for part in parts):
            raise OrganizeError("Application support and recovery files cannot be organized.")
        path = Path(relative)
        if path.is_absolute() or path.drive or path.root:
            raise OrganizeError("Organization paths must stay inside the scanned folder.")
        if destination:
            if (mode not in _ROOTS or len(parts) != 3 or parts[0] != _ROOTS[mode] or
                    (mode == "year" and not (re.fullmatch(r"\d{4}", parts[1]) and parts[1] != "0000" or
                                             parts[1] == "Unknown year"))):
                raise OrganizeError("Invalid organized destination in plan or journal.")
        result = self.root.joinpath(*parts)
        try:
            result.relative_to(self.root)
        except ValueError as error:
            raise OrganizeError("Organization paths must stay inside the scanned folder.") from error
        _normal_ancestors(result)
        return result

    def _save(self):
        _normal_ancestors(self.directory)
        self.directory.mkdir(exist_ok=True)
        _normal_ancestors(self.journal)
        temporary = self.directory / ("journal-" + uuid.uuid4().hex + ".tmp")
        generation = uuid.uuid4().hex
        event_file = self.directory / ("events-" + generation + ".jsonl")
        try:
            # A new immutable snapshot and a fresh append log form one
            # generation. Old logs cannot replay onto the new snapshot after a
            # crash between its atomic replacement and the next event write.
            with event_file.open("xb") as output:
                output.flush()
                os.fsync(output.fileno())
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"version": 1, "generation": generation, "operations": self.operations},
                          output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size > 64 * 1024 * 1024:
                raise OrganizeError("The organization history is full. No further files were moved.")
            _normal_ancestors(self.journal)
            os.replace(temporary, self.journal)
            self.generation = generation
            self._journal_unhealthy = False
            self._incomplete_tail = False
            self._index_records()
        finally:
            if _exists(temporary) and not is_reparse(temporary):
                temporary.unlink()

    def _index_records(self):
        self._record_locations = {id(record): (operation["id"], index)
                                  for operation in self.operations for index, record in enumerate(operation["files"])}

    def _append(self, event):
        if self._journal_unhealthy:
            raise OrganizeError("The organization recovery log needs to be reopened. No further files were moved.")
        if self.generation is None:
            self._save()
        event_file = self.directory / ("events-" + self.generation + ".jsonl")
        _normal_ancestors(event_file)
        if not event_file.is_file():
            raise OrganizeError("The organization recovery log is missing. No further files were moved.")
        # Small append-only fsynced intents make per-file durability linear in
        # the number of files, without rewriting the complete library each time.
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with event_file.open("ab") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            self._journal_unhealthy = True
            raise

    def _replay(self, generation):
        if generation is None:
            return
        if not isinstance(generation, str) or not _ID.fullmatch(generation):
            raise ValueError("Invalid organization recovery generation.")
        event_file = self.directory / ("events-" + generation + ".jsonl")
        _normal_ancestors(event_file)
        if not event_file.is_file() or event_file.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("Organization recovery log is missing or too large.")
        operations = {operation["id"]: operation for operation in self.operations}
        with event_file.open("rb") as source:
            while line := source.readline(16385):
                if len(line) > 16384:
                    raise ValueError("Invalid organization recovery event.")
                if not line.endswith(b"\n"):
                    # A force quit can leave only the last append incomplete.
                    # Its predecessor intent still protects any completed move.
                    self._incomplete_tail = True
                    break
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("operationId") not in operations:
                    raise ValueError("Invalid organization recovery operation.")
                operation = operations[event["operationId"]]
                if "finishedAt" in event:
                    if set(event) != {"operationId", "finishedAt"} or not isinstance(event["finishedAt"], str):
                        raise ValueError("Invalid organization completion event.")
                    operation["finishedAt"] = event["finishedAt"]
                    continue
                index = event.get("index")
                if (not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(operation["files"]) or
                        event.get("status") not in _STATUSES or
                        set(event) - {"operationId", "index", "status", "restoredPath"}):
                    raise ValueError("Invalid organization recovery file event.")
                record = operation["files"][index]
                if "restoredPath" in event:
                    restored = self._path(event["restoredPath"])
                    original = self._path(record["source"])
                    if restored != original:
                        raise ValueError("Undo must return files to their original location.")
                    record["restoredPath"] = event["restoredPath"]
                record["status"] = event["status"]
        self.generation = generation

    def _load(self):
        _normal_ancestors(self.journal)
        if not _exists(self.journal):
            return
        try:
            if self.journal.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("Organization journal is too large.")
            data = json.loads(self.journal.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1 or
                    not isinstance(data.get("operations"), list)):
                raise ValueError("Unsupported organization journal.")
            operations = data["operations"]
            ids = set()
            # Validate the whole journal before writing reconciliation changes.
            for operation in operations:
                if (not isinstance(operation, dict) or not _ID.fullmatch(str(operation.get("id", ""))) or
                        operation["id"] in ids or operation.get("mode") not in _ROOTS or
                        operation.get("yearBasis") not in {"taken", "modified"} or
                        not isinstance(operation.get("files"), list) or
                        not isinstance(operation.get("finishedAt"), str)):
                    raise ValueError("Invalid organization operation.")
                ids.add(operation["id"])
                sources, destinations = set(), set()
                for record in operation["files"]:
                    if (not isinstance(record, dict) or record.get("status") not in _STATUSES or
                            not _SHA.fullmatch(str(record.get("sha256", ""))) or
                            not isinstance(record.get("size"), int) or isinstance(record["size"], bool) or record["size"] < 0):
                        raise ValueError("Invalid organization file record.")
                    self._path(record["source"])
                    self._path(record["destination"], destination=True, mode=operation["mode"])
                    source_key, destination_key = record["source"].casefold(), record["destination"].casefold()
                    if source_key in sources or destination_key in destinations or source_key == destination_key:
                        raise ValueError("Duplicate or unchanged paths in organization journal.")
                    sources.add(source_key)
                    destinations.add(destination_key)
                    if "restoredPath" in record:
                        restored = self._path(record["restoredPath"])
                        original = self._path(record["source"])
                        if restored != original:
                            raise ValueError("Invalid restored organization location.")
            self.operations = operations
            self._replay(data.get("generation"))
            self._index_records()
            changed = self._incomplete_tail
            for operation in operations:
                for record in operation["files"]:
                    status = record["status"]
                    if status not in {"pending", "undo-pending"}:
                        continue
                    source = self._path(record["source"])
                    organized = self._path(record["destination"], destination=True, mode=operation["mode"])
                    if status == "pending":
                        if not _exists(source) and self._matches(organized, record):
                            record["status"] = "moved"
                        elif self._matches(source, record) and not _exists(organized):
                            record["status"] = "failed"
                        else:
                            record["status"] = "conflict"
                    else:
                        restored = self._path(record.get("restoredPath", record["source"]))
                        if not _exists(organized) and self._matches(restored, record):
                            record["status"] = "restored"
                        elif self._matches(organized, record) and not _exists(restored):
                            record["status"] = "moved"
                        else:
                            record["status"] = "conflict"
                    changed = True
                if not operation["finishedAt"]:
                    operation["finishedAt"] = datetime.now(timezone.utc).isoformat()
                    changed = True
            if changed:
                self._save()
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise OrganizeError(f"The organization recovery journal could not be read: {error}. No media files were changed.") from error

    @staticmethod
    def _matches(path, record):
        try:
            before = file_signature(path)
            return (path.is_file() and not is_reparse(path) and before[2] == record["size"] and
                    hash_file(path) == record["sha256"] and file_signature(path) == before)
        except OSError:
            return False

    def _verify(self, snapshot):
        path = self._path(snapshot["path"])
        if path != snapshot["file"] or not path.is_file():
            raise OrganizeError("A scanned file is missing. Scan again before organizing files.")
        try:
            before = file_signature(path)
            if (before != tuple(snapshot["signature"]) or hash_file(path) != snapshot["sha256"] or
                    file_signature(path) != before):
                raise OrganizeError("A scanned file changed. Scan again before organizing files.")
        except OSError as error:
            raise OrganizeError(f"A scanned file cannot be checked: {error}") from error

    @staticmethod
    def _snapshot(photo):
        return {"id": photo.id, "file": photo.file, "path": photo.path, "name": photo.name,
                "size": photo.size, "signature": tuple(photo.signature), "sha256": photo.sha256,
                "mediaType": photo.mediaType}

    def _unused_name(self, path, occupied):
        original = path
        number = 2
        while _exists(path) or os.path.normcase(str(path)).casefold() in occupied:
            label = f" ({number})"
            path = original.with_name(original.stem + label + original.suffix)
            number += 1
        return path

    def create_plan(self, photos, mode, year_basis="taken", progress=None):
        if not isinstance(mode, str) or mode not in _ROOTS:
            raise OrganizeError("Choose file type or year organization.")
        if not isinstance(year_basis, str) or year_basis not in {"taken", "modified"}:
            raise OrganizeError("Choose date taken or date modified for year organization.")
        snapshots = sorted((self._snapshot(photo) for photo in photos),
                           key=lambda item: (item["path"].casefold(), item["path"]))
        files, occupied = [], set()
        already, renamed = 0, 0
        groups = defaultdict(lambda: {"count": 0, "totalSize": 0})
        for index, snapshot in enumerate(snapshots):
            path = self._path(snapshot["path"])
            if path != snapshot["file"] or not path.is_file():
                raise OrganizeError("A scanned file is missing. Scan again before organizing files.")
            if mode == "type":
                group, date_source = _extension_group(snapshot["name"]), "not-applicable"
            else:
                group, date_source = _year_group(snapshot, year_basis)
            destination = self.root / _ROOTS[mode] / group / snapshot["name"]
            self._path(str(destination.relative_to(self.root)), destination=True, mode=mode)
            if os.path.normcase(str(destination)).casefold() == os.path.normcase(str(path)).casefold():
                already += 1
                occupied.add(os.path.normcase(str(path)).casefold())
                continue
            destination = self._unused_name(destination, occupied)
            renamed += destination.name != snapshot["name"]
            occupied.add(os.path.normcase(str(destination)).casefold())
            files.append({"id": snapshot["id"], "name": snapshot["name"], "path": snapshot["path"],
                          "destination": str(destination.relative_to(self.root)), "group": group,
                          "size": snapshot["size"], "dateSource": date_source})
            groups[group]["count"] += 1
            groups[group]["totalSize"] += snapshot["size"]
            if progress:
                progress(index + 1, len(snapshots), "planning")
        return {"ok": True, "mode": mode, "yearBasis": year_basis, "destinationRoot": _ROOTS[mode],
                "groups": [dict(name=group, **values) for group, values in sorted(groups.items())],
                "files": files, "moveCount": len(files), "alreadyOrganizedCount": already,
                "totalSize": sum(item["size"] for item in files), "renamedCount": renamed,
                "_snapshots": snapshots}

    def history(self):
        with self.lock:
            return [{"id": operation["id"], "mode": operation["mode"], "yearBasis": operation["yearBasis"],
                     "root": str(self.root), "destinationRoot": _ROOTS[operation["mode"]],
                     "finishedAt": operation["finishedAt"],
                     "movedCount": sum(record["status"] in {"moved", "undo-pending", "restored"}
                                       for record in operation["files"]),
                     "undoAvailable": any(record["status"] == "moved" for record in operation["files"])}
                    for operation in reversed(self.operations)]

    def _record_status(self, record, status, **updates):
        with self.lock:
            operation_id, index = self._record_locations[id(record)]
            self._append(dict(operationId=operation_id, index=index, status=status, **updates))
            record.update(status=status, **updates)

    def _finish(self, operation):
        with self.lock:
            finished = datetime.now(timezone.utc).isoformat()
            self._append({"operationId": operation["id"], "finishedAt": finished})
            operation["finishedAt"] = finished

    def execute(self, plan, operation_id, *, progress=None, moved=None):
        """Full-library preflight completes before the first durable move intent."""
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise OrganizeError("Invalid organization operation ID.")
        snapshots = {item["id"]: item for item in plan["_snapshots"]}
        for index, snapshot in enumerate(snapshots.values()):
            self._verify(snapshot)
            if progress:
                progress(index + 1, len(snapshots), "checking")
        for item in plan["files"]:
            destination = self._path(item["destination"], destination=True, mode=plan["mode"])
            if _exists(destination):
                raise OrganizeError("A planned destination now exists. Preview a new organization plan.")
        operation = {"id": operation_id, "mode": plan["mode"], "yearBasis": plan["yearBasis"],
                     "finishedAt": "", "files": []}
        for item in plan["files"]:
            snapshot = snapshots[item["id"]]
            operation["files"].append({"source": item["path"], "destination": item["destination"],
                                        "sha256": snapshot["sha256"], "size": snapshot["size"],
                                        "status": "prepared"})
        with self.lock:
            self.operations.append(operation)
            try:
                self._save()
            except Exception:
                self.operations.remove(operation)
                raise
        successes, failures = 0, []
        for index, (item, record) in enumerate(zip(plan["files"], operation["files"])):
            did_move = False
            try:
                snapshot = snapshots[item["id"]]
                self._verify(snapshot)
                source = self._path(record["source"])
                destination = self._path(record["destination"], destination=True, mode=plan["mode"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._path(record["destination"], destination=True, mode=plan["mode"])
                if _exists(destination):
                    raise OrganizeError("The destination became occupied. The existing file was kept.")
                self._record_status(record, "pending")
                # Recheck paths after journal fsync; never follow a new junction.
                self._verify(snapshot)
                source = self._path(record["source"])
                destination = self._path(record["destination"], destination=True, mode=plan["mode"])
                if file_signature(source) != snapshot["signature"]:
                    raise OrganizeError("The source changed during organization. It was kept.")
                _move_without_replace(source, destination)
                did_move = True
                successes += 1
                if moved:
                    moved(source, destination)
                self._record_status(record, "moved")
            except (OSError, OrganizeError) as error:
                failures.append({"name": item["name"], "path": item["path"], "error": str(error)})
                try:
                    self._record_status(record, "moved" if did_move else "failed")
                except (OSError, OrganizeError):
                    # Last durable pending intent recovers a completed move.
                    break
            if progress:
                progress(index + 1, len(operation["files"]), "moving", successes)
        try:
            self._finish(operation)
        except (OSError, OrganizeError) as error:
            failures.append({"name": "Recovery journal", "path": JOURNAL_DIRECTORY,
                             "error": "Moves remain recoverable; journal update failed: " + str(error)})
        return {"ok": not failures, "operationId": operation_id, "kind": "move", "mode": plan["mode"],
                "movedCount": successes, "skippedCount": len(operation["files"]) - successes,
                "failures": failures,
                "message": f"Moved {successes} of {len(operation['files'])} files."}

    def undo(self, operation_id, *, progress=None, moved=None):
        with self.lock:
            operation = next((item for item in self.operations if item["id"] == operation_id), None)
            if operation is None:
                raise OrganizeError("This organization operation was not found.")
            records = [record for record in operation["files"] if record["status"] == "moved"]
        if not records:
            raise OrganizeError("There are no files available to undo in this operation.")
        # Check every available file before undoing any part of this operation.
        for index, record in enumerate(records):
            source = self._path(record["destination"], destination=True, mode=operation["mode"])
            original = self._path(record["source"])
            if _exists(original):
                raise OrganizeError("An original location is occupied. No files were restored. Move that existing file before undoing.")
            if not self._matches(source, record):
                raise OrganizeError("An organized file changed or is missing. No files were restored.")
            if progress:
                progress(index + 1, len(records), "checking")
        successes, failures = 0, []
        for index, record in enumerate(records):
            did_move = False
            try:
                source = self._path(record["destination"], destination=True, mode=operation["mode"])
                if not self._matches(source, record):
                    raise OrganizeError("An organized file changed. It was kept.")
                destination = self._path(record["source"])
                if _exists(destination):
                    raise OrganizeError("The original location became occupied. The existing file was kept.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination = self._path(str(destination.relative_to(self.root)))
                self._record_status(record, "undo-pending", restoredPath=str(destination.relative_to(self.root)))
                source = self._path(record["destination"], destination=True, mode=operation["mode"])
                destination = self._path(record["restoredPath"])
                if not self._matches(source, record):
                    raise OrganizeError("An organized file changed during undo. It was kept.")
                _move_without_replace(source, destination)
                did_move = True
                successes += 1
                if moved:
                    moved(source, destination)
                self._record_status(record, "restored")
            except (OSError, OrganizeError) as error:
                failures.append({"name": Path(record["source"]).name, "path": record["destination"], "error": str(error)})
                try:
                    self._record_status(record, "restored" if did_move else "moved")
                except (OSError, OrganizeError):
                    break
            if progress:
                progress(index + 1, len(records), "restoring", successes)
        try:
            self._finish(operation)
        except (OSError, OrganizeError) as error:
            failures.append({"name": "Recovery journal", "path": JOURNAL_DIRECTORY,
                             "error": "Restored files remain recoverable; journal update failed: " + str(error)})
        return {"ok": not failures, "operationId": operation_id, "kind": "undo", "mode": operation["mode"],
                "movedCount": successes, "skippedCount": len(records) - successes, "failures": failures,
                "message": f"Restored {successes} of {len(records)} files to their original folders."}
