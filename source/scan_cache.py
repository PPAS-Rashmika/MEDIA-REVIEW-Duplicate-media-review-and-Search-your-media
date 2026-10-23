"""Versioned, bounded inspection cache. Cached records never authorize file changes."""
from __future__ import annotations

import base64
from dataclasses import fields
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

VERSION = 4
MAX_RECORD = 2 * 1024 * 1024


def photo_to_dict(photo):
    result = {}
    for item in fields(photo):
        value = getattr(photo, item.name)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, bytes):
            value = {'bytes': base64.b64encode(value).decode('ascii')}
        result[item.name] = value
    return result


def photo_from_dict(data):
    from scanner import Photo
    if not isinstance(data, dict):
        raise ValueError('Invalid cached media record.')
    allowed = {item.name for item in fields(Photo)}
    values = {key: value for key, value in data.items() if key in allowed}
    values['file'] = Path(values['file'])
    for name in ('sample', 'transformSample'):
        value = values.get(name, b'')
        if isinstance(value, dict):
            value = base64.b64decode(value['bytes'], validate=True)
        expected = 48 * 48 * 3 if name == 'sample' else 64 * 64 * 3
        if not isinstance(value, bytes) or len(value) not in (0, expected):
            raise ValueError('Invalid cached image sample.')
        values[name] = value
    for name in ('signature', 'audioFingerprint', 'visualEmbedding'):
        if values.get(name) is not None:
            values[name] = tuple(values.get(name, ()))
    if len(values.get('audioFingerprint', ())) > 7200 or len(values.get('visualEmbedding') or ()) > 2048:
        raise ValueError('Invalid cached media index.')
    if len(values.get('videoFrames', [])) > 12:
        raise ValueError('Invalid cached video index.')
    if (not isinstance(values.get('id'), str) or not isinstance(values.get('name'), str)
            or not isinstance(values.get('path'), str) or not isinstance(values.get('sha256', ''), str)
            or not re.fullmatch('[0-9a-f]{64}', values.get('sha256', ''))
            or len(values.get('signature', ())) != 4
            or any(not isinstance(v, int) or v < 0 for v in values['signature'])):
        raise ValueError('Invalid cached media identity.')
    for key in ('width', 'height', 'size', 'dhash', 'ahash'):
        if not isinstance(values.get(key, 0), int) or values.get(key, 0) < 0:
            raise ValueError('Invalid cached dimensions or hash.')
    if values.get('mediaType') not in {'image', 'video', 'audio', 'other'}:
        raise ValueError('Invalid cached media type.')
    if values.get('duration') is not None and (not isinstance(values['duration'], (int, float))
                                               or not math.isfinite(values['duration']) or values['duration'] < 0):
        raise ValueError('Invalid cached duration.')
    if not isinstance(values.get('audioTags', {}), dict) or not isinstance(values.get('metadata', {}), dict):
        raise ValueError('Invalid cached metadata.')
    if values.get('visualEmbedding') and any(not isinstance(v, (int, float)) or not math.isfinite(v)
                                            for v in values['visualEmbedding']):
        raise ValueError('Invalid cached visual index.')
    if any(not isinstance(v, int) or not -1 <= v < 2**64 for v in values.get('audioFingerprint', ())):
        raise ValueError('Invalid cached acoustic index.')
    for frame in values.get('videoFrames', []):
        if (not isinstance(frame, dict) or not isinstance(frame.get('hash'), int) or not 0 <= frame['hash'] < 2**64
                or not isinstance(frame.get('time'), (int, float)) or not math.isfinite(frame['time'])
                or not isinstance(frame.get('thumbnail'), str) or len(frame['thumbnail']) > 65536
                or not frame['thumbnail'].startswith('data:image/jpeg;base64,')
                or not isinstance(frame.get('sample'), str)
                or len(base64.b64decode(frame['sample'], validate=True)) != 24 * 24 * 3):
            raise ValueError('Invalid cached video frame.')
    return Photo(**values)


class InspectionCache:
    def __init__(self, directory=None, settings=None):
        directory = Path(directory) if directory is not None else Path(os.environ.get(
            'LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'MediaReview' / 'scan-cache'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'inspections-v3.sqlite3'
        if path.is_symlink():
            raise ValueError('The scan cache cannot be a linked file.')
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.execute('CREATE TABLE IF NOT EXISTS inspections (path TEXT, settings TEXT, signature TEXT, body TEXT, PRIMARY KEY(path, settings))')
        self.settings = hashlib.sha256(json.dumps({'version': VERSION, 'settings': settings or {}},
                                                 sort_keys=True).encode()).hexdigest()
        self.hits = 0

    def get(self, path, signature):
        row = self.connection.execute('SELECT signature, body FROM inspections WHERE path=? AND settings=?',
                                      (os.path.normcase(str(path)), self.settings)).fetchone()
        if row is None or row[0] != json.dumps(list(signature)) or len(row[1]) > MAX_RECORD:
            return None
        try:
            photo = photo_from_dict(json.loads(row[1]))
            if os.path.normcase(str(photo.file)) != os.path.normcase(str(path)) or tuple(photo.signature) != tuple(signature):
                return None
            self.hits += 1
            return photo
        except (TypeError, ValueError, KeyError):
            return None

    def put(self, photo):
        if photo.sourceKind != 'local':
            return
        body = json.dumps(photo_to_dict(photo), ensure_ascii=True, allow_nan=False)
        if len(body) > MAX_RECORD:
            return
        self.connection.execute('INSERT OR REPLACE INTO inspections VALUES (?, ?, ?, ?)',
                                (os.path.normcase(str(photo.file)), self.settings, json.dumps(list(photo.signature)), body))
        self.connection.commit()

    def close(self):
        self.connection.close()
