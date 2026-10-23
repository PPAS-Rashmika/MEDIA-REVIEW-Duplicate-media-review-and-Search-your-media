"""Generated, local fixtures for the version-three scan and matching modes."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
import scanner
import video_match
from scan_cache import InspectionCache, photo_from_dict, photo_to_dict
from scan_options import normalize_options

FFMPEG = video_match.ffmpeg_binary() or Path(__file__).resolve().parents[1] / 'bin' / 'ffmpeg.exe'


class V3ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v3-scanner-')
        self.base = Path(self.temp.name)
        self.first, self.second = self.base / 'first', self.base / 'second'
        self.first.mkdir()
        self.second.mkdir()
        self.stack = ExitStack()
        self.stack.enter_context(patch('scanner.classify_image', return_value=([], [], 'empty')))
        self.stack.enter_context(patch('scanner.extract_image_text', return_value=('', 'empty')))
        self.stack.enter_context(patch('scanner.visual_search.embed_image', return_value=None))
        self.stack.enter_context(patch('scanner.visual_search.capabilities', return_value={'available': False}))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def scan(self, roots=None, options=None, **kwargs):
        return scanner.scan_folders(roots or [self.first, self.second], options=options,
                                    cache_directory=self.base / 'cache', **kwargs)

    def image(self, path):
        image = Image.new('RGB', (240, 160), '#112233')
        draw = ImageDraw.Draw(image)
        for y in range(160):
            draw.line((0, y, 239, y), fill=(y, (y * 3) % 256, 200 - y))
        draw.rectangle((10, 15, 90, 75), fill='#cc4422')
        draw.ellipse((130, 80, 210, 150), fill='#2288dd')
        image.save(path)
        return image

    def test_multiple_roots_have_correct_paths_and_roles(self):
        (self.first / 'same.bin').write_bytes(b'identical')
        (self.second / 'copy.bin').write_bytes(b'identical')
        result = self.scan([{'path': str(self.first), 'reference': True}, str(self.second)],
                           {'comparisonScope': 'reference'})
        self.assertEqual(len(result.photos), 2)
        self.assertEqual(len(result.groups), 1)
        first = next(p for p in result.photos.values() if p.sourceRoot == str(self.first))
        self.assertTrue(first.reference)
        self.assertTrue(first.protected)
        self.assertEqual(first.path, 'same.bin')
        self.assertTrue(first.public()['protected'])

    def test_cross_root_scope_omits_internal_only_matches(self):
        for name in ('a.bin', 'b.bin'):
            (self.first / name).write_bytes(b'internal')
        (self.second / 'different.bin').write_bytes(b'other')
        self.assertFalse(self.scan(options={'comparisonScope': 'cross-root'}).groups)
        self.assertTrue(self.scan().groups)

    def test_overlapping_roots_do_not_duplicate_files_and_inherit_protection(self):
        nested = self.first / 'child'
        nested.mkdir()
        (nested / 'a.bin').write_bytes(b'a')
        result = self.scan([{'path': self.first, 'protected': True}, nested])
        self.assertEqual(len(result.photos), 1)
        photo = next(iter(result.photos.values()))
        self.assertEqual(photo.sourceRoot, str(nested))
        self.assertTrue(photo.protected)

    def test_cache_reuses_inspection_and_invalidates_changed_file(self):
        file = self.first / 'file.bin'
        file.write_bytes(b'original')
        with patch('scanner._inspect', wraps=scanner._inspect) as inspect:
            first = self.scan()
            self.assertEqual(inspect.call_count, 1)
        with patch('scanner._inspect', side_effect=AssertionError('Must reuse cache')):
            second = self.scan()
        self.assertEqual(second.stats['cacheHits'], 1)
        self.assertEqual(next(iter(first.photos.values())).sha256, next(iter(second.photos.values())).sha256)
        file.write_bytes(b'changed longer data')
        with patch('scanner._inspect', wraps=scanner._inspect) as inspect:
            third = self.scan()
            self.assertEqual(inspect.call_count, 1)
        self.assertEqual(third.stats['cacheHits'], 0)

    def test_cache_disabled_reanalyzes_and_settings_changes_invalidate(self):
        (self.first / 'file.bin').write_bytes(b'a')
        self.scan()
        with patch('scanner._inspect', wraps=scanner._inspect) as inspect:
            self.scan(options={'scanCache': False})
            self.assertEqual(inspect.call_count, 1)
        result = self.scan(options={'audioTagMatching': True})
        self.assertEqual(result.stats['cacheHits'], 0)

    def test_corrupt_cached_record_is_rebuilt_without_losing_file(self):
        (self.first / 'file.bin').write_bytes(b'original')
        self.scan()
        connection = sqlite3.connect(self.base / 'cache' / 'inspections-v3.sqlite3')
        connection.execute("UPDATE inspections SET body=?", ('{"sample": "invalid"}',))
        connection.commit()
        connection.close()
        result = self.scan()
        self.assertEqual(len(result.photos), 1)
        self.assertFalse(result.errors)
        self.assertEqual(result.stats['cacheHits'], 0)

    def test_file_change_during_cache_lookup_discards_stale_record(self):
        file = self.first / 'file.bin'
        file.write_bytes(b'original')
        self.scan()
        read = InspectionCache.get

        def changed(cache, path, signature):
            photo = read(cache, path, signature)
            file.write_bytes(b'replaced during cache lookup')
            return photo

        with patch.object(InspectionCache, 'get', changed):
            result = self.scan()
        self.assertFalse(result.photos)
        self.assertIn('changed', result.errors[0]['message'])

    def test_cache_excludes_itself_when_inside_root(self):
        (self.first / 'data.bin').write_bytes(b'a')
        result = scanner.scan_folder(self.first, cache_directory=self.first / 'cache')
        self.assertEqual([p.name for p in result.photos.values()], ['data.bin'])

    def test_stopped_scan_retains_committed_cache_for_explicit_restart(self):
        (self.first / 'a.bin').write_bytes(b'a')
        (self.first / 'b.bin').write_bytes(b'b')
        stopped = threading.Event()

        def progress(done, total, phase):
            if phase == 'read' and done == 1:
                stopped.set()

        result = self.scan(should_stop=stopped.is_set, on_progress=progress)
        self.assertTrue(result.stopped)
        self.assertEqual(len(result.photos), 1)
        restarted = self.scan()
        self.assertEqual(restarted.stats['cacheHits'], 1)
        self.assertEqual(len(restarted.photos), 2)

    def test_rotated_and_flipped_photo_suggestions(self):
        image = self.image(self.first / 'original.png')
        image.transpose(Image.Transpose.ROTATE_90).save(self.second / 'rotated.png')
        image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(self.second / 'flipped.png')
        result = self.scan(options={'imageTransforms': True})
        suggestions = [g for g in result.groups if g['kind'] == 'similar']
        self.assertEqual(len(suggestions), 3)
        self.assertTrue(all(g.get('reason') for g in suggestions))

    def test_cropped_photo_suggestion(self):
        image = self.image(self.first / 'original.png')
        image.crop((24, 16, 216, 144)).save(self.second / 'cropped.png')
        result = self.scan(options={'cropMatching': True})
        self.assertTrue(any(g['kind'] == 'similar' and 'cropped' in g.get('reason', '') for g in result.groups))

    def test_folder_matching_uses_names_and_complete_tree(self):
        for root in (self.first, self.second):
            (root / 'nested').mkdir()
            (root / 'nested' / 'file.bin').write_bytes(b'same')
        result = self.scan(options={'folderMatching': True})
        self.assertEqual(len(result.folderGroups), 2)
        self.assertEqual(result.stats['folderGroups'], 2)
        self.assertEqual(result.folderGroups[0]['fileCount'], 1)
        (self.second / 'extra.bin').write_bytes(b'extra')
        result = self.scan(options={'folderMatching': True, 'extensions': ['bin']})
        self.assertEqual(len(result.folderGroups), 1)

    def test_filtered_out_files_prevent_false_complete_folder_match(self):
        for root in (self.first, self.second):
            (root / 'file.bin').write_bytes(b'same')
        (self.second / 'private.txt').write_bytes(b'different')
        result = self.scan(options={'folderMatching': True, 'extensions': ['bin']})
        self.assertFalse(result.folderGroups)

    def test_photo_codec_preserves_bytes_vectors_and_roles(self):
        self.image(self.first / 'file.png')
        photo = next(iter(self.scan(options={'imageTransforms': True}).photos.values()))
        reconstructed = photo_from_dict(photo_to_dict(photo))
        self.assertEqual(reconstructed.transformSample, photo.transformSample)
        self.assertEqual(reconstructed.file, photo.file)
        self.assertEqual(reconstructed.signature, photo.signature)

    def test_new_options_validate(self):
        for options in ({'videoMatching': 1}, {'videoThreshold': 100}, {'comparisonScope': 'unsafe'},
                        {'audioTagFields': []}, {'imageTransforms': 'yes'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_options(options)

    @unittest.skipUnless(FFMPEG.is_file(), 'FFmpeg test runtime unavailable')
    def test_reencoded_video_matches_and_has_timeline(self):
        first, second = self.first / 'movie.mp4', self.second / 'copy.mkv'
        subprocess.run([str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=128x96:rate=10',
                        '-t', '4', '-c:v', 'mpeg4', '-q:v', '2', '-an', str(first)], check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        subprocess.run([str(FFMPEG), '-v', 'error', '-i', str(first), '-vf', 'scale=256:192',
                        '-c:v', 'mpeg4', '-q:v', '3', '-an', str(second)], check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with patch('video_match.ffmpeg_binary', return_value=FFMPEG):
            result = self.scan(options={'videoMatching': True})
        self.assertTrue(any(g['kind'] == 'video' for g in result.groups), result.errors)
        for photo in result.photos.values():
            self.assertEqual(len(photo.videoFrames), 8)
            self.assertEqual(len(photo.public()['videoTimeline']), 8)

    @unittest.skipUnless(FFMPEG.is_file(), 'FFmpeg test runtime unavailable')
    def test_audio_tags_match_across_encodings(self):
        first, second = self.first / 'song.mp3', self.second / 'song.flac'
        for path in (first, second):
            subprocess.run([str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
                            '-metadata', 'title=Fixture song', '-metadata', 'artist=Fixture artist',
                            '-metadata', 'album=Fixture album', str(path)], check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with patch('video_match.ffmpeg_binary', return_value=FFMPEG):
            result = self.scan(options={'audioTagMatching': True})
        self.assertTrue(any(g['kind'] == 'audio-tags' for g in result.groups), result.errors)
        self.assertTrue(all(p.audioTags.get('artist') == 'Fixture artist' for p in result.photos.values()))

    @unittest.skipUnless(FFMPEG.is_file(), 'FFmpeg test runtime unavailable')
    def test_cropped_rotated_and_unrelated_video_frames(self):
        first = self.first / 'movie.mp4'
        subprocess.run([str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=128x96:rate=10',
                        '-t', '4', '-c:v', 'mpeg4', '-q:v', '2', '-an', str(first)], check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for name, transform in (('cropped', 'crop=104:78:12:8,scale=256:192'), ('rotated', 'transpose=1')):
            subprocess.run([str(FFMPEG), '-v', 'error', '-i', str(first), '-vf', transform,
                            '-c:v', 'mpeg4', '-q:v', '3', '-an', str(self.second / (name + '.mp4'))], check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        subprocess.run([str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x96:rate=10',
                        '-t', '4', '-c:v', 'mpeg4', '-q:v', '2', '-an', str(self.second / 'unrelated.mp4')], check=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with patch('video_match.ffmpeg_binary', return_value=FFMPEG):
            result = self.scan(options={'videoMatching': True})
        photos = result.photos
        related = [{photos[i].name for i in group['photos']} for group in result.groups if group['kind'] == 'video']
        self.assertIn({'movie.mp4', 'cropped.mp4'}, related)
        self.assertIn({'movie.mp4', 'rotated.mp4'}, related)
        self.assertFalse(any('unrelated.mp4' in names for names in related))

    @unittest.skipUnless(FFMPEG.is_file(), 'FFmpeg test runtime unavailable')
    def test_black_video_frames_do_not_create_similarity_suggestions(self):
        for root, size in ((self.first, '128x96'), (self.second, '256x192')):
            subprocess.run([str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i',
                            f'color=c=black:size={size}:rate=10', '-t', '4', '-c:v', 'mpeg4',
                            '-an', str(root / 'black.mp4')], check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        with patch('video_match.ffmpeg_binary', return_value=FFMPEG):
            result = self.scan(options={'videoMatching': True})
        self.assertFalse(any(g['kind'] == 'video' for g in result.groups))
        self.assertTrue(all(p.videoStatus == 'insufficient-detail' for p in result.photos.values()))

    def test_missing_or_different_audio_tags_do_not_match(self):
        from types import SimpleNamespace
        first = SimpleNamespace(audioTags={'title': 'Song', 'artist': 'A', 'album': 'B'})
        second = SimpleNamespace(audioTags={'title': 'Song', 'artist': 'A'})
        self.assertFalse(video_match.tags_match(first, second, ['title', 'artist', 'album']))
        self.assertTrue(video_match.tags_match(first, second, ['title', 'artist']))


if __name__ == '__main__':
    unittest.main()
