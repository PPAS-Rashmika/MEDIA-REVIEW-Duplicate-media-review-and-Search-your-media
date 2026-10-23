"""Recoverable file moves, with an atomic journal written before each move."""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scanner import TRASH_NAME, Photo, file_signature, hash_file, is_reparse, media_metadata


class StorageError(ValueError):
    pass


class TrashStore:
    def __init__(self, root):
        self.root = Path(os.path.abspath(str(root)))
        self.directory = self.root / TRASH_NAME
        self.journal = self.directory / "journal.json"
        self.lock = threading.RLock()
        self.records = []
        self._load()

    def _load(self):
        if self.directory.exists() and is_reparse(self.directory):
            raise StorageError("The recovery folder is a link. Rename it before using file recovery.")
        if not self.journal.exists():
            return
        if is_reparse(self.journal):
            raise StorageError("The recovery journal is a link and cannot be used safely.")
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("items"), list):
                raise ValueError("Unsupported recovery journal.")
            self.records = data["items"]
            changed = False
            for record in self.records:
                source = self._original(record["relativePath"])
                destination = self._stored(record)
                if record.get("status") == "pending":
                    if destination.is_file() and not source.exists():
                        record["status"] = "trashed"
                        changed = True
                    elif source.is_file() and not destination.exists():
                        record["status"] = "cancelled"
                        changed = True
                elif record.get("status") == "restoring":
                    restore_path = self._original(record.get("restoreRelativePath", record["relativePath"]))
                    if restore_path.is_file() and not destination.exists():
                        record["status"] = "restored"
                        changed = True
                    elif destination.is_file():
                        record["status"] = "trashed"
                        changed = True
            if changed:
                self._save()
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StorageError(f"The file recovery journal could not be read: {error}. Your files have not been changed.") from error

    def _safe_directory(self):
        if self.directory.exists() and is_reparse(self.directory):
            raise StorageError("The recovery folder became a link. No file was moved.")
        self.directory.mkdir(exist_ok=True)
        if self.journal.exists() and is_reparse(self.journal):
            raise StorageError("The recovery journal became a link. No file was moved.")

    def _save(self):
        self._safe_directory()
        temporary = self.directory / ("journal-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"version": 1, "items": self.records}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _original(self, relative) -> Path:
        relative = Path(str(relative))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] == TRASH_NAME:
            raise StorageError("Invalid original path in recovery journal.")
        result = self.root / relative
        try:
            result.absolute().relative_to(self.root)
        except ValueError as error:
            raise StorageError("The requested file is outside this folder.") from error
        current = self.root
        for part in relative.parts:
            current /= part
            if current.exists() and is_reparse(current):
                raise StorageError("A folder or file is now a link. Scan a normal folder before continuing.")
        if is_reparse(self.root):
            raise StorageError("The selected folder is now a link. No file was moved.")
        return result

    def _stored(self, record) -> Path:
        if self.directory.exists() and is_reparse(self.directory):
            raise StorageError("The recovery folder became a link. No recovery file can be read safely.")
        name = str(record["trashFile"])
        if Path(name).name != name or not name or name in {".", "..", "journal.json"}:
            raise StorageError("Invalid recovery-file path in journal.")
        result = self.directory / name
        if result.exists() and is_reparse(result):
            raise StorageError("A recovery file became a link.")
        return result

    def verify_photo(self, photo: Photo):
        path = self._original(photo.path)
        if path != photo.file or not path.is_file():
            raise StorageError("This file no longer exists at its scanned location. Scan again.")
        try:
            before = file_signature(path)
            if before != photo.signature or hash_file(path) != photo.sha256 or file_signature(path) != before:
                raise StorageError("This file changed after scanning. Scan again before moving it.")
        except OSError as error:
            raise StorageError(f"Cannot check this file: {error}") from error

    def move(self, photo: Photo, keeper: Photo, keeper_store=None) -> dict:
        with self.lock:
            if photo.id == keeper.id:
                raise StorageError("Choose a different file to keep.")
            self.verify_photo(photo)
            (keeper_store or self).verify_photo(keeper)
            record_id = uuid.uuid4().hex
            record = {"id": record_id, "relativePath": photo.path, "name": photo.name,
                      "trashFile": record_id + photo.file.suffix, "size": photo.size,
                      "sha256": photo.sha256, "deletedAt": datetime.now(timezone.utc).isoformat(), "status": "pending",
                      "mediaType": photo.mediaType, "mime": photo.mime, "previewMode": photo.previewMode,
                      "format": photo.format}
            destination = self._stored(record)
            self.records.append(record)
            try:
                self._save()  # Intent is durable before the original moves.
            except Exception:
                self.records.remove(record)
                raise
            try:
                # os.rename refuses to replace an existing file on Windows; the
                # random recovery name also makes collision exceedingly unlikely.
                if destination.exists():
                    raise StorageError("A recovery filename collision occurred. Please try again.")
                self.verify_photo(photo)
                (keeper_store or self).verify_photo(keeper)
                os.rename(photo.file, destination)
            except Exception as error:
                record["status"] = "cancelled"
                try:
                    self._save()
                except OSError:
                    pass
                raise StorageError(f"The file could not be moved: {error}") from error
            record["status"] = "trashed"
            try:
                self._save()
            except (OSError, StorageError):
                # The previous pending intent is enough to recover this completed
                # move on startup. Report it as completed even if the journal
                # became unavailable; the original has already been moved.
                pass
            return self.public(record)

    def public(self, record) -> dict:
        if all(key in record for key in ("mediaType", "mime", "format")):
            kind, mime, media_format = record["mediaType"], record["mime"], record["format"]
        else:
            kind, mime, media_format = media_metadata(self._stored(record))
        return {"id": record["id"], "name": record["name"],
                "originalPath": str(self.root / record["relativePath"]), "size": record["size"],
                "deletedAt": record["deletedAt"], "previewUrl": "/api/trash/" + record["id"] + "/preview",
                "mediaType": record.get("mediaType", kind), "mime": record.get("mime", mime),
                "previewMode": record.get("previewMode", "image" if kind == "image" else "fallback"),
                "format": record.get("format", media_format)}

    def items(self) -> list[dict]:
        with self.lock:
            return [self.public(record) for record in reversed(self.records)
                    if record.get("status") in {"trashed", "pending", "restoring"} and self._stored(record).is_file()]

    def preview_path(self, record_id) -> Path:
        with self.lock:
            for record in self.records:
                if record["id"] == record_id and record.get("status") in {"trashed", "pending", "restoring"}:
                    path = self._stored(record)
                    if path.is_file():
                        return path
            raise StorageError("This recovery file was not found.")

    def restore(self, record_id: str) -> dict:
        with self.lock:
            record = next((item for item in self.records if item["id"] == record_id and item.get("status") in {"trashed", "pending", "restoring"}), None)
            if record is None:
                raise StorageError("This recovery file was not found.")
            source = self._stored(record)
            if not source.is_file():
                raise StorageError("The recovery file is missing. No existing file was changed.")
            if hash_file(source) != record["sha256"]:
                raise StorageError("The recovery file changed. Restore it manually after checking it.")
            destination = self._original(record["relativePath"])
            suffix = 1
            while destination.exists():
                destination = self._original(str(Path(record["relativePath"]).with_name(
                    Path(record["relativePath"]).stem + f" (restored {suffix})" + Path(record["relativePath"]).suffix)))
                suffix += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._original(str(destination.relative_to(self.root)))
            record["status"] = "restoring"
            record["restoreRelativePath"] = str(destination.relative_to(self.root))
            self._save()
            try:
                if destination.exists():
                    raise StorageError("The restore location now exists. Try restoring again.")
                os.rename(source, destination)
            except OSError as error:
                record["status"] = "trashed"
                self._save()
                raise StorageError(f"The file could not be restored: {error}") from error
            record["status"] = "restored"
            try:
                self._save()
            except OSError:
                pass  # The durable restoring intent recovers on next startup.
            return {"id": record_id, "name": destination.name, "path": str(destination),
                    "renamed": destination.name != record["name"]}

    def remove_records(self, record_ids, *, recycle=False, confirmed=False):
        """Explicitly dispose verified recovery files; never walk an arbitrary path."""
        if confirmed is not True:
            raise StorageError("Review the recovery-file list and confirm removal first.")
        with self.lock:
            if (not isinstance(record_ids, list) or any(not isinstance(x, str) for x in record_ids)
                    or len(record_ids) != len(set(record_ids))):
                raise StorageError("Choose recovery files from the current list.")
            selected = []
            for record_id in record_ids:
                record = next((r for r in self.records if r.get('id') == record_id and r.get('status') == 'trashed'), None)
                if record is None:
                    raise StorageError("The recovery-file list changed. Preview it again.")
                path = self._stored(record)
                before = file_signature(path)
                if not path.is_file() or hash_file(path) != record['sha256'] or file_signature(path) != before:
                    raise StorageError("A recovery file changed. Nothing was removed.")
                selected.append((record, path, before))
            removed, failures = [], []
            for record, path, before in selected:
                try:
                    self._stored(record)
                    if file_signature(path) != before or hash_file(path) != record['sha256']:
                        raise StorageError("The recovery file changed before removal.")
                    if recycle:
                        windows_recycle([path])
                    else:
                        path.unlink()
                    record['status'] = 'recycled' if recycle else 'purged'
                    self._save()
                    removed.append(record['id'])
                except (OSError, StorageError) as error:
                    failures.append({'id': record['id'], 'error': str(error)})
            return {'removed': removed, 'failures': failures}


def windows_recycle(paths):
    """Ask the native Windows shell to recycle specific absolute regular files."""
    if os.name != 'nt':
        raise StorageError('Windows Recycle Bin is available only on Windows.')
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]

    entries = []
    for path in paths:
        path = Path(os.path.abspath(str(path)))
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if os.path.lexists(current) and is_reparse(current):
                raise StorageError('A selected path became a link or junction.')
        if not path.is_file():
            raise StorageError('A selected file no longer exists.')
        entries.append(str(path))
    if not entries:
        return
    # Explicit app confirmation happens before this call. FOF_ALLOWUNDO asks
    # Explorer to use the Recycle Bin instead of a permanent shell delete.
    source = ctypes.create_unicode_buffer('\0'.join(entries) + '\0\0')
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    # NO_CONNECTED_ELEMENTS keeps an HTML file's associated directory out of
    # this explicit file-only request. WANTNUKEWARNING overrides the silent
    # confirmation if Windows cannot recycle a file (for example on a share).
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x2000 | 0x4000
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise StorageError('Windows did not complete the Recycle Bin operation. Check the original location.')
