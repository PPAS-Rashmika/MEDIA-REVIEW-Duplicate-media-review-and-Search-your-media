"""Generated-fixture runtime checks shared by source and packaged builds."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os
import subprocess
import struct
import threading
import time
import wave
import zipfile
import zlib

import numpy as np
from PIL import Image


def run(parent):
    from app import ReviewApp
    from scanner import scan_folder
    import audio_fingerprint
    from archive_scan import archive_decoder
    from cloud_scan import _binary

    root = Path(parent) / 'advanced-check'
    root.mkdir()
    media = root / 'media'
    media.mkdir()
    for name, color, taken in [('capture-a.jpg', 'red', '2026:01:10 12:00:00'),
                               ('capture-b.jpg', 'blue', '2026:01:10 12:00:01')]:
        exif = Image.Exif()
        exif[271], exif[272], exif[36867] = 'Generated', 'Fixture Camera', taken
        Image.new('RGB', (80, 60), color).save(media / name, exif=exif)
    (media / 'copy-a.txt').write_text('Generated duplicate fixture only', 'utf-8')
    (media / 'copy-b.txt').write_text('Generated duplicate fixture only', 'utf-8')
    with zipfile.ZipFile(media / 'samples.zip', 'w') as archive:
        archive.write(media / 'capture-a.jpg', 'pictures/capture-a.jpg')
        archive.writestr('../unsafe.txt', 'This entry must never be extracted')
    decoder = archive_decoder()
    assert decoder is not None, 'Bundled RAR/7z decoder is missing'
    subprocess.run([str(decoder), 'a', '-t7z', '-mx=1', str(media / 'samples.7z'), 'copy-a.txt'],
                   cwd=media, capture_output=True, check=True, timeout=20,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def rar_header(payload):
        return struct.pack('<H', zlib.crc32(payload) & 0xffff) + payload

    rar = bytearray(b'Rar!\x1a\x07\x00')
    rar += rar_header(struct.pack('<BHHHI', 0x73, 0, 13, 0, 0))
    name, data = b'copy-a.txt', (media / 'copy-a.txt').read_bytes()
    payload = struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(name), len(data), len(data),
                          2, zlib.crc32(data), 0, 20, 0x30, len(name), 0x20) + name
    rar += rar_header(payload) + data + rar_header(struct.pack('<BHH', 0x7b, 0, 7))
    (media / 'samples.rar').write_bytes(rar)

    def recording(name, notes):
        rate = 22050
        frames = []
        for frequency in notes:
            t = np.arange(rate) / rate
            tone = (0.4 * np.sin(2 * np.pi * frequency * t)
                    + 0.16 * np.sin(2 * np.pi * frequency * 2.03 * t)
                    + 0.08 * np.sin(2 * np.pi * (frequency * 0.5 + 13) * t))
            frames.append(tone * np.minimum(1, t * 20) * np.minimum(1, (1 - t) * 20))
        destination = media / name
        with wave.open(str(destination), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes((np.concatenate(frames) * 32767).astype('<i2').tobytes())
        return destination

    first = recording('original.wav', [220, 330, 440, 550, 440, 330, 220, 660])
    different = recording('different.wav', [880, 770, 660, 550, 880, 990, 1100, 770])
    encoded = media / 'encoded.mp3'
    subprocess.run([str(audio_fingerprint.ffmpeg_binary()), '-hide_banner', '-loglevel', 'error',
                    '-nostdin', '-i', str(first), '-codec:a', 'libmp3lame', '-b:a', '96k', str(encoded)],
                   check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    records = []
    for filename in (first, encoded, different):
        fingerprint, seconds = audio_fingerprint.fingerprint(filename)
        records.append(SimpleNamespace(audioFingerprint=fingerprint, audioDuration=seconds))
    same_score = audio_fingerprint.similarity(records[0], records[1])
    different_score = audio_fingerprint.similarity(records[0], records[2])
    assert same_score >= 90 and different_score < 80, (same_score, different_score)
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in media.iterdir()}
    settings = {'archives': True, 'audioMatching': True, 'metadataMatching': True}
    app = ReviewApp(session_directory=root / 'sessions')
    loaded_app = ReviewApp(session_directory=root / 'sessions')
    entered, release = threading.Event(), threading.Event()
    original_progress = app._progress

    def progress(done, total, phase, *args, **kwargs):
        original_progress(done, total, phase, *args, **kwargs)
        if phase == 'read' and done == 1:
            entered.set()
            release.wait(20)

    try:
        app._progress = progress
        app.start_scan(media, options=settings)
        assert entered.wait(30), 'Advanced scan did not reach the pause checkpoint'
        assert app.pause_scan()['accepted']
        release.set()
        until = time.monotonic() + 20
        while time.monotonic() < until and not app.state()['scanPaused']:
            time.sleep(0.02)
        paused = app.state()
        assert paused['scanPaused'] and paused['scanning'] and not paused['stopRequested'], paused
        assert app.resume_scan()['accepted']
        app.scan_thread.join(90)
        state = app.state()
        assert not state['scanning'] and state['progress']['phase'] == 'complete', state
        kinds = {group['kind'] for group in state['groups']}
        assert {'exact', 'audio', 'metadata'} <= kinds, kinds
        members = [photo for photo in app.result.photos.values() if photo.sourceKind == 'archive']
        assert len(members) == 3
        image_member = next(photo for photo in members if photo.mediaType == 'image')
        preview, content_type = app.photo(image_member.id, 'thumb')
        assert preview and content_type.startswith('image/')
        assert app.search('capture-a', mode='filename')['total'] == 2
        saved = app.save_session('Generated advanced runtime check')
        assert loaded_app.state()['hasScanned'] is False
        restored = loaded_app.load_session(saved['session']['id'])
        assert restored['stats']['total'] == state['stats']['total'] and restored['groups'] == state['groups']
        assert restored['scanOptions']['audioMatching'] and not restored['scanning']
        assert loaded_app.search('capture-a', mode='filename')['total'] == 2
        filtered = scan_folder(media, options={'extensions': ['jpg'], 'excludePatterns': ['capture-b.jpg'],
                                               'minWidth': 40, 'minHeight': 40})
        assert [photo.name for photo in filtered.photos.values()] == ['capture-a.jpg']
        assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in media.iterdir()}
        assert not (root / 'unsafe.txt').exists()
        binary = _binary()
        assert binary is not None, 'Bundled cloud adapter is missing'
        cloud_version = subprocess.run([str(binary), 'version'], capture_output=True, text=True,
                                       timeout=20, check=True,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.splitlines()[0]
        return dict(pauseResume=True, savedScanReopened=True, advancedFilters=True,
                    acousticMatching=True, audioSameScore=same_score, audioDifferentScore=different_score,
                    archiveMemberPreview=True, metadataSuggestions=True, cloudAdapter=cloud_version,
                    archiveFormatsVerified=['ZIP', 'RAR', '7z'],
                    cloudLiveSignInTested=False, generatedFilesUnchanged=True)
    finally:
        release.set()
        app.close()
        loaded_app.close()
