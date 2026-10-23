"""Explicit, versioned saved scans. Never executes serialized objects or scans on load."""
from __future__ import annotations

import base64
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets

from scanner import Photo, ScanResult, calculate_stats, file_signature, hash_file, is_reparse

MAX_SESSION_BYTES = 128 * 1024 * 1024
MAX_FILES = 200_000
ID_PATTERN = re.compile(r"[a-f0-9]{32}")


class SessionError(ValueError):
    pass


def default_directory():
    base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local' / 'share'))
    return base / 'Media Review' / 'Saved scans'


def _plain_json(value):
    """JSON's NaN/Infinity extensions are never accepted in a saved index."""
    raise SessionError('The saved scan contains an invalid number.')


def _photo_dict(photo):
    result = {}
    for item in dataclasses.fields(Photo):
        value = getattr(photo, item.name)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, bytes):
            value = {'base64': base64.b64encode(value).decode('ascii')}
        result[item.name] = value
    return result


def _safe_path(root, relative):
    if not isinstance(relative, str) or not relative or len(relative) > 32768:
        raise SessionError('The saved scan contains an invalid file path.')
    clean = relative.replace('\\', '/')
    path = PurePosixPath(clean)
    if path.is_absolute() or any(p in {'', '.', '..'} or ':' in p for p in path.parts):
        raise SessionError('The saved scan contains an unsafe file path.')
    target = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.exists() and is_reparse(current):
            raise SessionError('A saved file or folder is now a linked path.')
    return target


def _bounded_string(data, key, maximum=32768):
    value = data.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise SessionError('The saved scan contains invalid file details.')
    return value


def _decode_photo(data, root, offline=False):
    if not isinstance(data, dict):
        raise SessionError('The saved scan contains an invalid file record.')
    source = data.get('sourceKind', 'local')
    if source not in {'local', 'archive'}:
        raise SessionError('The saved scan contains an unsupported file source.')
    relative = data.get('archiveContainer') if source == 'archive' else data.get('path')
    target = _safe_path(root, relative)
    signature = data.get('signature')
    if (not isinstance(signature, list) or len(signature) != 4 or
            any(type(n) is not int or n < 0 for n in signature)):
        raise SessionError('The saved scan contains an invalid file signature.')
    if not offline and (not target.is_file() or file_signature(target) != tuple(signature)):
        return None
    photo_id = _bounded_string(data, 'id', 64)
    if not re.fullmatch(r'[a-f0-9]{24}', photo_id):
        raise SessionError('The saved scan contains an invalid file identifier.')
    # Path-to-ID binding prevents an edited session from retargeting an entry.
    if source == 'archive':
        from archive_scan import safe_name
        member = _bounded_string(data, 'archiveMember', 2048)
        if not safe_name(member) or data.get('path') != relative + '!/' + member:
            raise SessionError('The saved scan contains an unsafe archive member.')
        identity = os.path.normcase(str(target)) + '!/' + member
    else:
        identity = os.path.normcase(str(target))
    expected = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    if photo_id != expected:
        raise SessionError('A saved file identifier does not match its location.')
    digest = _bounded_string(data, 'sha256', 64)
    if digest and not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise SessionError('The saved scan contains an invalid content signature.')
    if not offline and source == 'local' and digest and hash_file(target) != digest:
        return None
    if not offline and source == 'archive' and digest:
        from archive_scan import materialize_member, ArchiveError
        try:
            with materialize_member(target, member, expected_signature=tuple(signature)) as extracted:
                if hash_file(extracted) != digest:
                    return None
        except (ArchiveError, OSError):
            return None
    args = {}
    field_names = {f.name for f in dataclasses.fields(Photo)}
    for key in field_names:
        if key in data:
            args[key] = data[key]
    args['file'] = target
    args['signature'] = tuple(signature)
    args['id'] = photo_id
    for key in ['name', 'path', 'format', 'issue', 'mime', 'previewMode', 'mediaType',
                'imageTextStatus', 'ocrSearchStatus', 'visualStatus', 'pixel_hash',
                'archiveContainer', 'archiveMember', 'audioStatus']:
        if key in args:
            args[key] = _bounded_string(data, key)
    if args.get('mediaType', 'image') not in {'image', 'video', 'audio', 'other'}:
        raise SessionError('The saved scan contains an invalid media type.')
    for key in ['size', 'width', 'height', 'dhash', 'ahash']:
        value = args.get(key, 0)
        if type(value) is not int or not 0 <= value <= 2**64:
            raise SessionError('The saved scan contains invalid file dimensions or hashes.')
    if source == 'local' and args.get('size') != signature[2]:
        raise SessionError('The saved scan contains an invalid file size.')
    if 'previewAvailable' in args and type(args['previewAvailable']) is not bool:
        raise SessionError('The saved scan contains an invalid preview setting.')
    for key in ['duration', 'audioDuration']:
        value = args.get(key)
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            raise SessionError('The saved scan contains an invalid duration.')
    for sample_key, maximum in [('sample', 48*48*3), ('transformSample', 256*256*3)]:
        sample = args.get(sample_key, {'base64': ''})
        if not isinstance(sample, dict) or set(sample) != {'base64'} or not isinstance(sample['base64'], str):
            raise SessionError('The saved scan contains an invalid image fingerprint.')
        try:
            args[sample_key] = base64.b64decode(sample['base64'], validate=True)
        except Exception as error:
            raise SessionError('The saved image fingerprint is damaged.') from error
        if len(args[sample_key]) > maximum or len(args[sample_key]) not in ({0,48*48*3} if sample_key == 'sample' else {0,64*64*3}):
            raise SessionError('The saved image fingerprint has an invalid size.')
    vector = args.get('visualEmbedding')
    if vector is not None:
        if (not isinstance(vector, list) or len(vector) != 512 or
                any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)):
            raise SessionError('The saved scan contains an invalid visual index.')
        args['visualEmbedding'] = tuple(vector)
    audio = args.get('audioFingerprint')
    if audio is not None:
        if (not isinstance(audio, list) or len(audio) > 100000 or
                any(type(v) is not int or not -1 <= v <= 2**64 for v in audio)):
            raise SessionError('The saved scan contains an invalid audio index.')
        args['audioFingerprint'] = tuple(audio)
    for key in ['imageCategories', 'imageCategoryReasons']:
        values = args.get(key, [])
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or len(v)>4096 for v in values):
            raise SessionError('The saved scan contains invalid image labels.')
    text = args.get('ocrText', '')
    if not isinstance(text, str) or len(text) > 2_000_000:
        raise SessionError('The saved scan contains an invalid text index.')
    if 'metadata' in args and (not isinstance(args['metadata'], dict) or len(json.dumps(args['metadata'])) > 65536):
        raise SessionError('The saved scan contains invalid image metadata.')
    for flag in ['protected', 'reference', 'offline']:
        if flag in args and type(args[flag]) is not bool:
            raise SessionError('The saved scan contains invalid folder roles.')
    frames = args.get('videoFrames', [])
    if not isinstance(frames, list) or len(frames) > 128:
        raise SessionError('The saved video index is invalid.')
    for frame in frames:
        if (not isinstance(frame, dict) or type(frame.get('time')) not in {float, int}
                or not math.isfinite(frame['time']) or frame['time'] < 0
                or type(frame.get('hash')) is not int or not 0 <= frame['hash'] < 2**64
                or not isinstance(frame.get('sample'), str) or len(frame['sample']) > 100000
                or not isinstance(frame.get('thumbnail'), str) or len(frame['thumbnail']) > 100000):
            raise SessionError('The saved video index is invalid.')
        if not frame['thumbnail'].startswith('data:image/jpeg;base64,'):
            raise SessionError('The saved video thumbnail is invalid.')
    tags = args.get('audioTags', {})
    if not isinstance(tags, dict) or len(tags) > 100 or any(not isinstance(k, str) or not isinstance(v, str)
            or len(k) > 100 or len(v) > 4096 for k,v in tags.items()):
        raise SessionError('The saved audio tags are invalid.')
    args['sourceRoot'] = str(root)
    args['offline'] = offline
    if offline:
        args['protected'] = True
        args['previewAvailable'] = False
        args['issue'] = 'Offline catalog entry. Reconnect the drive and scan it to preview or change this file.'
    return Photo(**args)


class SessionStore:
    def __init__(self, directory=None):
        # Constructor is deliberately free of filesystem reads and writes.
        self.directory = Path(directory) if directory is not None else default_directory()

    def _path(self, session_id):
        if not isinstance(session_id, str) or not ID_PATTERN.fullmatch(session_id):
            raise SessionError('Choose a saved scan from the list.')
        return self.directory / (session_id + '.json')

    def _read(self, session_id):
        path = self._path(session_id)
        if is_reparse(self.directory) or is_reparse(path) or path.stat().st_size > MAX_SESSION_BYTES:
            raise SessionError('This saved scan is linked or too large to open.')
        try:
            body = json.loads(path.read_text('utf-8'), parse_constant=_plain_json)
        except (ValueError, UnicodeError) as error:
            raise SessionError('This saved scan is damaged or unsupported.') from error
        if not isinstance(body, dict) or body.get('schema') != 1 or body.get('id') != session_id:
            raise SessionError('This saved scan uses an unsupported format.')
        return body

    @staticmethod
    def summary(body):
        return {key: body.get(key) for key in ['id', 'name', 'savedAt', 'root', 'files', 'stopped']}

    def list(self):
        if not self.directory.exists():
            return []
        if is_reparse(self.directory):
            raise SessionError('Choose an ordinary saved scan directory.')
        result = []
        for path in sorted(self.directory.glob('*.json'))[:1000]:
            if ID_PATTERN.fullmatch(path.stem):
                try:
                    result.append(self.summary(self._read(path.stem)))
                except (SessionError, OSError):
                    continue
        return sorted(result, key=lambda item: str(item.get('savedAt', '')), reverse=True)

    def save(self, name, root, result, settings):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise SessionError('Enter a saved scan name of 1 to 100 characters.')
        if len(result.photos) > MAX_FILES:
            raise SessionError('This scan has too many files to save in one session.')
        session_id = secrets.token_hex(16)
        body = dict(schema=1, id=session_id, name=name.strip(), root=str(root),
                    savedAt=datetime.now(timezone.utc).isoformat(), files=len(result.photos),
                    stopped=result.stopped, settings=settings,
                    photos=[_photo_dict(photo) for photo in result.photos.values()],
                    groups=result.groups, errors=result.errors, folderGroups=getattr(result, 'folderGroups', []))
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(data) > MAX_SESSION_BYTES:
            raise SessionError('This saved scan would exceed the 128 MB session limit.')
        self.directory.mkdir(parents=True, exist_ok=True)
        if is_reparse(self.directory):
            raise SessionError('The saved scan location cannot be a linked directory.')
        destination = self._path(session_id)
        temporary = destination.with_suffix('.writing')
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return self.summary(body)

    def load(self, session_id, offline=False):
        body = self._read(session_id)
        if (not isinstance(body.get('root'), str) or not Path(body['root']).is_absolute() or
                not isinstance(body.get('photos'), list) or len(body['photos']) > MAX_FILES or
                not isinstance(body.get('settings'), dict) or type(body.get('stopped')) is not bool):
            raise SessionError('The saved scan has invalid folder or settings data.')
        root = Path(body['root'])
        if not offline and (not root.is_dir() or is_reparse(root)):
            raise SessionError('The saved media folder is unavailable. Connect its drive or choose a new scan.')
        from workspace import normalize_roots
        roots = normalize_roots(body['settings'].get('roots'), root, require_online=False, allow_nested=True)
        allowed_roots = {os.path.normcase(item['path']): Path(item['path']) for item in roots}
        result = ScanResult(stopped=body['stopped'])
        skipped = 0
        for entry in body['photos']:
            try:
                source_root = entry.get('sourceRoot') or str(root) if isinstance(entry, dict) else str(root)
                record_root = allowed_roots.get(os.path.normcase(source_root)) if isinstance(source_root, str) else None
                if record_root is None:
                    raise SessionError('A saved file refers to a folder outside the saved workspace.')
                photo = _decode_photo(entry, record_root, offline=offline)
            except OSError:
                photo = None
            if photo is None:
                skipped += 1
            elif photo.id in result.photos:
                raise SessionError('The saved scan contains duplicate file identifiers.')
            else:
                result.photos[photo.id] = photo
        groups = body.get('groups')
        if not isinstance(groups, list) or len(groups) > MAX_FILES * 10:
            raise SessionError('The saved scan contains an invalid comparison list.')
        seen = set()
        for group in groups:
            if (not isinstance(group, dict) or group.get('kind') not in {'exact','pixels','similar','audio','metadata','video','audio-tags'} or
                    not isinstance(group.get('photos'), list) or len(group['photos']) > MAX_FILES or
                    any(not isinstance(value, str) for value in group['photos']) or
                    not isinstance(group.get('id'), str) or not re.fullmatch(r'[a-f0-9]{24}', group['id']) or
                    type(group.get('score')) not in {int,float} or not math.isfinite(group['score']) or
                    not 0 <= group['score'] <= 100 or group['id'] in seen):
                raise SessionError('The saved scan contains an invalid comparison group.')
            seen.add(group['id'])
            members = list(dict.fromkeys(member for member in group['photos'] if member in result.photos))
            if len(members) >= 2:
                result.groups.append(dict(group, photos=members))
        errors = body.get('errors', [])
        if not isinstance(errors, list) or len(errors) > MAX_FILES:
            raise SessionError('The saved scan contains an invalid notice list.')
        result.errors = [dict(name=str(item.get('name', ''))[:32768], message=str(item.get('message', ''))[:65536])
                         for item in errors if isinstance(item, dict)]
        folder_groups = body.get('folderGroups', [])
        if not isinstance(folder_groups, list) or len(folder_groups) > MAX_FILES:
            raise SessionError('The saved folder comparison list is invalid.')
        for group in folder_groups:
            if (not isinstance(group, dict) or not isinstance(group.get('id'), str)
                    or not re.fullmatch('[a-f0-9]{24}', group['id']) or group.get('kind') != 'folder'
                    or not isinstance(group.get('folders'), list) or not 2 <= len(group['folders']) <= MAX_FILES
                    or type(group.get('fileCount')) is not int or group['fileCount'] < 0
                    or type(group.get('totalSize')) is not int or group['totalSize'] < 0):
                raise SessionError('A saved folder comparison is invalid.')
            folders = []
            for folder in group['folders']:
                if not isinstance(folder, dict) or not isinstance(folder.get('sourceRoot'), str) or not isinstance(folder.get('path'), str):
                    raise SessionError('A saved folder location is invalid.')
                source_root = allowed_roots.get(os.path.normcase(folder['sourceRoot']))
                target = Path(folder['path'])
                if source_root is None or not target.is_absolute() or not target.is_relative_to(source_root):
                    raise SessionError('A saved folder is outside the saved workspace.')
                if '..' in target.parts:
                    raise SessionError('A saved folder contains an unsafe path.')
                if not offline and (not target.is_dir() or is_reparse(target)):
                    continue
                role = next(item for item in roots if os.path.normcase(item['path']) == os.path.normcase(str(source_root)))
                folders.append(dict(folder, protected=offline or role['protected'], reference=role['reference']))
            if len(folders) >= 2 and not skipped:
                result.folderGroups.append(dict(group, folders=folders))
        if skipped:
            result.stopped = True
            result.errors.append({'name':'Saved scan', 'message':f'{skipped} changed, missing or linked files were skipped. Scan again for a complete current library.'})
        result.stats = calculate_stats(result.photos, result.groups)
        result.stats['folderGroups'] = len(result.folderGroups)
        return root, result, body['settings'], self.summary(body), skipped
