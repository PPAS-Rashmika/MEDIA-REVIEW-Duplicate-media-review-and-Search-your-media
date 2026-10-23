"""Bounded multi-folder recovery and explicit interrupted-scan checkpoints."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets

from scanner import is_reparse
from storage import TrashStore, StorageError


def normalize_roots(roots, fallback=None, require_online=True, allow_nested=False):
    if roots is None:
        roots = [fallback] if fallback else []
    if not isinstance(roots, list) or not 1 <= len(roots) <= 32:
        raise ValueError('Choose between one and 32 scan folders.')
    result, seen = [], set()
    for item in roots:
        item = {'path': str(item)} if isinstance(item, (str, Path)) else item
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path'].strip():
            raise ValueError('Every scan folder needs a path.')
        if any(type(item.get(key, False)) is not bool for key in ('protected', 'reference')):
            raise ValueError('Folder protection must be true or false.')
        path = Path(os.path.abspath(os.path.expanduser(item['path'].strip())))
        if require_online and (not path.is_dir() or is_reparse(path)):
            raise ValueError('A scan folder is unavailable or linked: ' + str(path))
        current = path
        while current != current.parent:
            if current.exists() and is_reparse(current):
                raise ValueError('Choose folders without linked ancestors.')
            current = current.parent
        identity = os.path.normcase(str(path))
        if identity in seen:
            raise ValueError('Each scan folder must appear only once.')
        # Nested roots make ownership and deletion protection ambiguous.
        for prior in result:
            a = os.path.normcase(str(path))
            b = os.path.normcase(prior['path'])
            if not allow_nested and (a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)):
                raise ValueError('Choose separate scan folders; selected folders must not contain each other.')
        seen.add(identity)
        result.append({'path': str(path), 'protected': item.get('protected', False) or item.get('reference', False),
                       'reference': item.get('reference', False)})
    return result


class MultiRootTrash:
    def __init__(self, roots):
        self.roots = roots
        self.root = Path(roots[0]['path'])
        self.stores = {os.path.normcase(item['path']): TrashStore(Path(item['path'])) for item in roots
                       if Path(item['path']).is_dir()}

    def for_photo(self, photo):
        if getattr(photo, 'offline', False):
            raise StorageError('Offline catalog files are read-only. Reconnect the drive and scan it before changing files.')
        source = getattr(photo, 'sourceRoot', '')
        root = source or str(self.root)
        store = self.stores.get(os.path.normcase(root))
        if store is None:
            raise StorageError('This file belongs to an unavailable scan folder.')
        return store

    def _original(self, relative):
        return self.stores[os.path.normcase(str(self.root))]._original(relative)

    def verify_photo(self, photo):
        return self.for_photo(photo).verify_photo(photo)

    def move(self, photo, keeper):
        if getattr(photo, 'protected', False) or getattr(photo, 'reference', False):
            raise StorageError('This file is in a protected folder.')
        return self.for_photo(photo).move(photo, keeper, keeper_store=self.for_photo(keeper))

    def items(self):
        return sorted((dict(item, sourceRoot=str(store.root)) for store in self.stores.values()
                       for item in store.items()), key=lambda item: item['deletedAt'], reverse=True)

    def _record_store(self, record_id):
        for store in self.stores.values():
            if any(record['id'] == record_id for record in store.records):
                return store
        raise StorageError('This recovery file was not found.')

    def preview_path(self, record_id):
        return self._record_store(record_id).preview_path(record_id)

    def restore(self, record_id):
        store = self._record_store(record_id)
        root = next(item for item in self.roots if os.path.normcase(item['path']) == os.path.normcase(str(store.root)))
        if root.get('protected'):
            raise StorageError('Remove folder protection before restoring files into it.')
        return dict(store.restore(record_id), sourceRoot=str(store.root))


class InterruptedScan:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'interrupted-scan.json'

    def read(self):
        if not self.path.exists():
            return None
        try:
            if is_reparse(self.directory) or is_reparse(self.path) or self.path.stat().st_size > 262144:
                return None
            body = json.loads(self.path.read_text('utf-8'))
            if not isinstance(body, dict) or body.get('schema') != 1:
                return None
            body['roots'] = normalize_roots(body.get('roots'), require_online=False)
            if not isinstance(body.get('settings'), dict):
                return None
            return body
        except (ValueError, OSError):
            return None

    def write(self, roots, settings):
        self.directory.mkdir(parents=True, exist_ok=True)
        if is_reparse(self.directory) or (self.path.exists() and is_reparse(self.path)):
            raise ValueError('The scan checkpoint location is linked.')
        temporary = self.directory / (secrets.token_hex(12) + '.checkpoint')
        try:
            data = json.dumps({'schema': 1, 'roots': roots, 'settings': settings}, allow_nan=False)
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self):
        if self.path.exists() and not is_reparse(self.path):
            self.path.unlink()
