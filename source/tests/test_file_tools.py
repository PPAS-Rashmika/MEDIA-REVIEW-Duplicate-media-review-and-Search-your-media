"""Productivity operations exercise generated files only, including stale plans."""
import csv
import io
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from file_tools import FileToolError, FileTools, keeper_choices
from scanner import Photo, ScanResult, file_signature, hash_file
from storage import TrashStore


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='media-review-tools-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'media'
        self.root.mkdir()
        self.app = SimpleNamespace(root=self.root, roots=[{'path':str(self.root)}], has_scanned=True,
            scanning=False, session_busy=False, lock=threading.RLock(), review_revision=0,
            result=ScanResult(), preview_cache={}, organization={}, store=TrashStore(self.root),
            session_store=SimpleNamespace(directory=self.base / 'sessions'))
        self.app._require_idle = lambda: None
        def invalidate(): self.app.review_revision += 1
        self.app._invalidate_bulk_plans = invalidate
        self.app.state = lambda: {'revision':self.app.review_revision, 'photos':[p.public() for p in self.app.result.photos.values()]}
        self.tools = FileTools(self.app)

    def photo(self, name, content=b'generated content', root=None):
        root = root or self.root
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        identity = str(len(self.app.result.photos) + 1)
        photo = Photo(identity, path, path.name, str(path.relative_to(root)), len(content), file_signature(path),
                      sha256=hash_file(path), mediaType='other')
        photo.sourceRoot = str(root)
        self.app.result.photos[identity] = photo
        return photo

    def group(self, *photos):
        group = {'id':str(len(self.app.result.groups)+1), 'kind':'exact', 'score':100, 'photos':[p.id for p in photos]}
        self.app.result.groups.append(group)
        return group

    def test_keep_rules_respect_dates_formats_folder_and_protected(self):
        a = self.photo('preferred/a.jpg')
        b = self.photo('b.png')
        os.utime(a.file, (1000,1000)); a.signature = file_signature(a.file)
        os.utime(b.file, (2000,2000)); b.signature = file_signature(b.file)
        group = self.group(a,b)
        self.assertEqual(self.tools.choose_keepers('newest')[group['id']], b.id)
        self.assertEqual(self.tools.choose_keepers('oldest')[group['id']], a.id)
        self.assertEqual(self.tools.choose_keepers('folder',str(a.file.parent))[group['id']], a.id)
        self.assertEqual(self.tools.choose_keepers('format',formats=['png','jpg'])[group['id']], b.id)
        a.protected = True
        self.assertEqual(self.tools.choose_keepers('newest')[group['id']], a.id)

    def test_report_has_bom_and_escapes_excel_formula_names(self):
        a,b = self.photo('=malicious.txt'),self.photo('normal.txt')
        self.group(a,b)
        data = self.tools.report_csv()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertEqual(rows[1][3], "'=malicious.txt")
        self.assertEqual(len(rows),3)

    def test_rename_preview_creates_nothing_execute_and_restart_undo(self):
        a = self.photo('old/name.txt', b'preserved bytes')
        plan = self.tools.plan('rename',template='holiday_{counter:03d}',start=4)
        destination = Path(plan['files'][0]['destination'])
        self.assertEqual(destination.name,'holiday_004.txt')
        self.assertFalse(destination.exists())
        self.assertFalse(self.tools.directory.exists())
        result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],1)
        self.assertEqual(destination.read_bytes(),b'preserved bytes')
        self.assertFalse((self.root/'old/name.txt').exists())
        reopened = FileTools(self.app)
        self.assertTrue(reopened.history()[0]['undoAvailable'])
        undone = reopened.undo(result['operationId'],True)
        self.assertEqual(undone['completed'],1)
        self.assertEqual((self.root/'old/name.txt').read_bytes(),b'preserved bytes')

    def test_no_confirmation_and_revision_changes_reject(self):
        a = self.photo('a.txt')
        plan = self.tools.plan('rename',template='new')
        with self.assertRaises(FileToolError): self.tools.execute(plan['planId'])
        self.app.review_revision += 1
        with self.assertRaises(FileToolError): self.tools.execute(plan['planId'],True)
        self.assertTrue(a.file.is_file())

    def test_existing_destination_and_bad_templates_reject(self):
        self.photo('a.txt')
        self.photo('new.txt')
        for template in ['new','../{name}','{name.__class__}','CON']:
            with self.assertRaises(FileToolError): self.tools.plan('rename',template=template)

    def test_changed_later_file_rejects_entire_batch_even_same_stat(self):
        a,b = self.photo('a.txt',b'aaaa'),self.photo('b.txt',b'bbbb')
        plan = self.tools.plan('rename',template='new_{counter}')
        info = b.file.stat()
        b.file.write_bytes(b'cccc')
        os.utime(b.file,ns=(info.st_atime_ns,info.st_mtime_ns))
        with self.assertRaises(FileToolError): self.tools.execute(plan['planId'],True)
        self.assertTrue(a.file.is_file())
        self.assertFalse((self.root/'new_1.txt').exists())

    def test_offline_and_protected_files_cannot_be_changed(self):
        a = self.photo('a.txt')
        a.offline = True
        with self.assertRaises(FileToolError): self.tools.plan('rename',ids=[a.id])
        a.offline = False
        self.app.roots[0]['protected'] = True
        with self.assertRaises(FileToolError): self.tools.plan('rename',ids=[a.id])

    def test_empty_nested_folders_cleanup_and_undo_preserve_root_and_files(self):
        self.photo('occupied/a.txt')
        (self.root/'empty/nested').mkdir(parents=True)
        plan = self.tools.plan('empty-folders')
        self.assertEqual(plan['count'],2)
        self.assertTrue((self.root/'empty/nested').is_dir())
        result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],2)
        self.assertTrue(self.root.is_dir())
        self.assertTrue((self.root/'occupied/a.txt').is_file())
        self.tools.undo(result['operationId'],True)
        self.assertTrue((self.root/'empty/nested').is_dir())

    def test_folder_acquiring_file_after_preview_is_preserved(self):
        (self.root/'empty').mkdir()
        plan = self.tools.plan('empty-folders')
        (self.root/'empty/new.txt').write_text('new file')
        result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],0)
        self.assertTrue((self.root/'empty/new.txt').is_file())

    def test_hardlink_saves_duplicate_inode_then_undo_creates_independent_copy(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        plan = self.tools.plan('hardlinks')
        self.assertEqual(plan['count'],1)
        before = a.file.stat().st_ino,b.file.stat().st_ino
        self.assertNotEqual(*before)
        result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],1,result)
        self.assertEqual(a.file.stat().st_ino,b.file.stat().st_ino)
        self.assertEqual(self.tools.plan('hardlinks')['count'],0)
        reopened = FileTools(self.app)
        undone = reopened.undo(result['operationId'],True)
        self.assertEqual(undone['completed'],1,undone)
        self.assertNotEqual(a.file.stat().st_ino,b.file.stat().st_ino)
        b.file.write_bytes(b'independent edit')
        self.assertEqual(a.file.read_bytes(),b'generated content')

    def test_hardlink_undo_refuses_edits_to_shared_contents(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        result = self.tools.execute(self.tools.plan('hardlinks')['planId'],True)
        a.file.write_bytes(b'edited since linking')
        undo = self.tools.undo(result['operationId'],True)
        self.assertEqual(undo['completed'],0)
        self.assertEqual(b.file.read_bytes(),b'edited since linking')

    def test_native_recycle_keeps_original_and_updates_review(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        plan = self.tools.plan('recycle')
        with patch('file_tools.windows_recycle',side_effect=lambda paths: [p.unlink() for p in paths]):
            result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],1)
        self.assertTrue(a.file.is_file())
        self.assertFalse(b.file.exists())
        self.assertEqual(len(self.app.result.photos),1)
        self.assertFalse(self.app.result.groups)

    def test_permanent_trash_purge_verifies_and_removes_only_recovery_file(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        record = self.app.store.move(b,a)
        del self.app.result.photos[b.id]
        plan = self.tools.plan('purge-trash')
        self.assertTrue(plan['permanent'])
        self.assertEqual(plan['count'],1)
        result = self.tools.execute(plan['planId'],True)
        self.assertEqual(result['completed'],1,result)
        self.assertEqual(self.app.store.items(),[])
        self.assertTrue(a.file.is_file())
        self.assertTrue(self.app.store.journal.is_file())

    def test_multiple_roots_rename_and_protected_root_guard(self):
        second = self.base/'other'
        second.mkdir()
        self.app.roots.append({'path':str(second)})
        a,b = self.photo('a.txt'),self.photo('b.txt',root=second)
        plan = self.tools.plan('rename',template='new_{counter}')
        self.assertEqual(plan['count'],2)
        self.app.roots[1]['protected'] = True
        with self.assertRaises(FileToolError): self.tools.execute(plan['planId'],True)
        self.assertTrue(a.file.is_file())
        self.assertTrue(b.file.is_file())

    def test_rename_undo_refuses_new_file_at_original_location(self):
        self.photo('a.txt')
        result = self.tools.execute(self.tools.plan('rename',template='renamed')['planId'],True)
        (self.root/'a.txt').write_bytes(b'new unrelated bytes')
        undo = self.tools.undo(result['operationId'],True)
        self.assertEqual(undo['completed'],0)
        self.assertEqual((self.root/'a.txt').read_bytes(),b'new unrelated bytes')
        self.assertEqual((self.root/'renamed.txt').read_bytes(),b'generated content')

    def test_cross_root_trash_verifies_keeper_in_its_own_store(self):
        other = self.base/'other'
        other.mkdir()
        a = self.photo('original.txt')
        b = self.photo('copy.txt',root=other)
        second_store = TrashStore(other)
        record = second_store.move(b,a,keeper_store=self.app.store)
        self.assertFalse(b.file.exists())
        self.assertEqual(second_store.preview_path(record['id']).read_bytes(),b'generated content')
        self.assertTrue(a.file.is_file())

    def test_interrupted_prepared_intent_undo_never_removes_original(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        plan = self.tools.plan('hardlinks')
        item = dict(self.tools.plans[plan['planId']]['files'][0],status='pending')
        item['backup'] = str(b.file.with_name('.interrupted-backup'))
        self.tools._save([{'id':'interrupted','kind':'hardlinks','createdAt':'2026-01-01','files':[item]}])
        result = FileTools(self.app).undo('interrupted',True)
        self.assertEqual(result['completed'],1)
        self.assertTrue(a.file.is_file())
        self.assertTrue(b.file.is_file())
        self.assertNotEqual(a.file.stat().st_ino,b.file.stat().st_ino)

    def test_hardlink_backup_cleanup_failure_remains_undoable(self):
        a,b = self.photo('a.txt'),self.photo('b.txt')
        self.group(a,b)
        original_unlink = Path.unlink
        def fail_backup(path,*args,**kwargs):
            if path.suffix == '.bak': raise PermissionError('simulated cleanup lock')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_backup):
            result = self.tools.execute(self.tools.plan('hardlinks')['planId'],True)
        self.assertEqual(result['completed'],1)
        self.assertTrue(self.tools.history()[0]['undoAvailable'])
        undo = self.tools.undo(result['operationId'],True)
        self.assertEqual(undo['completed'],1,undo)
        self.assertNotEqual(a.file.stat().st_ino,b.file.stat().st_ino)


if __name__ == '__main__':
    unittest.main()
