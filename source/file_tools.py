"""Explicit file productivity plans, content checks and durable undo.

Every plan is tied to the current review revision and expires. Journal replay
only reports work; it never performs a file operation at application startup.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from organizer import _move_without_replace, _normal_ancestors
from scanner import SUPPORT_DIRECTORIES, calculate_stats, file_signature, hash_file, is_reparse
from storage import StorageError, windows_recycle


class FileToolError(ValueError):
    pass


def keeper_choices(photos, groups, rule='quality', preferred_folder='', formats=None):
    """Rank every group deterministically; a protected member always wins."""
    if rule not in {'quality', 'newest', 'oldest', 'folder', 'format', 'largest', 'smallest'}:
        raise FileToolError('Choose a valid keep rule.')
    if not isinstance(preferred_folder, str) or len(preferred_folder) > 32768:
        raise FileToolError('Choose a preferred folder.')
    if isinstance(formats, str):
        formats = re.split(r'[,;\s]+', formats)
    if formats is not None and (not isinstance(formats, list) or len(formats) > 100 or any(not isinstance(x, str) for x in formats)):
        raise FileToolError('Enter a short list of preferred file formats.')
    formats = [str(x).lower().lstrip('.') for x in (formats or [])]
    preferred = os.path.normcase(os.path.abspath(preferred_folder)) if preferred_folder.strip() else ''

    def rank(photo):
        modified = photo.signature[3]
        quality = (photo.width * photo.height, photo.size)
        value = quality
        if rule == 'newest': value = (modified, *quality)
        elif rule == 'oldest': value = (-modified, *quality)
        elif rule == 'largest': value = (photo.size, *quality)
        elif rule == 'smallest': value = (-photo.size, *quality)
        elif rule == 'folder':
            try: inside = bool(preferred) and os.path.commonpath([preferred, os.path.normcase(str(photo.file))]) == preferred
            except ValueError: inside = False
            value = (inside, *quality)
        elif rule == 'format':
            extension = photo.file.suffix.lower().lstrip('.')
            value = (-(formats.index(extension) if extension in formats else len(formats)), *quality)
        return (bool(getattr(photo, 'protected', False) or getattr(photo, 'reference', False)), *value)

    result = {}
    for group in groups:
        candidates = sorted((photos[p] for p in group['photos'] if p in photos), key=lambda p: str(p.file).casefold())
        if candidates:
            result[group['id']] = max(candidates, key=rank).id
    return result


def _csv_cell(value):
    text = str(value if value is not None else '')
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


class FileTools:
    def __init__(self, app, directory=None):
        self.app = app
        base = getattr(getattr(app, 'session_store', None), 'directory', None)
        self.directory = Path(directory) if directory is not None else Path(base or Path.home() / '.media-review') / 'file-tools'
        self.journal = self.directory / 'operations.json'
        self.plans = OrderedDict()

    def _roots(self):
        roots = getattr(self.app, 'roots', None) or ([self.app.root] if self.app.root else [])
        result = []
        for value in roots:
            data = value if isinstance(value, dict) else {'path': value}
            if data.get('path'):
                result.append((Path(os.path.abspath(str(data['path']))), bool(data.get('protected') or data.get('reference'))))
        return result

    def _root_for(self, path, *, mutable=True):
        path = Path(os.path.abspath(str(path)))
        candidates = []
        for root, protected in self._roots():
            try:
                path.relative_to(root)
                candidates.append((len(root.parts), root, protected))
            except ValueError:
                continue
        if not candidates:
            raise FileToolError('A selected path is outside the current scanned folders.')
        if mutable and any(protected for _, _, protected in candidates):
            raise FileToolError('Files in protected or reference folders cannot be changed.')
        _normal_ancestors(path)
        return max(candidates)[1]

    def _idle(self):
        self.app._require_idle()
        if getattr(self.app, 'session_busy', False) or getattr(self.app, 'organization', {}).get('running'):
            raise FileToolError('Wait for the current operation to finish.')
        if not getattr(self.app, 'has_scanned', True):
            raise FileToolError('Scan folders or open a scan before using file tools.')

    def _mutable_photo(self, photo):
        if getattr(photo, 'sourceKind', 'local') != 'local' or getattr(photo, 'offline', False):
            raise FileToolError('Archive members and offline catalog entries are read-only.')
        if getattr(photo, 'protected', False) or getattr(photo, 'reference', False):
            raise FileToolError('Files in protected or reference folders cannot be changed.')
        return self._root_for(photo.file)

    def _snapshot(self, photo, *, mutable=True):
        root = self._mutable_photo(photo) if mutable else self._root_for(photo.file, mutable=False)
        if getattr(photo, 'offline', False) or getattr(photo, 'sourceKind', 'local') != 'local':
            raise FileToolError('This file is not an available local file.')
        return {'id': photo.id, 'source': str(photo.file), 'root': str(root), 'name': photo.name,
                'signature': list(photo.signature), 'sha256': photo.sha256, 'size': photo.size}

    def _verify(self, item, key='source', *, mutable=True):
        path = Path(item[key])
        self._root_for(path, mutable=mutable)
        before = file_signature(path)
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise FileToolError('A selected file is no longer a regular file.')
        if tuple(item['signature']) != before or hash_file(path) != item['sha256'] or file_signature(path) != before:
            raise FileToolError('A selected file changed. Preview the operation again.')
        return path

    def _load(self):
        if not self.journal.exists():
            return []
        _normal_ancestors(self.journal)
        if self.journal.stat().st_size > 32 * 1024 * 1024:
            raise FileToolError('The file-tools journal is too large to open safely.')
        try:
            data = json.loads(self.journal.read_text('utf-8'))
        except (ValueError, OSError) as error:
            raise FileToolError('The file-tools recovery journal could not be read.') from error
        if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('operations'), list):
            raise FileToolError('The file-tools recovery journal is unsupported.')
        return data['operations']

    def _save(self, operations):
        _normal_ancestors(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _normal_ancestors(self.journal)
        temporary = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as output:
                json.dump({'schema': 1, 'operations': operations}, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal)
        finally:
            if temporary.exists(): temporary.unlink()

    def history(self):
        with self.app.lock:
            self._idle()
            selected = {os.path.normcase(str(root)) for root, _ in self._roots()}
            result = []
            for operation in reversed(self._load()):
                if not selected.intersection(os.path.normcase(p.get('root', '')) for p in operation.get('files', [])):
                    continue
                completed = sum(x.get('status') == 'done' for x in operation.get('files', []))
                pending = sum(x.get('status') in {'pending', 'undo-pending'} for x in operation.get('files', []))
                result.append({'id': operation.get('id'), 'kind': operation.get('kind'), 'createdAt': operation.get('createdAt'),
                               'completed': completed, 'pending': pending,
                               'undoAvailable': bool((completed or pending) and operation.get('kind') in {'rename', 'hardlinks', 'empty-folders'})})
            return result[:100]

    def report_csv(self):
        with self.app.lock:
            self._idle()
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(['Group', 'Match type', 'Similarity percent', 'Filename', 'Full path', 'Source root', 'Bytes', 'Format', 'Width', 'Height', 'Protected', 'Offline'])
            for group in self.app.result.groups:
                for photo_id in group['photos']:
                    photo = self.app.result.photos.get(photo_id)
                    if photo:
                        writer.writerow([_csv_cell(x) for x in [group['id'], group['kind'], group.get('score', ''), photo.name,
                            str(photo.file), getattr(photo, 'sourceRoot', '') or str(self.app.root or ''), photo.size, photo.format,
                            photo.width, photo.height, bool(getattr(photo, 'protected', False)), bool(getattr(photo, 'offline', False))]])
            return ('\ufeff' + output.getvalue()).encode('utf-8')

    def choose_keepers(self, rule='quality', preferred_folder='', formats=None):
        with self.app.lock:
            self._idle()
            return keeper_choices(self.app.result.photos, self.app.result.groups, rule, preferred_folder, formats)

    def plan(self, kind, **payload):
        with self.app.lock:
            self._idle()
            if getattr(self.app, 'catalog_only', False):
                raise FileToolError('Offline catalogs are read-only. Reconnect the drive and scan it before changing files.')
            if kind not in {'rename', 'empty-folders', 'hardlinks', 'recycle', 'purge-trash', 'recycle-trash'}:
                raise FileToolError('Choose a supported file tool.')
            files = []
            if kind == 'rename':
                template = payload.get('template', '{date}_{counter:04d}')
                if not isinstance(template, str) or not template or len(template) > 200:
                    raise FileToolError('Enter a naming template of at most 200 characters.')
                if re.search(r'{(?!name}|date}|year}|ext}|counter(?::0?[1-8]d)?})', template):
                    raise FileToolError('Use {name}, {date}, {year}, {ext} and {counter:04d} in the naming template.')
                ids = self._ids(payload.get('ids'))
                count = payload.get('start', 1)
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 999999999:
                    raise FileToolError('Choose a nonnegative starting counter.')
                occupied = set()
                for photo_id in ids:
                    photo = self.app.result.photos[photo_id]
                    item = self._snapshot(photo)
                    modified = datetime.fromtimestamp(photo.signature[3] / 1_000_000_000)
                    try:
                        name = template.format(name=photo.file.stem, date=modified.strftime('%Y-%m-%d'), year=modified.year,
                                               ext=photo.file.suffix.lstrip('.'), counter=count)
                    except (ValueError, KeyError, IndexError, AttributeError) as error:
                        raise FileToolError('The naming template is invalid.') from error
                    if '{ext}' not in template: name += photo.file.suffix
                    if (not name or len(name) > 240 or name.endswith((' ', '.')) or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                            or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
                        raise FileToolError('The template creates a filename Windows cannot use.')
                    destination = photo.file.with_name(name)
                    count += 1
                    if os.path.normcase(str(destination)) == os.path.normcase(str(photo.file)): continue
                    normalized = os.path.normcase(str(destination))
                    if normalized in occupied or os.path.lexists(destination):
                        raise FileToolError('The template creates an existing or repeated name. Change the template or starting counter.')
                    occupied.add(normalized)
                    files.append(dict(item, destination=str(destination)))
            elif kind in {'hardlinks', 'recycle'}:
                choices = self.choose_keepers(payload.get('rule', 'quality'), payload.get('preferredFolder', ''), payload.get('formats'))
                wanted = set(self._ids(payload.get('ids'))) if payload.get('ids') is not None else None
                exact = [g for g in self.app.result.groups if g['kind'] == 'exact' and all(
                    getattr(self.app.result.photos[p], 'sourceKind', 'local') == 'local'
                    and not getattr(self.app.result.photos[p], 'offline', False) for p in g['photos'])]
                protected_ids = set(choices[g['id']] for g in exact if g['id'] in choices)
                seen = set()
                for group in exact:
                    keeper = self.app.result.photos[choices[group['id']]]
                    keep = self._snapshot(keeper, mutable=False)
                    for photo_id in group['photos']:
                        if photo_id in protected_ids or photo_id in seen or (wanted is not None and photo_id not in wanted): continue
                        photo = self.app.result.photos[photo_id]
                        if getattr(photo, 'protected', False) or getattr(photo, 'reference', False) or getattr(photo, 'offline', False): continue
                        if getattr(photo, 'sourceKind', 'local') != 'local': continue
                        item = self._snapshot(photo)
                        if kind == 'hardlinks':
                            if item['signature'][0] != keep['signature'][0]: continue
                            if item['signature'][:2] == keep['signature'][:2]: continue
                        files.append(dict(item, keeper=keep))
                        seen.add(photo_id)
            elif kind == 'empty-folders':
                scheduled = set()
                for root, protected in self._roots():
                    if protected: continue
                    self._root_for(root)
                    pending, contents = [root], {}
                    while pending:
                        path = pending.pop()
                        try:
                            self._root_for(path)
                            with os.scandir(path) as entries:
                                children = [(Path(e.path), e.is_dir(follow_symlinks=False)) for e in entries]
                            contents[path] = [child for child, _ in children]
                            for child, directory in children:
                                if directory and child.name.casefold() not in {str(n).casefold() for n in SUPPORT_DIRECTORIES} and not is_reparse(child):
                                    try:
                                        self._root_for(child)
                                        pending.append(child)
                                    except (OSError, ValueError):
                                        continue
                        except (OSError, ValueError):
                            continue
                    for path in sorted(contents, key=lambda p: len(p.parts), reverse=True):
                        try:
                            self._root_for(path)
                            if path == root or any(child not in scheduled for child in contents[path]): continue
                            scheduled.add(path)
                            files.append({'source': str(path), 'root': str(root), 'name': path.name, 'size': 0})
                        except (OSError, ValueError): continue
                files = list({x['source']: x for x in files}.values())
                files.sort(key=lambda x: len(Path(x['source']).parts), reverse=True)
            else:
                stores = getattr(self.app.store, 'stores', None)
                stores = list(stores.values()) if stores else [self.app.store]
                for store in stores:
                    try: self._root_for(store.root)
                    except FileToolError: continue
                    for item in store.items():
                        path = store.preview_path(item['id'])
                        files.append({'id': item['id'], 'source': str(path), 'root': str(store.root), 'name': item['name'],
                                      'size': item['size'], 'signature': list(file_signature(path)), 'sha256': hash_file(path)})
            now = time.monotonic()
            self.plans = OrderedDict((key, value) for key, value in self.plans.items() if value['expires'] > now)
            identity = uuid.uuid4().hex
            self.plans[identity] = {'id': identity, 'kind': kind, 'files': files, 'revision': self.app.review_revision, 'expires': now + 600}
            while len(self.plans) > 8: self.plans.popitem(last=False)
            return {'planId': identity, 'kind': kind, 'files': [{k: v for k, v in x.items() if k not in {'signature', 'sha256'}} for x in files],
                    'count': len(files), 'totalSize': sum(x['size'] for x in files), 'permanent': kind == 'purge-trash',
                    'note': 'Hard links share file contents. Editing one linked path changes the others. Undo creates independent copies.' if kind == 'hardlinks' else ''}

    def _ids(self, values):
        if values is None:
            return [p.id for p in sorted(self.app.result.photos.values(), key=lambda p: str(p.file).casefold())
                    if getattr(p, 'sourceKind', 'local') == 'local' and not (getattr(p, 'protected', False) or getattr(p, 'reference', False) or getattr(p, 'offline', False))]
        if (not isinstance(values, list) or len(values) > 100000 or any(not isinstance(x, str) or x not in self.app.result.photos for x in values)
                or len(values) != len(set(values))):
            raise FileToolError('Choose files from the current library.')
        return values

    def _changed(self):
        self.app._invalidate_bulk_plans()
        self.plans.clear()
        self.app.preview_cache.clear()
        if hasattr(self.app.result, 'folderGroups'): self.app.result.folderGroups = []
        if hasattr(self.app, '_refresh_stats'): self.app._refresh_stats()
        else: self.app.result.stats = calculate_stats(self.app.result.photos, self.app.result.groups)

    def _moved(self, source, destination):
        callback = getattr(self.app, '_organization_moved', None)
        if callback:
            callback(Path(source), Path(destination))
            return
        for photo in self.app.result.photos.values():
            if photo.file == Path(source):
                photo.file = Path(destination)
                photo.name = photo.file.name
                photo.path = str(photo.file.relative_to(self._root_for(photo.file)))
                photo.signature = file_signature(photo.file)

    def execute(self, plan_id, confirmed=False):
        with self.app.lock:
            self._idle()
            if getattr(self.app, 'catalog_only', False):
                raise FileToolError('Offline catalogs are read-only.')
            plan = self.plans.get(plan_id)
            if confirmed is not True:
                raise FileToolError('Review the preview and confirm this operation first.')
            if not plan or plan['expires'] <= time.monotonic() or plan['revision'] != self.app.review_revision:
                raise FileToolError('This preview expired or the library changed. Preview again.')
            kind, files = plan['kind'], plan['files']
            if not files: raise FileToolError('There are no eligible files in this preview.')
            for item in files:
                if kind == 'empty-folders': self._root_for(item['source'])
                else:
                    self._verify(item)
                    if item.get('keeper'): self._verify(item['keeper'], mutable=False)
                    if item.get('keeper') and item['sha256'] != item['keeper']['sha256']:
                        raise FileToolError('This operation requires verified exact copies.')
                    if kind == 'rename' and os.path.lexists(item['destination']):
                        raise FileToolError('A rename destination now exists. Preview again.')
            operations = self._load()
            operation = {'id': uuid.uuid4().hex, 'kind': kind, 'createdAt': datetime.now(timezone.utc).isoformat(),
                         'files': [dict(copy.deepcopy(item), status='prepared') for item in files]}
            operations.append(operation)
            self._save(operations)
            del self.plans[plan_id]
            failures = []
            for item in operation['files']:
                try:
                    item['status'] = 'pending'
                    self._save(operations)
                    path = Path(item['source'])
                    if kind == 'rename':
                        self._verify(item)
                        destination = Path(item['destination'])
                        self._root_for(destination)
                        _move_without_replace(path, destination)
                        item['destinationSignature'] = list(file_signature(destination))
                        item['status'] = 'done'
                        self._save(operations)
                        self._moved(path, destination)
                    elif kind == 'hardlinks':
                        self._verify(item)
                        keeper = self._verify(item['keeper'], mutable=False)
                        if item['sha256'] != item['keeper']['sha256'] or path.stat().st_dev != keeper.stat().st_dev:
                            raise FileToolError('Hard links require identical content on the same filesystem.')
                        backup = path.with_name('.' + path.name + '.media-review-' + operation['id'] + '.bak')
                        item['backup'] = str(backup)
                        self._save(operations)
                        _move_without_replace(path, backup)
                        try:
                            os.link(keeper, path, follow_symlinks=False)
                        except Exception:
                            _move_without_replace(backup, path)
                            raise
                        item['linkedSignature'] = list(file_signature(path))
                        item['status'] = 'done'
                        self._save(operations)
                        if hash_file(backup) != item['sha256']:
                            raise FileToolError('The independent recovery copy changed; it was kept at ' + str(backup))
                        backup.unlink()
                        for photo in self.app.result.photos.values():
                            if photo.file == path: photo.signature = file_signature(path)
                    elif kind == 'empty-folders':
                        self._root_for(path)
                        path.rmdir()  # Refuses any directory that has acquired contents.
                    elif kind == 'recycle':
                        self._verify(item)
                        self._verify(item['keeper'], mutable=False)
                        windows_recycle([path])
                        if path.exists(): raise FileToolError('Windows left this file at its original location.')
                        removed = {p.id for p in self.app.result.photos.values() if p.file == path}
                        self.app.result.photos = {k: p for k, p in self.app.result.photos.items() if k not in removed}
                        self.app.result.groups = [dict(g, photos=remaining) for g in self.app.result.groups
                                                  if len(remaining := [p for p in g['photos'] if p not in removed]) > 1]
                    else:
                        stores = getattr(self.app.store, 'stores', None)
                        values = list(stores.values()) if stores else [self.app.store]
                        store = next((s for s in values if s.root == Path(item['root'])), None)
                        if store is None: raise FileToolError('The recovery folder changed.')
                        result = store.remove_records([item['id']], recycle=kind == 'recycle-trash', confirmed=True)
                        if result['failures']: raise FileToolError(result['failures'][0]['error'])
                    item['status'] = 'done'
                except (OSError, ValueError) as error:
                    # A completed filesystem change remains undoable even when
                    # a later preview refresh or backup cleanup reports an error.
                    if item.get('status') != 'done': item['status'] = 'failed'
                    item['error'] = str(error)
                    failures.append({'name': item['name'], 'error': str(error)})
                self._save(operations)
            self._changed()
            return {'ok': not failures, 'operationId': operation['id'], 'completed': sum(x['status'] == 'done' for x in operation['files']),
                    'failures': failures, 'state': self.app.state()}

    def undo(self, operation_id, confirmed=False):
        with self.app.lock:
            self._idle()
            if getattr(self.app, 'catalog_only', False):
                raise FileToolError('Offline catalogs are read-only.')
            if confirmed is not True: raise FileToolError('Confirm returning these files to their previous paths.')
            operations = self._load()
            operation = next((x for x in operations if x.get('id') == operation_id), None)
            if not operation or operation.get('kind') not in {'rename', 'hardlinks', 'empty-folders'}:
                raise FileToolError('Choose an available operation to undo.')
            failures, completed = [], 0
            for item in reversed(operation['files']):
                if item.get('status') not in {'done', 'pending', 'undo-pending'}: continue
                try:
                    source = Path(item['source'])
                    self._root_for(source)
                    kind = operation['kind']
                    item['status'] = 'undo-pending'
                    self._save(operations)
                    if kind == 'rename':
                        destination = Path(item['destination'])
                        self._root_for(destination)
                        if source.is_file() and not destination.exists():
                            self._verify(item)
                            item['status'] = 'undone'
                            completed += 1
                            self._save(operations)
                            continue
                        if source.exists():
                            raise FileToolError('The original name is occupied. No existing file was overwritten.')
                        check = dict(item, signature=item.get('destinationSignature', item['signature']))
                        self._verify(check, 'destination')
                        _move_without_replace(destination, source)
                        self._moved(destination, source)
                    elif kind == 'hardlinks':
                        backup = Path(item['backup'])
                        self._root_for(backup)
                        if backup.exists():
                            if hash_file(backup) != item['sha256']: raise FileToolError('The recovery copy changed.')
                            if source.exists():
                                self._verify(dict(item, signature=item.get('linkedSignature', item['signature'])))
                                source.unlink()
                            _move_without_replace(backup, source)
                        else:
                            if not item.get('linkedSignature'):
                                # Intent was written, but linking never started.
                                self._verify(item)
                                item['status'] = 'undone'
                                completed += 1
                                self._save(operations)
                                continue
                            self._verify(dict(item, signature=item.get('linkedSignature', item['signature'])))
                            temporary = source.with_name('.media-review-undo-' + uuid.uuid4().hex)
                            with source.open('rb') as incoming, temporary.open('xb') as output:
                                shutil.copyfileobj(incoming, output, 1024 * 1024)
                                output.flush()
                                os.fsync(output.fileno())
                            shutil.copystat(source, temporary, follow_symlinks=False)
                            if (file_signature(source) != tuple(item['linkedSignature']) or hash_file(source) != item['sha256']
                                    or hash_file(temporary) != item['sha256']):
                                temporary.unlink()
                                raise FileToolError('The linked file changed during undo.')
                            # Both names were verified immediately before replacement.
                            os.replace(temporary, source)
                        for photo in self.app.result.photos.values():
                            if photo.file == source: photo.signature = file_signature(source)
                    else:
                        if source.exists() and not source.is_dir(): raise FileToolError('A file occupies the old folder location.')
                        source.mkdir(parents=True, exist_ok=True)
                    item['status'] = 'undone'
                    completed += 1
                except (OSError, ValueError) as error:
                    item['status'] = 'done'
                    failures.append({'name': item.get('name', ''), 'error': str(error)})
                self._save(operations)
            self._changed()
            return {'ok': not failures, 'completed': completed, 'failures': failures, 'state': self.app.state()}
