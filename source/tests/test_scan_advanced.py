"""Generated fixtures for filters, read-only archives, metadata and sound matching."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os
import subprocess
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile
import zlib

import numpy as np
from PIL import Image
import archive_scan
import audio_fingerprint
import scanner
from metadata_match import metadata_match, read_metadata
from scan_options import normalize_options


class AdvancedScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='advanced-scan-')
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch('scanner.classify_image', return_value=([], [], 'empty')))
        self.stack.enter_context(patch('scanner.extract_image_text', return_value=('', 'empty')))
        self.stack.enter_context(patch('scanner.visual_search.embed_image', return_value=None))
        self.stack.enter_context(patch('scanner.visual_search.capabilities', return_value={'available': False}))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def image(self, name, color='red', size=(80, 40), taken=None, camera='Fixture Camera'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new('RGB', size, color)
        exif = Image.Exif()
        if taken:
            exif[36867] = taken
            exif[271] = 'Test'
            exif[272] = camera
        image.save(path, exif=exif)
        return path

    def test_rejects_invalid_and_unknown_options(self):
        for options in ({'unknown': 1}, {'audioMatching': 1}, {'minSize': True}, {'maxSize': -1},
                        {'minSize': 10, 'maxSize': 1}, {'modifiedFrom': '2026-99-99'}, {'modifiedFrom': '20260101'},
                        {'modifiedFrom': '2026-02-01', 'modifiedTo': '2026-01-01'},
                        {'audioThreshold': float('nan')}, {'extensions': ['../png']},
                        {'metadataGpsTolerance': -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_options(options)

    def test_extension_and_folder_globs_filter_before_inspection(self):
        keep = self.image('keep.PNG')
        self.image('other.jpg')
        self.image('private/secret.png')
        (self.root / 'copy.tmp').write_bytes(b'data')
        result = scanner.scan_folder(self.root, options={'extensions': '*.PNG', 'excludePatterns': ['private', '*.tmp']})
        self.assertEqual([p.file for p in result.photos.values()], [keep])

    def test_size_and_inclusive_modified_date_filters(self):
        (self.root / 'small.bin').write_bytes(b'1')
        large = self.root / 'large.bin'
        large.write_bytes(b'12345')
        modified = datetime(2026, 1, 15, 23, 59, 58).timestamp()
        os.utime(large, (modified, modified))
        options = {'minSize': 5, 'maxSize': 5, 'modifiedFrom': '2026-01-15', 'modifiedTo': '2026-01-15'}
        result = scanner.scan_folder(self.root, options=options)
        self.assertEqual([p.file for p in result.photos.values()], [large])

    def test_dimension_filters_are_image_only(self):
        keep = self.image('wide.png', size=(200, 100))
        self.image('narrow.png', size=(100, 100))
        (self.root / 'file.bin').write_bytes(b'content')
        result = scanner.scan_folder(self.root, options={'minWidth': 150, 'minHeight': 80})
        self.assertEqual([p.file for p in result.photos.values()], [keep])

    def test_zip_members_match_local_files_and_are_read_only(self):
        path = self.image('original.png')
        before = path.read_bytes()
        archive = self.root / 'pictures.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('images/copy.png', before)
            z.writestr('note.txt', b'archive note')
        archive_before = archive.read_bytes()
        result = scanner.scan_folder(self.root, options={'archives': True})
        entries = [p for p in result.photos.values() if p.sourceKind == 'archive']
        self.assertEqual(len(entries), 2)
        image = next(p for p in entries if p.archiveMember.endswith('.png'))
        self.assertEqual(image.file, archive)
        self.assertEqual(image.signature, scanner.file_signature(archive))
        self.assertEqual(image.archiveContainer, 'pictures.zip')
        self.assertEqual(image.public()['sourceKind'], 'archive')
        self.assertTrue(any(g['kind'] == 'exact' and image.id in g['photos'] for g in result.groups))
        self.assertEqual(result.stats['duplicateBytes'], 0, 'Virtual archive entries must not inflate reclaimable bytes')
        with archive_scan.materialize_member(archive, image.archiveMember, image.signature) as decoded:
            self.assertEqual(decoded.read_bytes(), before)
        self.assertFalse(decoded.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(archive.read_bytes(), archive_before)

    def test_archive_member_filters_do_not_require_container_extension(self):
        image = self.image('original.png')
        with zipfile.ZipFile(self.root / 'pictures.zip', 'w') as z:
            z.writestr('public/copy.png', image.read_bytes())
            z.writestr('private/copy.png', image.read_bytes())
            z.writestr('note.txt', b'text')
        result = scanner.scan_folder(self.root, options={'archives': True, 'extensions': ['png'], 'excludePatterns': ['private']})
        self.assertEqual(len(result.photos), 2)
        self.assertEqual(sum(p.sourceKind == 'archive' for p in result.photos.values()), 1)

    def test_archive_disabled_does_not_read_members(self):
        with zipfile.ZipFile(self.root / 'pictures.zip', 'w') as z:
            z.writestr('note.txt', b'text')
        result = scanner.scan_folder(self.root)
        self.assertEqual(len(result.photos), 1)
        self.assertTrue(all(p.sourceKind == 'local' for p in result.photos.values()))

    def test_unsafe_nested_and_oversize_archive_entries_are_reported(self):
        archive = self.root / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape.txt', b'outside')
            z.writestr('C:/escape.txt', b'outside')
            z.writestr('nested.zip', b'not read')
            link = zipfile.ZipInfo('link.txt')
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            z.writestr(link, b'target')
            z.writestr('bomb.bin', b'x' * (2 * 1024 * 1024), compress_type=zipfile.ZIP_DEFLATED)
            z.writestr('safe.txt', b'safe')
        members, errors = archive_scan.list_members(archive)
        self.assertEqual([m.name for m in members], ['safe.txt'])
        self.assertEqual(len(errors), 5)
        with self.assertRaises(archive_scan.ArchiveError):
            with archive_scan.materialize_member(archive, '../escape.txt'):
                pass
        self.assertFalse((self.root.parent / 'escape.txt').exists())

    def test_tar_gz_scan_and_link_rejection(self):
        archive = self.root / 'bundle.tar.gz'
        with tarfile.open(archive, 'w:gz') as z:
            item = tarfile.TarInfo('safe.txt')
            item.size = 6
            z.addfile(item, BytesIO(b'hello!'))
            link = tarfile.TarInfo('link')
            link.type, link.linkname = tarfile.SYMTYPE, '../outside'
            z.addfile(link)
        members, errors = archive_scan.list_members(archive)
        self.assertEqual([m.name for m in members], ['safe.txt'])
        self.assertEqual(len(errors), 1)
        result = scanner.scan_folder(self.root, options={'archives': True})
        self.assertEqual(sum(p.sourceKind == 'archive' for p in result.photos.values()), 1)

    def test_stale_archive_signature_refuses_preview(self):
        archive = self.root / 'file.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('file.txt', b'original')
        signature = scanner.file_signature(archive)
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('file.txt', b'changed')
        with self.assertRaises(archive_scan.ArchiveError):
            with archive_scan.materialize_member(archive, 'file.txt', signature):
                pass

    def test_archive_cancel_does_not_extract_or_report_read_failure(self):
        archive = self.root / 'file.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('file.txt', b'original')
        with self.assertRaises(archive_scan.ArchiveCancelled):
            archive_scan.list_members(archive, lambda: True)
        result = scanner.scan_folder(self.root, options={'archives': True}, should_stop=lambda: True)
        self.assertTrue(result.stopped)
        self.assertEqual(result.errors, [])

    def test_metadata_time_camera_matching_is_explicit_suggestion(self):
        self.image('one.jpg', 'red', taken='2026:01:10 12:00:00')
        self.image('two.jpg', 'blue', taken='2026:01:10 12:00:02')
        self.image('far.jpg', 'green', taken='2026:01:10 12:01:00')
        self.image('camera.jpg', 'black', taken='2026:01:10 12:00:00', camera='Different')
        result = scanner.scan_folder(self.root, options={'metadataMatching': True})
        groups = [g for g in result.groups if g['kind'] == 'metadata']
        self.assertEqual(len(groups), 1)
        names = {result.photos[i].name for i in groups[0]['photos']}
        self.assertEqual(names, {'one.jpg', 'two.jpg'})
        self.assertIn('different photographs', groups[0]['reason'])
        self.assertEqual(result.stats['exactGroups'], 0)
        self.assertTrue(all(p.metadata.get('dateTaken') for p in result.photos.values()))

    def test_metadata_matching_requires_real_date_and_selected_gps_criteria(self):
        base = {'dateTaken': '2026-01-10T12:00:00', 'cameraMake': 'A', 'cameraModel': 'B',
                'gpsLatitude': 6.9271, 'gpsLongitude': 79.8612}
        a = SimpleNamespace(mediaType='image', metadata=base)
        b = SimpleNamespace(mediaType='image', metadata=dict(base, gpsLatitude=6.92711))
        self.assertTrue(metadata_match(a, b, normalize_options({'metadataGpsTolerance': 10})))
        b.metadata = dict(base, gpsLatitude=7.0)
        self.assertFalse(metadata_match(a, b, normalize_options({'metadataGpsTolerance': 10})))
        b.metadata = {'cameraMake': 'A', 'cameraModel': 'B'}
        self.assertFalse(metadata_match(a, b, normalize_options()))

    def test_metadata_decode_limits_do_not_discard_exact_file_records(self):
        with patch('metadata_match.Image.open', side_effect=Image.DecompressionBombError('oversized image')):
            self.assertEqual(read_metadata(self.root / 'oversized.jpg'), {})

    @unittest.skipUnless(archive_scan.archive_decoder(), 'Portable 7-Zip decoder required')
    def test_actual_7z_members_match_local_and_encrypted_members_are_skipped(self):
        original = self.image('original.png')
        archive = self.root / 'collection.7z'
        binary = str(archive_scan.archive_decoder())
        subprocess.run([binary, 'a', str(archive), original.name], cwd=self.root,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        before = archive.read_bytes()
        members, errors = archive_scan.list_members(archive)
        self.assertEqual([m.name for m in members], ['original.png'])
        self.assertEqual(errors, [])
        with archive_scan.materialize_member(archive, members[0].name) as decoded:
            self.assertEqual(decoded.read_bytes(), original.read_bytes())
        result = scanner.scan_folder(self.root, options={'archives': True, 'extensions': ['png']})
        self.assertEqual(len(result.photos), 2)
        self.assertTrue(any(g['kind'] == 'exact' for g in result.groups))
        self.assertEqual(archive.read_bytes(), before)
        encrypted = self.root / 'encrypted.7z'
        subprocess.run([binary, 'a', '-pfixturePassword', str(encrypted), original.name], cwd=self.root,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        members, errors = archive_scan.list_members(encrypted)
        self.assertEqual(members, [])
        self.assertTrue(any('Encrypted' in e['message'] for e in errors))

    @unittest.skipUnless(archive_scan.archive_decoder(), 'Portable 7-Zip decoder required')
    def test_actual_stored_rar4_member_and_unsafe_path_rejection(self):
        """Build tiny uncompressed RAR4 records; no proprietary encoder needed."""
        archive = self.root / 'collection.rar'
        original = self.image('original.png')

        def header(payload):
            return struct.pack('<H', zlib.crc32(payload) & 0xffff) + payload

        output = bytearray(b'Rar!\x1a\x07\x00')
        output += header(struct.pack('<BHHHI', 0x73, 0, 13, 0, 0))
        for name, data in [('images/copy.png', original.read_bytes()), ('../escape.txt', b'outside')]:
            encoded = name.encode('utf-8')
            payload = struct.pack('<BHHIIBIIBBHI', 0x74, 0x8000, 32 + len(encoded), len(data), len(data),
                                  2, zlib.crc32(data), 0, 20, 0x30, len(encoded), 0x20) + encoded
            output += header(payload) + data
        output += header(struct.pack('<BHH', 0x7b, 0, 7))
        archive.write_bytes(output)
        before = archive.read_bytes()
        members, errors = archive_scan.list_members(archive)
        self.assertEqual([m.name for m in members], ['images/copy.png'])
        self.assertTrue(any('Unsafe' in e['message'] for e in errors))
        with archive_scan.materialize_member(archive, 'images/copy.png') as decoded:
            self.assertEqual(decoded.read_bytes(), original.read_bytes())
        result = scanner.scan_folder(self.root, options={'archives': True, 'extensions': ['png']})
        self.assertEqual(len(result.photos), 2)
        self.assertTrue(any(g['kind'] == 'exact' for g in result.groups))
        self.assertEqual(archive.read_bytes(), before)


class AcousticFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if audio_fingerprint.ffmpeg_binary() is None:
            raise unittest.SkipTest('Bundled FFmpeg is required for actual acoustic matching tests.')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='audio-fingerprint-')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def recording(self, name, frequencies):
        rate = 22050
        samples = []
        for index, frequency in enumerate(frequencies):
            t = np.arange(rate) / rate
            tone = (0.4 * np.sin(2 * np.pi * frequency * t)
                    + 0.16 * np.sin(2 * np.pi * frequency * 2.03 * t)
                    + 0.08 * np.sin(2 * np.pi * (frequency * 0.5 + 13) * t))
            envelope = np.minimum(1, t * 20) * np.minimum(1, (1 - t) * 20)
            samples.append(tone * envelope)
        output = self.root / name
        with wave.open(str(output), 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes((np.concatenate(samples) * 32767).astype('<i2').tobytes())
        return output

    def test_reencoded_recording_matches_but_different_melody_does_not(self):
        original = self.recording('original.wav', [220, 330, 440, 550, 440, 330, 220, 660])
        different = self.recording('different.wav', [880, 770, 660, 550, 880, 990, 1100, 770])
        encoded = self.root / 'different-name.mp3'
        subprocess.run([str(audio_fingerprint.ffmpeg_binary()), '-hide_banner', '-loglevel', 'error',
                        '-nostdin', '-i', str(original), '-codec:a', 'libmp3lame', '-b:a', '96k', str(encoded)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        records = []
        for path in (original, encoded, different):
            value, duration = audio_fingerprint.fingerprint(path)
            records.append(SimpleNamespace(audioFingerprint=value, audioDuration=duration))
        self.assertGreaterEqual(audio_fingerprint.similarity(records[0], records[1]), 90)
        self.assertLess(audio_fingerprint.similarity(records[0], records[2]), 80)
        result = scanner.scan_folder(self.root, options={'audioMatching': True})
        groups = [g for g in result.groups if g['kind'] == 'audio']
        self.assertEqual(len(groups), 1)
        self.assertEqual({result.photos[i].name for i in groups[0]['photos']}, {'original.wav', 'different-name.mp3'})

    def test_silence_short_audio_and_cancellation_do_not_produce_matches(self):
        short = self.recording('short.wav', [440])
        self.assertEqual(audio_fingerprint.fingerprint(short)[0], ())
        silence = self.root / 'silence.wav'
        with wave.open(str(silence), 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(11025)
            target.writeframes(bytes(11025 * 5 * 2))
        self.assertEqual(audio_fingerprint.fingerprint(silence)[0], ())
        with self.assertRaises(audio_fingerprint.AudioCancelled):
            audio_fingerprint.fingerprint(short, lambda: True)

    def test_actual_audio_decode_allows_only_local_input_protocols(self):
        original = self.recording('local.wav', [220, 330, 440, 550])
        with patch('audio_fingerprint.subprocess.Popen', wraps=subprocess.Popen) as launched:
            landmarks, duration = audio_fingerprint.fingerprint(original)
        self.assertTrue(landmarks)
        self.assertGreaterEqual(duration, 3)
        arguments = launched.call_args.args[0]
        boundary = arguments.index('-protocol_whitelist')
        self.assertLess(boundary, arguments.index('-i'), 'The protocol boundary must apply to the decoder input')
        self.assertEqual(set(arguments[boundary + 1].split(',')), {'file', 'pipe'})


if __name__ == '__main__':
    unittest.main()
