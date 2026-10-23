"""Validated, JSON-safe settings for an explicitly requested scan."""
from __future__ import annotations

from datetime import date, datetime, time
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re

DEFAULTS = {
    'extensions': [], 'excludePatterns': [], 'minSize': None, 'maxSize': None,
    'modifiedFrom': '', 'modifiedTo': '', 'minWidth': 0, 'minHeight': 0,
    'audioMatching': False, 'audioThreshold': 90, 'archives': False,
    'metadataMatching': False, 'metadataTimeTolerance': 2,
    'metadataGpsTolerance': 0, 'metadataSameCamera': True,
    'scanCache': True, 'imageTransforms': False, 'cropMatching': False,
    'videoMatching': False, 'videoThreshold': 92, 'audioTagMatching': False,
    'audioTagFields': ['artist', 'title', 'album'], 'folderMatching': False,
    'comparisonScope': 'all',
}


def normalize_options(options=None):
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError('Advanced scan settings must be an object.')
    unknown = set(options) - set(DEFAULTS)
    if unknown:
        raise ValueError('Unknown scan setting: ' + ', '.join(sorted(unknown)))
    result = {key: options.get(key, list(value) if isinstance(value, list) else value)
              for key, value in DEFAULTS.items()}
    for key in ('audioMatching', 'archives', 'metadataMatching', 'metadataSameCamera',
                'scanCache', 'imageTransforms', 'cropMatching', 'videoMatching',
                'audioTagMatching', 'folderMatching'):
        if not isinstance(result[key], bool):
            raise ValueError(key + ' must be true or false.')
    for key, maximum in (('minSize', 2**63 - 1), ('maxSize', 2**63 - 1),
                         ('minWidth', 200000), ('minHeight', 200000)):
        value = result[key]
        if value is None and key in ('minSize', 'maxSize'):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(key + ' must be a nonnegative whole number.')
    if result['minSize'] is not None and result['maxSize'] is not None and result['minSize'] > result['maxSize']:
        raise ValueError('Minimum file size must not exceed maximum file size.')
    for key, low, high in (('audioThreshold', 70, 99), ('videoThreshold', 80, 99), ('metadataTimeTolerance', 0, 3600),
                           ('metadataGpsTolerance', 0, 100000)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not low <= value <= high:
            raise ValueError(f'{key} must be between {low} and {high}.')
    for key in ('extensions', 'excludePatterns'):
        value = result[key]
        if isinstance(value, str):
            value = re.split(r'[,;\n]+', value)
        if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) for v in value):
            raise ValueError(key + ' must contain at most 100 text entries.')
        value = list(dict.fromkeys(v.strip().replace('\\', '/').casefold() for v in value if v.strip()))
        if any(len(v) > 512 or '\x00' in v for v in value):
            raise ValueError(key + ' contains an invalid or excessively long entry.')
        if key == 'extensions':
            value = [v.removeprefix('*.').removeprefix('.') for v in value]
            if any(not re.fullmatch(r'[a-z0-9][a-z0-9.+_-]{0,30}', v) for v in value):
                raise ValueError('Enter file extensions such as jpg, png, mp3 or pdf.')
        result[key] = value
    for key in ('modifiedFrom', 'modifiedTo'):
        value = result[key]
        if not isinstance(value, str):
            raise ValueError('Choose a valid modified-date range.')
        try:
            if value:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('Expected an ISO calendar date.')
                date.fromisoformat(value)
        except ValueError as error:
            raise ValueError('Modified dates must use YYYY-MM-DD.') from error
    if result['modifiedFrom'] and result['modifiedTo'] and result['modifiedFrom'] > result['modifiedTo']:
        raise ValueError('The starting modified date must not be after the ending date.')
    if result['comparisonScope'] not in ('all', 'cross-root', 'reference'):
        raise ValueError('Choose all folders, cross-root comparisons, or reference comparisons.')
    fields = result['audioTagFields']
    if (not isinstance(fields, list) or not fields or len(fields) > 7
            or any(not isinstance(key, str) or key not in {'artist', 'title', 'album', 'album_artist', 'date', 'track', 'genre'} for key in fields)):
        raise ValueError('Choose one or more supported audio tags.')
    result['audioTagFields'] = list(dict.fromkeys(fields))
    return result


def excluded(relative, options, directory=False):
    relative = str(relative).replace('\\', '/').strip('/').casefold()
    components = relative.split('/')
    for raw in options['excludePatterns']:
        pattern = raw.strip('/')
        if not pattern:
            continue
        if fnmatchcase(relative, pattern) or fnmatchcase(relative, pattern + '/*'):
            return True
        if '/' not in pattern and any(fnmatchcase(part, pattern) for part in components):
            return True
        if directory and fnmatchcase(relative + '/', raw):
            return True
    return False


def accepts_file(relative, size, modified, options, check_extension=True):
    if excluded(relative, options):
        return False
    extension = PurePosixPath(str(relative).replace('\\', '/')).suffix.lower().lstrip('.')
    if check_extension and options['extensions'] and extension not in options['extensions']:
        return False
    if options['minSize'] is not None and size < options['minSize']:
        return False
    if options['maxSize'] is not None and size > options['maxSize']:
        return False
    if options['modifiedFrom'] or options['modifiedTo']:
        try:
            modified_date = datetime.fromtimestamp(modified).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return False
        if options['modifiedFrom'] and modified_date < options['modifiedFrom']:
            return False
        if options['modifiedTo'] and modified_date > options['modifiedTo']:
            return False
    return True


def accepts_dimensions(photo, options):
    if not options['minWidth'] and not options['minHeight']:
        return True
    return (photo.mediaType == 'image' and photo.width >= options['minWidth']
            and photo.height >= options['minHeight'])
