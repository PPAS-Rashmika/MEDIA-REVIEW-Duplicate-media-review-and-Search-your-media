"""Offline spectral fingerprints for conservative same-recording suggestions.

Decode at most 30 minutes to mono PCM. Match duration and a sequence of spectral
landmarks; filenames, container bytes and tags do not participate in the score.
"""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import numpy as np

SAMPLE_RATE = 11025
MAX_SECONDS = 1800
FRAME = 2048
HOP = 2756
MAX_LANDMARKS = 7200


class AudioCancelled(Exception):
    pass


def _check(should_stop):
    if should_stop and should_stop():
        raise AudioCancelled()


def ffmpeg_binary():
    root = Path(__file__).resolve().parent
    for path in (root / 'bin' / 'ffmpeg.exe', root / 'bin' / 'ffmpeg',
                 Path(sys.executable).parent / 'bin' / 'ffmpeg.exe'):
        if path.is_file() and not path.is_symlink():
            return path
    found = shutil.which('ffmpeg')
    return Path(found) if found else None


def fingerprint(path, should_stop=None):
    binary = ffmpeg_binary()
    if binary is None:
        raise ValueError('Audio matching needs the bundled FFmpeg decoder.')
    _check(should_stop)
    with tempfile.TemporaryDirectory(prefix='media-review-audio-') as directory:
        output = Path(directory) / 'decoded.pcm'
        process = subprocess.Popen([
            str(binary), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-protocol_whitelist', 'file,pipe', '-i', str(path), '-vn', '-map', '0:a:0', '-t', str(MAX_SECONDS),
            '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 's16le', '-y', str(output),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started = time.monotonic()
        try:
            while process.poll() is None:
                _check(should_stop)
                if time.monotonic() - started > 240:
                    raise ValueError('Audio fingerprint preparation exceeded its time limit.')
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            _check(should_stop)
            if process.returncode or not output.is_file():
                raise ValueError('This audio format could not be decoded for sound matching.')
            # FFmpeg's explicit duration/rate/channel limits also bound disk and memory.
            if output.stat().st_size > SAMPLE_RATE * MAX_SECONDS * 2 + 4096:
                raise ValueError('Decoded audio exceeded the safe size limit.')
            samples = np.fromfile(output, dtype='<i2').astype(np.float32) / 32768.0
            duration = len(samples) / SAMPLE_RATE
            if duration < 3 or float(np.sqrt(np.mean(samples * samples))) < 0.001:
                return (), duration
            window = np.hanning(FRAME)
            edges = np.geomspace(80, 5000, 33) * FRAME / SAMPLE_RATE
            edges = np.clip(edges.astype(int), 1, FRAME // 2)
            landmarks = []
            for start in range(0, len(samples) - FRAME, HOP):
                _check(should_stop)
                frame = samples[start:start + FRAME]
                if float(np.sqrt(np.mean(frame * frame))) < 0.001:
                    landmarks.append(-1)
                    continue
                spectrum = np.abs(np.fft.rfft(frame * window))
                # Two separated peaks resist small codec noise around the strongest bin.
                spectrum[:8] = 0
                peak1 = int(np.argmax(spectrum))
                alternate = spectrum.copy()
                alternate[max(0, peak1 - 3):peak1 + 4] = 0
                peak2 = int(np.argmax(alternate))
                bands = [float(np.mean(spectrum[edges[i]:max(edges[i] + 1, edges[i + 1])]))
                         for i in range(32)]
                bits = 0
                for i in range(31):
                    bits = (bits << 1) | (bands[i] > bands[i + 1])
                landmarks.append((bits << 20) | (peak1 << 10) | peak2)
                if len(landmarks) >= MAX_LANDMARKS:
                    break
            return tuple(landmarks), round(duration, 3)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def similarity(left, right):
    a, b = left.audioFingerprint, right.audioFingerprint
    if not a or not b or len(a) < 10 or len(b) < 10:
        return None
    duration_a, duration_b = left.audioDuration, right.audioDuration
    if min(duration_a, duration_b) < 3 or abs(duration_a - duration_b) > max(1.0, min(duration_a, duration_b) * 0.02):
        return None
    # Encoders can add a short leading delay. Test at most two landmark frames.
    best = 0.0
    for offset in range(-2, 3):
        first = a[max(0, offset):]
        second = b[max(0, -offset):]
        count = min(len(first), len(second))
        if count < 10:
            continue
        matches = []
        for x, y in zip(first[:count], second[:count]):
            if x < 0 or y < 0:
                continue
            contrast = 1 - (((x >> 20) ^ (y >> 20)).bit_count() / 31)
            xp, yp = ((x >> 10) & 1023, x & 1023), ((y >> 10) & 1023, y & 1023)
            peak = sum(max(0, 1 - min(abs(p - q) for q in yp) / 4) for p in xp) / 2
            matches.append(0.35 * contrast + 0.65 * peak)
        if len(matches) >= max(10, count // 2):
            best = max(best, sum(matches) / len(matches))
    return round(best * 100, 1)
