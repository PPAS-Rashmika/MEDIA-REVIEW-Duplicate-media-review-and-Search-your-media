"""Cross-feature regression checks using disposable local files only."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import ReviewApp, ReviewError
from session_store import SessionError
from storage import StorageError
from workspace import normalize_roots


class WorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.a, self.b = self.base/'drive-a', self.base/'drive-b'
        self.a.mkdir(); self.b.mkdir()
        (self.a/'original.txt').write_bytes(b'Generated exact duplicate contents')
        (self.b/'copy.txt').write_bytes(b'Generated exact duplicate contents')
        self.review = ReviewApp(session_directory=self.base/'sessions')

    def tearDown(self):
        self.review.close()
        self.temp.cleanup()

    def scan(self, **kwargs):
        roots = kwargs.pop('roots', [{'path':str(self.a)},{'path':str(self.b)}])
        self.review.start_scan(roots=roots, **kwargs)
        self.review.scan_thread.join(15)
        self.assertFalse(self.review.scanning)
        self.assertEqual(self.review.progress['phase'], 'complete', self.review.state())
        return self.review.state()

    def group(self):
        return next(g for g in self.review.result.groups if g['kind']=='exact')

    def test_cross_root_delete_restore_uses_each_own_recovery_store(self):
        self.scan()
        photos = {p.name:p for p in self.review.result.photos.values()}
        group = self.group()
        result = self.review.delete(photos['copy.txt'].id,group['id'],photos['original.txt'].id)
        self.assertFalse((self.b/'copy.txt').exists())
        self.assertTrue((self.a/'original.txt').exists())
        self.assertEqual(self.review.trash()['items'][0]['sourceRoot'],str(self.b))
        self.review.restore(result['trashId'])
        self.assertTrue((self.b/'copy.txt').exists())
        self.assertEqual(len(self.review.result.photos),2)
        self.assertEqual(next(p for p in self.review.result.photos.values() if p.name=='copy.txt').sourceRoot,str(self.b))

    def test_protected_original_rejected_by_direct_and_bulk_removal(self):
        self.scan(roots=[{'path':str(self.a),'protected':True},{'path':str(self.b)}])
        photos = {p.name:p for p in self.review.result.photos.values()}
        group=self.group()
        with self.assertRaises(ReviewError):
            self.review.delete(photos['original.txt'].id,group['id'],photos['copy.txt'].id)
        plan=self.review.bulk_delete_plan([group['id']],{group['id']:photos['copy.txt'].id})
        self.assertEqual(plan['count'],0)
        plan=self.review.bulk_delete_plan([group['id']],{group['id']:photos['original.txt'].id})
        self.assertEqual(plan['count'],1)
        self.review.bulk_delete(plan['planId'],True)
        self.assertTrue((self.a/'original.txt').exists())

    def test_secondary_root_rename_undo_retargets_index_and_preserves_private_fields(self):
        self.scan()
        photo=next(p for p in self.review.result.photos.values() if p.name=='copy.txt')
        photo.ocrText='Generated private index'
        plan=self.review.tools.plan('rename',ids=[photo.id],template='renamed_{counter:04d}')
        result=self.review.tools.execute(plan['planId'],True)
        self.assertTrue(result['ok'],result)
        renamed=next(p for p in self.review.result.photos.values() if p.name=='renamed_0001.txt')
        self.assertEqual(renamed.sourceRoot,str(self.b))
        self.assertEqual(renamed.ocrText,'Generated private index')
        self.assertEqual(renamed.path,'renamed_0001.txt')
        result=self.review.tools.undo(result['operationId'],True)
        self.assertTrue(result['ok'],result)
        self.assertTrue((self.b/'copy.txt').is_file())
        self.assertTrue(any(p.name=='copy.txt' for p in self.review.result.photos.values()))

    def test_saved_multi_root_workspace_reopens_with_roles_and_originals_verified(self):
        self.scan(roots=[{'path':str(self.a),'reference':True},{'path':str(self.b)}],options={'comparisonScope':'reference'})
        saved=self.review.save_session('Generated two-drive review')['session']
        self.review.load_session(saved['id'])
        self.assertEqual(len(self.review.roots),2)
        self.assertTrue(next(p for p in self.review.result.photos.values() if p.name=='original.txt').protected)
        self.assertEqual(len(self.review.result.groups),1)

    def test_offline_catalog_opens_when_drive_unavailable_without_filesystem_scan(self):
        self.scan()
        saved=self.review.save_session('Disconnected generated drives')['session']
        self.a.rename(self.base/'disconnected-a'); self.b.rename(self.base/'disconnected-b')
        with patch('app.scan_folder') as scan:
            state=self.review.load_session(saved['id'],offline=True)
        scan.assert_not_called()
        self.assertTrue(state['catalogOnly'])
        self.assertEqual(len(state['photos']),2)
        self.assertTrue(all(p['offline'] and p['protected'] for p in state['photos']))
        self.assertEqual(self.review.search('original',mode='filename')['total'],1)
        with self.assertRaises((ReviewError,StorageError)):
            self.review.photo(state['photos'][0]['id'])
        with self.assertRaises(ValueError):self.review.tools.plan('rename')

    def test_offline_keeper_cannot_authorize_removal_of_available_only_copy(self):
        self.scan(roots=[{'path':str(self.a)}])
        saved=self.review.save_session('Generated offline catalog')['session']
        self.a.rename(self.base/'disconnected-a')
        self.scan(roots=[{'path':str(self.b)}],catalog_ids=[saved['id']])
        offline=next(p for p in self.review.result.photos.values() if p.offline)
        live=next(p for p in self.review.result.photos.values() if not p.offline)
        group=self.group()
        self.assertEqual(self.review.bulk_delete_plan([group['id']],{group['id']:offline.id})['count'],0)
        with self.assertRaises(ReviewError):self.review.delete(live.id,group['id'],offline.id)
        self.assertTrue((self.b/'copy.txt').exists())

    def test_catalog_reference_can_compare_against_next_explicit_scan(self):
        self.scan(roots=[{'path':str(self.a),'reference':True}])
        saved=self.review.save_session('Generated reference catalog')['session']
        self.a.rename(self.base/'unplugged')
        self.scan(roots=[{'path':str(self.b)}],catalog_ids=[saved['id']],options={'comparisonScope':'reference'})
        self.assertEqual(len(self.review.result.groups),1)

    def test_duplicate_folder_snapshot_survives_save_and_offline_load(self):
        (self.a/'original.txt').rename(self.a/'same.txt');(self.b/'copy.txt').rename(self.b/'same.txt')
        self.scan(options={'folderMatching':True})
        self.assertTrue(self.review.result.folderGroups)
        saved=self.review.save_session('Generated directory catalog')['session']
        self.a.rename(self.base/'offline-a');self.b.rename(self.base/'offline-b')
        self.review.load_session(saved['id'],offline=True)
        self.assertTrue(self.review.result.folderGroups)
        self.assertTrue(all(f['protected'] for g in self.review.result.folderGroups for f in g['folders']))

    def test_checkpoint_failure_does_not_leave_active_worker_or_replace_current_review(self):
        self.scan()
        prior=self.review.result
        with patch.object(self.review.interrupted_store,'write',side_effect=OSError('Generated read-only checkpoint')):
            with self.assertRaises(OSError):self.review.start_scan(str(self.a))
        self.assertFalse(self.review.scanning)
        self.assertIs(self.review.result,prior)

    def test_new_app_does_not_resume_until_explicit_request_and_reuses_checkpoint_cache(self):
        self.scan()
        self.review.interrupted_store.write(self.review.roots,{'options':self.review.scan_options,'recursive':True,'threshold':94,'includeAllFiles':True})
        self.review.close()
        self.review=ReviewApp(session_directory=self.base/'sessions')
        self.assertFalse(self.review.has_scanned)
        self.assertIsNone(self.review.scan_thread)
        self.assertTrue(self.review.interrupted_scan()['checkpoint'])
        with patch('scanner._inspect',side_effect=AssertionError('Completed analysis should be reused')):
            self.review.resume_interrupted()
            self.review.scan_thread.join(15)
        self.assertEqual(self.review.progress['phase'],'complete',self.review.state())
        self.assertEqual(len(self.review.result.photos),2)
        self.assertEqual(self.review.result.stats['cacheHits'],2)
        self.assertIsNone(self.review.interrupted_scan()['checkpoint'])

    def test_protected_root_blocks_organization_undo_from_existing_history(self):
        self.scan(roots=[{'path':str(self.a)}])
        plan=self.review.organize_plan('type')
        operation=self.review.organize_execute(plan['planId'])
        self.review.organization_thread.join(15)
        self.scan(roots=[{'path':str(self.a),'protected':True}])
        with self.assertRaises(ReviewError):self.review.organize_undo(operation['operationId'])

    def test_active_scan_rejects_overlapping_roots_before_starting(self):
        nested=self.a/'nested';nested.mkdir()
        with self.assertRaises(ValueError):self.review.start_scan(roots=[{'path':str(self.a)},{'path':str(nested),'protected':True}])
        self.assertFalse(self.review.scanning)

    def test_offline_connected_catalog_cannot_cleanup_empty_folders_or_undo(self):
        (self.a/'empty').mkdir()
        self.scan(roots=[{'path':str(self.a)}])
        saved=self.review.save_session('Generated connected snapshot')['session']
        self.review.load_session(saved['id'],offline=True)
        with self.assertRaises(ValueError):self.review.tools.plan('empty-folders')
        with self.assertRaises(ValueError):self.review.tools.undo('unavailable',True)
        self.assertTrue((self.a/'empty').is_dir())

    def test_failed_selected_catalog_is_reported_with_partial_local_results(self):
        self.scan(roots=[{'path':str(self.a)}])
        saved=self.review.save_session('Generated corrupt snapshot')['session']
        self.review.session_store._path(saved['id']).write_text('{}','utf-8')
        self.review.start_scan(roots=[{'path':str(self.b)}],catalog_ids=[saved['id']])
        self.review.scan_thread.join(15)
        self.assertEqual(self.review.progress['phase'],'error')
        self.assertTrue(self.review.result.stopped)
        self.assertEqual(len(self.review.result.photos),1)
        self.assertTrue(self.review.result.errors)
        self.assertTrue(self.review.interrupted_scan()['checkpoint'])


if __name__=='__main__':unittest.main()
