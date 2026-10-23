"""EXIF comparisons are review suggestions, never proof of identical content."""
from __future__ import annotations

from datetime import datetime
import math
from PIL import Image


def _text(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    return str(value or '').strip().replace('\x00', '')[:256]


def _coordinate(values, ref):
    if not isinstance(values, (tuple, list)) or len(values) != 3:
        return None
    try:
        coordinate = float(values[0]) + float(values[1]) / 60 + float(values[2]) / 3600
        if _text(ref).upper() in ('S', 'W'):
            coordinate = -coordinate
        return coordinate if math.isfinite(coordinate) else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def read_metadata(path):
    metadata = {}
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            try:
                detail = exif.get_ifd(34665)
            except (KeyError, ValueError, TypeError, OSError):
                detail = {}
            for source in (detail.get(36867), exif.get(36867), detail.get(36868), exif.get(306)):
                if source:
                    try:
                        taken = datetime.strptime(_text(source)[:19], '%Y:%m:%d %H:%M:%S')
                        metadata['dateTaken'] = taken.isoformat()
                        break
                    except ValueError:
                        pass
            for name, code in (('cameraMake', 271), ('cameraModel', 272)):
                value = _text(exif.get(code))
                if value:
                    metadata[name] = value
            try:
                gps = exif.get_ifd(34853)
            except (KeyError, ValueError, TypeError, OSError):
                gps = {}
            latitude = _coordinate(gps.get(2), gps.get(1))
            longitude = _coordinate(gps.get(4), gps.get(3))
            if latitude is not None and longitude is not None and abs(latitude) <= 90 and abs(longitude) <= 180:
                metadata.update(gpsLatitude=latitude, gpsLongitude=longitude)
    except Exception:
        # Malformed EXIF or decoder safety limits must not discard a file's
        # already computed exact hash. No scan callbacks execute in this helper.
        pass
    return metadata


def _distance(a, b):
    lat1, lat2 = math.radians(a['gpsLatitude']), math.radians(b['gpsLatitude'])
    lat = lat2 - lat1
    lon = math.radians(b['gpsLongitude'] - a['gpsLongitude'])
    h = math.sin(lat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(lon / 2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def metadata_match(left, right, options):
    a, b = left.metadata, right.metadata
    if left.mediaType != 'image' or right.mediaType != 'image' or not a.get('dateTaken') or not b.get('dateTaken'):
        return False
    try:
        difference = abs((datetime.fromisoformat(a['dateTaken']) - datetime.fromisoformat(b['dateTaken'])).total_seconds())
        if difference > options['metadataTimeTolerance']:
            return False
        if options['metadataSameCamera']:
            camera_a = (a.get('cameraMake', '').casefold(), a.get('cameraModel', '').casefold())
            camera_b = (b.get('cameraMake', '').casefold(), b.get('cameraModel', '').casefold())
            if not any(camera_a) or camera_a != camera_b:
                return False
        if options['metadataGpsTolerance']:
            if not all(k in m for m in (a, b) for k in ('gpsLatitude', 'gpsLongitude')):
                return False
            if _distance(a, b) > options['metadataGpsTolerance']:
                return False
    except (ValueError, TypeError, OverflowError):
        return False
    return True
