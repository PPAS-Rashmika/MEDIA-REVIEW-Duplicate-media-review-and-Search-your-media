"""Bounded local video frame analysis and tag extraction using bundled FFmpeg."""
from __future__ import annotations

import base64
import io
from pathlib import Path
import re
import subprocess
import tempfile
import time

from PIL import Image, ImageChops, ImageStat
from audio_fingerprint import ffmpeg_binary, AudioCancelled


def _run(arguments, should_stop=None, timeout=90):
    binary = ffmpeg_binary()
    if binary is None:
        raise ValueError('This matching mode needs the bundled FFmpeg decoder.')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([str(binary), '-hide_banner', '-nostdin', '-protocol_whitelist', 'file,pipe', *arguments],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                if should_stop and should_stop():
                    raise AudioCancelled()
                if time.monotonic() - started > timeout:
                    raise ValueError('Media analysis exceeded its time limit.')
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            if should_stop and should_stop():
                raise AudioCancelled()
            output.seek(0)
            errors.seek(0)
            return process.returncode, output.read(2 * 1024 * 1024), errors.read(128 * 1024).decode('utf-8', 'replace')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def probe(path, should_stop=None):
    code, body, error = _run(['-i', str(path), '-map_metadata', '0', '-f', 'ffmetadata', 'pipe:1'], should_stop, 30)
    duration = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', error)
    seconds = None
    if duration:
        seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    tags = {}
    if code == 0:
        for line in body.decode('utf-8', 'replace').splitlines():
            if '=' in line and not line.startswith((';', '#', '[')):
                key, value = line.split('=', 1)
                key = key.strip().casefold()
                if key in {'artist', 'title', 'album', 'album_artist', 'date', 'track', 'genre'}:
                    tags[key] = value.strip()[:1024]
    return seconds, tags


def fingerprint(path, should_stop=None):
    duration, _ = probe(path, should_stop)
    if not duration or duration < 1:
        raise ValueError('Video duration could not be read or the clip is too short.')
    # Eight evenly distributed frames bound storage while comparing the entire timeline.
    fps = 8 / duration
    code, raw, error = _run(['-v', 'error', '-i', str(path), '-an', '-vf',
                            f'fps={fps:.12f},scale=64:64:flags=area', '-frames:v', '8',
                            '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], should_stop)
    if code:
        raise ValueError('Video frames could not be decoded.')
    frames = []
    size = 64 * 64 * 3
    for index in range(min(8, len(raw) // size)):
        image = Image.frombytes('RGB', (64, 64), raw[index * size:(index + 1) * size])
        gray = list(image.convert('L').resize((9, 8)).tobytes())
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | (gray[y * 9 + x] > gray[y * 9 + x + 1])
        sample = image.resize((24, 24)).tobytes()
        preview = io.BytesIO()
        image.save(preview, 'JPEG', quality=70)
        frames.append({'time': round((index + 0.5) * duration / 8, 3), 'hash': bits,
                       'sample': base64.b64encode(sample).decode('ascii'),
                       'informative': ImageStat.Stat(image.convert('L')).stddev[0] >= 7,
                       'thumbnail': 'data:image/jpeg;base64,' + base64.b64encode(preview.getvalue()).decode('ascii')})
    return frames, duration


def similarity(left, right, should_stop=None):
    if min(len(left.videoFrames), len(right.videoFrames)) < 4:
        return None
    first_duration, second_duration = left.duration or 0, right.duration or 0
    if not min(first_duration, second_duration) or abs(first_duration - second_duration) > max(1.5, min(first_duration, second_duration) * 0.04):
        return None
    scores = []
    for first, second in zip(left.videoFrames, right.videoFrames):
        if should_stop and should_stop():
            return None
        # Black screens, title-card backgrounds and flat frames carry too little
        # visual evidence to establish that two different videos are related.
        if not first.get('informative', True) or not second.get('informative', True):
            continue
        a = Image.frombytes('RGB', (24, 24), base64.b64decode(first['sample']))
        b = Image.frombytes('RGB', (24, 24), base64.b64decode(second['sample']))
        stats = ImageStat.Stat(ImageChops.difference(a, b))
        score = 100 * (1 - sum(stats.mean) / (3 * 255)) if (first['hash'] ^ second['hash']).bit_count() <= 12 else 0
        if score < 92:
            score = max(score, _transformed_frame_score(first, second, should_stop))
        scores.append(score)
    return round(sum(scores) / len(scores), 1) if len(scores) >= 4 and sum(s >= 85 for s in scores) >= len(scores) * 0.75 else None


def _transformed_frame_score(first, second, should_stop=None):
    """Try orientation changes and modest central crops, retaining a colour gate."""
    a = Image.open(io.BytesIO(base64.b64decode(first['thumbnail'].split(',', 1)[1]))).convert('RGB')
    b = Image.open(io.BytesIO(base64.b64decode(second['thumbnail'].split(',', 1)[1]))).convert('RGB')
    best = 0
    for operation in (None, Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_180, Image.Transpose.ROTATE_270,
                      Image.Transpose.FLIP_LEFT_RIGHT, Image.Transpose.FLIP_TOP_BOTTOM,
                      Image.Transpose.TRANSPOSE, Image.Transpose.TRANSVERSE):
        if should_stop and should_stop():
            return 0
        rotated = a if operation is None else a.transpose(operation)
        for source, target in ((rotated, b), (b, rotated)):
            for fraction in (1.0, 0.8, 0.65):
                width, height = round(source.width * fraction), round(source.height * fraction)
                x, y = (source.width - width) // 2, (source.height - height) // 2
                image = source.crop((x, y, x + width, y + height)).resize((24, 24), Image.Resampling.LANCZOS)
                reference = target.resize((24, 24), Image.Resampling.LANCZOS)
                difference = ImageStat.Stat(ImageChops.difference(image, reference))
                score = 100 * (1 - sum(difference.mean) / (3 * 255))
                # Independent channel agreement avoids averaging away colour changes.
                if max(difference.mean) <= 28:
                    best = max(best, score)
                if best >= 99:
                    return best
    return best


def tags_match(left, right, fields):
    a, b = left.audioTags, right.audioTags
    return bool(fields) and all(isinstance(a.get(key), str) and a[key].strip()
                               and a[key].strip().casefold() == str(b.get(key, '')).strip().casefold()
                               for key in fields)
