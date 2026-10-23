"""Saved-index and real pause/resume tests use generated disposable files only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
import app
import scanner
from session_store import SessionStore, SessionError
from tests.test_app import ApiFixture


class SavedScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'media'
        self.root.mkdir()
        self.directory = self.base / 'saved'
        self.review = app.ReviewApp(self.root, session_directory=self.directory)

    def tearDown(self):
        self.review.close()
        self.temp.cleanup()

    def generated(self):
        for name, data in [('a.txt', b'generated duplicate'), ('b.txt', b'generated duplicate'),
                           ('c.txt', b'generated unique')]:
            (self.root/name).write_bytes(data)

    def scan(self, **kwargs):
        self.review.start_scan(str(self.root), **kwargs)
        self.review.scan_thread.join(10)
        self.assertFalse(self.review.scanning)
        self.assertEqual(self.review.progress['phase'], 'complete', self.review.state())

    def test_constructor_and_session_list_never_scan_or_create_directory(self):
        with patch('app.scan_folder') as scan:
            self.assertEqual(self.review.sessions()['sessions'], [])
            self.assertFalse(self.directory.exists())
            self.assertFalse(self.review.has_scanned)
            scan.assert_not_called()

    def test_save_reopen_restores_results_options_and_private_search_index(self):
        self.generated()
        self.scan(options={'extensions':['txt'], 'minSize':3})
        original = {p.name: p.file.read_bytes() for p in self.review.result.photos.values()}
        photo = next(iter(self.review.result.photos.values()))
        photo.ocrText = 'Generated private words 123'
        photo.visualEmbedding = tuple([1.0] + [0.0]*511)
        photo.audioFingerprint = (-1, 12, 16)
        saved = self.review.save_session('Generated test')
        self.assertEqual(len(saved['sessions']), 1)
        other = app.ReviewApp(session_directory=self.directory)
        try:
            with patch('app.scan_folder') as scan:
                state = other.load_session(saved['session']['id'])
                scan.assert_not_called()
            self.assertEqual(state['stats']['total'], 3)
            self.assertEqual(state['stats']['exactGroups'], 1)
            self.assertEqual(state['scanOptions']['extensions'], ['txt'])
            self.assertFalse(state['scanPaused'])
            restored = other.result.photos[photo.id]
            self.assertEqual(restored.ocrText, photo.ocrText)
            self.assertEqual(restored.visualEmbedding, photo.visualEmbedding)
            self.assertEqual(restored.audioFingerprint, (-1,12,16))
            self.assertNotIn('ocrText', state['photos'][0])
            self.assertEqual(state['sessionLoad']['skipped'], 0)
        finally:
            other.close()
        for name, data in original.items():
            self.assertEqual((self.root/name).read_bytes(), data)

    def test_changed_missing_and_same_signature_changed_bytes_are_skipped(self):
        self.generated()
        self.scan()
        saved = self.review.save_session('Stale fixture')['session']
        modified = self.root / 'a.txt'
        info = modified.stat()
        modified.write_bytes(b'x'*info.st_size)
        os.utime(modified, ns=(info.st_atime_ns, info.st_mtime_ns))
        (self.root/'b.txt').unlink()
        state = self.review.load_session(saved['id'])
        self.assertEqual(state['stats']['total'], 1)
        self.assertEqual(state['groups'], [])
        self.assertEqual(state['sessionLoad']['skipped'], 2)
        self.assertTrue(state['scanStopped'])

    def test_invalid_session_ids_and_traversal_records_are_rejected_without_replacing_review(self):
        self.generated()
        self.scan()
        saved = self.review.save_session('Tamper fixture')['session']
        for value in ['../escape', '', None, 'x'*32]:
            with self.subTest(value=value), self.assertRaises((SessionError,OSError)):
                self.review.load_session(value)
        path = self.directory / (saved['id']+'.json')
        payload = json.loads(path.read_text('utf-8'))
        payload['photos'][0]['path'] = '../outside.txt'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaises(SessionError):
            self.review.load_session(saved['id'])
        self.assertEqual(self.review.result.stats['total'], 3)
        self.assertFalse(self.review.session_busy)

    def test_archive_member_preview_persistence_and_mutation_protection(self):
        picture = io.BytesIO()
        Image.new('RGB', (30,24), 'blue').save(picture, 'PNG')
        (self.root/'local.png').write_bytes(picture.getvalue())
        with zipfile.ZipFile(self.root/'bundle.zip','w') as archive:
            archive.writestr('inside.png', picture.getvalue())
        with patch('scanner.extract_image_text', return_value=('', 'empty')), \
                patch('scanner.visual_search.embed_image', return_value=None), \
                patch('scanner.classify_image', return_value=([],[], 'no-text')):
            self.scan(options={'archives':True})
        archived = next(p for p in self.review.result.photos.values() if p.sourceKind=='archive')
        local = next(p for p in self.review.result.photos.values() if p.name=='local.png')
        body, mime = self.review.photo(archived.id)
        self.assertEqual(mime, 'image/jpeg')
        self.assertTrue(body.startswith(b'\xff\xd8'))
        group = next(g for g in self.review.result.groups if archived.id in g['photos'])
        with self.assertRaises(app.ReviewError):
            self.review.delete(archived.id, group['id'], local.id)
        with self.assertRaises(app.ReviewError):
            self.review.delete(local.id, group['id'], archived.id)
        plan = self.review.bulk_delete_plan([group['id']])
        self.assertEqual(plan['count'], 0)
        self.assertEqual(plan['skippedArchiveGroups'], 1)
        organized = self.review.organize_plan('type')
        self.assertFalse(any('!/' in item['path'] for item in organized['files']))
        saved = self.review.save_session('Archive fixture')['session']
        state = self.review.load_session(saved['id'])
        self.assertEqual(state['sessionLoad']['skipped'], 0)
        self.assertEqual(self.review.photo(archived.id)[1], 'image/jpeg')

    def test_archive_container_move_and_undo_retarget_member_previews_and_saved_results(self):
        picture = io.BytesIO()
        Image.new('RGB', (24,20), 'green').save(picture, 'PNG')
        with zipfile.ZipFile(self.root/'bundle.zip','w') as archive:
            archive.writestr('inside.png', picture.getvalue())
        with patch('scanner.extract_image_text', return_value=('', 'empty')), \
                patch('scanner.visual_search.embed_image', return_value=None), \
                patch('scanner.classify_image', return_value=([],[], 'no-text')):
            self.scan(options={'archives':True})
        archived = next(p for p in self.review.result.photos.values() if p.sourceKind=='archive')
        original_id = archived.id
        plan = self.review.organize_plan('type')
        operation = self.review.organize_execute(plan['planId'])
        self.review.organization_thread.join(10)
        self.assertFalse(self.review.organization['running'])
        self.assertNotEqual(archived.id, original_id)
        self.assertEqual(archived.file, self.root/'Organized by type'/'ZIP'/'bundle.zip')
        self.assertEqual(self.review.photo(archived.id)[1], 'image/jpeg')
        saved = self.review.save_session('Moved archive')['session']
        self.review.load_session(saved['id'])
        self.assertEqual(self.review.photo(archived.id)[1], 'image/jpeg')
        self.review.organize_undo(operation['operationId'])
        self.review.organization_thread.join(10)
        self.assertEqual(self.review.photo(original_id)[1], 'image/jpeg')

    def test_bulk_removing_archive_container_removes_its_virtual_children_and_restores_container(self):
        with zipfile.ZipFile(self.root/'first.zip','w') as archive:
            archive.writestr('inside.txt', b'Generated archived fixture')
        (self.root/'second.zip').write_bytes((self.root/'first.zip').read_bytes())
        self.scan(options={'archives':True})
        self.assertEqual(len(self.review.result.photos),4)
        containers = {p.id for p in self.review.result.photos.values() if p.sourceKind=='local'}
        group = next(g for g in self.review.result.groups if set(g['photos'])==containers)
        plan = self.review.bulk_delete_plan([group['id']])
        self.assertEqual(plan['count'],1)
        result = self.review.bulk_delete(plan['planId'],True)
        self.assertEqual(result['deletedCount'],1)
        self.assertEqual(len(self.review.result.photos),2)
        for photo in self.review.result.photos.values():
            self.assertTrue(photo.file.is_file())
        self.review.restore(result['deleted'][0]['id'])
        self.assertEqual(len(self.review.result.photos),3)
        self.assertTrue(any('include its members' in e['message'] for e in self.review.result.errors))


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'a.txt').write_bytes(b'generated pause file')
        (self.root/'b.txt').write_bytes(b'generated pause file')
        self.review = app.ReviewApp(self.root, session_directory=self.root/'saved')
        self.entered, self.release = threading.Event(), threading.Event()

    def tearDown(self):
        self.release.set()
        self.review.close()
        self.temp.cleanup()

    def start_gated(self):
        inspect = scanner._inspect
        def controlled(path, root, *args, **kwargs):
            if path.name=='a.txt':
                self.entered.set()
                self.release.wait(5)
            return inspect(path, root, *args, **kwargs)
        patcher = patch('scanner._inspect', side_effect=controlled)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.review.start_scan(str(self.root))
        self.assertTrue(self.entered.wait(3))

    def wait_paused(self):
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            state = self.review.state()
            if state['scanPaused']:
                return state
            time.sleep(.01)
        self.fail('Worker did not acknowledge Pause')

    def test_pause_resume_preserves_worker_and_finishes_without_restart(self):
        self.start_gated()
        worker = self.review.scan_thread
        self.assertTrue(self.review.pause_scan()['accepted'])
        self.assertFalse(self.review.pause_scan()['accepted'])
        self.release.set()
        paused = self.wait_paused()
        self.assertEqual(paused['progress']['phase'], 'paused')
        self.assertTrue(paused['scanning'])
        self.assertTrue(worker.is_alive())
        self.assertEqual(len(self.review.result.photos), 0)
        with self.assertRaises(app.ReviewError):
            self.review.start_scan(str(self.root))
        with self.assertRaises(app.ReviewError):
            self.review.save_session('Busy')
        self.assertTrue(self.review.resume_scan()['accepted'])
        worker.join(5)
        self.assertIs(worker, self.review.scan_thread)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.review.result.stats['total'],2)
        self.assertFalse(self.review.result.stopped)
        self.assertFalse(self.review.resume_scan()['accepted'])

    def test_stop_while_paused_unblocks_worker_and_allows_fresh_scan(self):
        self.start_gated()
        self.review.pause_scan()
        self.release.set()
        self.wait_paused()
        self.assertTrue(self.review.stop_scan()['accepted'])
        self.review.scan_thread.join(5)
        self.assertFalse(self.review.scanning)
        self.assertTrue(self.review.result.stopped)
        self.assertFalse(self.review.resume_scan()['accepted'])
        self.review.start_scan(str(self.root))
        self.review.scan_thread.join(5)
        self.assertEqual(self.review.result.stats['total'],2)
        self.assertFalse(self.review.result.stopped)

    def test_close_while_paused_releases_worker_without_publishing(self):
        self.start_gated()
        self.review.pause_scan()
        self.release.set()
        self.wait_paused()
        worker = self.review.scan_thread
        self.review.close()
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.review.scanning)
        self.assertEqual(self.review.result.photos,{})


class AdvancedRoutesTests(ApiFixture):
    def setUp(self):
        super().setUp()
        self.server.app.session_store = SessionStore(self.root/'saved-scans')

    def test_new_mutation_routes_require_token_origin_and_json(self):
        paths = ['/api/scan/pause','/api/scan/resume','/api/sessions/list','/api/sessions/save',
                 '/api/sessions/load','/api/cloud/connect','/api/cloud/answer','/api/cloud/cancel',
                 '/api/cloud/folders','/api/cloud/scan','/api/cloud/stop','/api/cloud/open']
        with patch.object(self.server.app.cloud, 'connect') as connect, \
                patch.object(self.server.app.cloud, 'start_scan') as scan:
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(self.request('POST',path,{},authorized=False)[0],403)
                    self.assertEqual(self.request('POST',path,{},headers={'Origin':'https://invalid.example'})[0],403)
                    self.assertEqual(self.request('POST',path,{},headers={'Content-Type':'text/plain'})[0],415)
            connect.assert_not_called()
            scan.assert_not_called()
        self.assertFalse((self.root/'saved-scans').exists())

    def test_session_http_round_trip_never_starts_a_new_scan(self):
        (self.root/'a.txt').write_bytes(b'generated route fixture')
        state = self.scan()
        self.assertEqual(state['stats']['total'],1)
        status, saved = self.request('POST','/api/sessions/save',{'name':'HTTP fixture'})
        self.assertEqual(status,200,saved)
        with patch('app.scan_folder') as scan:
            status, reopened = self.request('POST','/api/sessions/load',{'id':saved['session']['id']})
            self.assertEqual(status,200,reopened)
            scan.assert_not_called()
        self.assertEqual(reopened['stats']['total'],1)
        self.assertFalse(reopened['scanning'])
        for path in ['/api/scan/pause','/api/scan/resume']:
            status, idle = self.request('POST',path,{})
            self.assertEqual(status,200,idle)
            self.assertFalse(idle['accepted'])

    def test_cloud_routes_dispatch_only_after_explicit_request_and_never_return_secrets(self):
        with patch.object(self.server.app.cloud,'connect', return_value={'ok':True}) as connect:
            self.assertEqual(self.request('POST','/api/cloud/connect',{
                'provider':'drive','label':'Generated account','clientId':'fixture-client','clientSecret':'fixture-secret'})[0],200)
            connect.assert_called_once_with('drive','Generated account','fixture-client','fixture-secret')
        with patch.object(self.server.app.cloud,'folders',return_value={'folders':[]}) as folders:
            self.assertEqual(self.request('POST','/api/cloud/folders',{'accountId':'fixture-id','path':'Photos'})[0],200)
            folders.assert_called_once_with('fixture-id','Photos')
        for route in ['/static/advanced.js','/static/cloud.js','/static/cloud.css']:
            self.assertEqual(self.request('GET',route)[0],200,route)
