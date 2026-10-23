"""Cloud management exercises a generated in-memory provider, never user accounts."""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cloud_scan import CloudError, CloudService, ACTION_PLAN_SECONDS, RECOVERY_FOLDER, _binary
from tests.test_cloud_scan import ACCOUNT, item


class GeneratedCloud:
    def __init__(self, provider='drive', records=None):
        self.files = {record['Path']: copy.deepcopy(record) for record in records or [item('Photos/one.jpg'), item('Photos/two.jpg')]}
        self.folders = {'', 'Photos', 'Destination'}
        self.calls = []
        self.fail_move = False

    def run(self, arguments, **kwargs):
        self.calls.append(arguments)
        command = arguments[0]
        path = arguments[1].split(':', 1)[1]
        if command == 'lsjson' and '--stat' in arguments:
            if path not in self.files:
                raise CloudError('Generated file does not exist.')
            return copy.deepcopy(self.files[path])
        if command == 'lsjson':
            if path not in self.folders:
                raise CloudError('Generated folder does not exist.')
            result = []
            for full_path, record in self.files.items():
                if path and not full_path.startswith(path + '/'):
                    continue
                relative = full_path[len(path) + 1:] if path else full_path
                if '/' in relative and '--recursive' not in arguments:
                    continue
                result.append(dict(copy.deepcopy(record), Path=relative))
            return result
        if command == 'mkdir':
            components = path.split('/')
            self.folders.update('/'.join(components[:i]) for i in range(1, len(components) + 1))
            return {}
        if command == 'moveto':
            if self.fail_move:
                raise CloudError('Generated move failure.')
            destination = arguments[2].split(':', 1)[1]
            if destination in self.files:
                if '--ignore-existing' not in arguments:
                    raise AssertionError('Move did not protect existing destination')
                return {}
            record = self.files.pop(path)
            self.files[destination] = dict(record, Path=destination, Name=destination.rsplit('/', 1)[-1])
            return {}
        raise AssertionError('Unexpected adapter operation: ' + str(arguments))


class CloudActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='media-review-cloud-actions-')
        self.directory = Path(self.temporary.name)
        self.service = CloudService(data_dir=self.directory, binary=Path(__file__))
        self.service.accounts_cache = [dict(ACCOUNT, writeEnabled=True)]
        self.provider = GeneratedCloud()
        self.patched = patch.object(self.service, '_run', side_effect=self.provider.run)
        self.patched.start()
        self.service.start_scan(ACCOUNT['id'], 'Photos')
        self.finish()
        self.ids = [f['id'] for f in self.service.scan['groups'][0]['files']]

    def tearDown(self):
        self.service.close()
        self.patched.stop()
        self.temporary.cleanup()

    def finish(self):
        worker = self.service.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        return self.service.state()

    def test_plan_is_read_only_and_names_every_destination(self):
        before = copy.deepcopy(self.provider.files)
        plan = self.service.action_plan([self.ids[1]], 'trash')
        self.assertEqual(before, self.provider.files)
        self.assertEqual('Photos/two.jpg', plan['files'][0]['source'])
        self.assertTrue(plan['files'][0]['destination'].startswith(RECOVERY_FOLDER + '/'))
        self.assertEqual(ACCOUNT['label'], plan['accountLabel'])
        self.assertEqual(12, plan['totalSize'])
        self.assertFalse(any(call[0] in {'moveto', 'mkdir', 'delete', 'deletefile', 'purge'} for call in self.provider.calls))

    def test_exact_confirmation_is_required_and_plan_is_single_use(self):
        plan = self.service.action_plan([self.ids[1]])
        for value in (False, None, 'true', 1):
            with self.assertRaises(CloudError):
                self.service.execute_action(plan['id'], value)
        self.service.execute_action(plan['id'], True)
        self.assertEqual('complete', self.finish()['action']['phase'])
        self.assertNotIn('Photos/two.jpg', self.provider.files)
        self.assertIn('Photos/one.jpg', self.provider.files)
        self.assertIn(plan['files'][0]['destination'], self.provider.files)
        with self.assertRaises(CloudError):
            self.service.execute_action(plan['id'], True)

    def test_cannot_trash_every_file_in_group(self):
        with self.assertRaisesRegex(CloudError, 'at least one'):
            self.service.action_plan(self.ids, 'trash')
        self.assertEqual(2, len(self.provider.files))

    def test_source_change_or_keeper_change_prevents_entire_batch(self):
        for changed in ('Photos/two.jpg', 'Photos/one.jpg'):
            with self.subTest(changed=changed):
                plan = self.service.action_plan([self.ids[1]], 'trash')
                self.provider.files[changed]['Hashes'] = {'MD5': 'b' * 32}
                self.service.execute_action(plan['id'], True)
                state = self.finish()
                self.assertEqual('error', state['action']['phase'])
                self.assertEqual(0, state['action']['done'])
                self.assertFalse(any(call[0] == 'moveto' for call in self.provider.calls))
                self.provider.files[changed]['Hashes'] = {'MD5': 'a' * 32}

    def test_identity_change_is_rejected_even_when_checksum_matches(self):
        self.provider.files['Photos/two.jpg']['ID'] = 'replacement-object'
        with self.assertRaisesRegex(CloudError, 'identity'):
            self.service.action_plan([self.ids[1]])

    def test_ambiguous_same_name_remote_objects_are_rejected(self):
        self.provider.files['Photos/TWO.jpg'] = item('Photos/TWO.jpg', identity='another-object')
        with self.assertRaisesRegex(CloudError, 'ambiguous'):
            self.service.action_plan([self.ids[1]])

    def test_missing_stable_identity_is_rejected(self):
        self.service.files[self.ids[1]]['remoteId'] = ''
        with self.assertRaises(CloudError):
            self.service.action_plan([self.ids[1]])

    def test_expired_preview_and_rescan_invalidate_plan(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.action_plans[plan['id']]['created'] -= ACTION_PLAN_SECONDS + 1
        with self.assertRaisesRegex(CloudError, 'expired'):
            self.service.execute_action(plan['id'], True)
        plan = self.service.action_plan([self.ids[1]])
        self.service.start_scan(ACCOUNT['id'], 'Photos')
        self.finish()
        with self.assertRaises(CloudError):
            self.service.execute_action(plan['id'], True)

    def test_readonly_old_connection_requires_reconnect(self):
        self.service.accounts_cache[0]['writeEnabled'] = False
        with self.assertRaisesRegex(CloudError, 'Reconnect'):
            self.service.action_plan([self.ids[1]])
        self.assertTrue(self.service.state()['readOnly'])

    def test_invalid_selection_action_and_destination_are_rejected(self):
        for ids in ([], [self.ids[0]] * 2, ['x'], None, [None]):
            with self.assertRaises(CloudError):
                self.service.action_plan(ids)
        for action in ('purge', 'delete', None):
            with self.assertRaises(CloudError):
                self.service.action_plan([self.ids[1]], action)
        for destination in ('../outside', 'Other/../outside', 'C:/outside', '.MediaReviewTrash/elsewhere'):
            with self.assertRaises(CloudError):
                self.service.action_plan([self.ids[1]], 'move', destination)

    def test_move_type_and_year_produce_distinct_batches_without_overwrite(self):
        for action, segment in (('move', '/two.jpg'), ('type', '/JPG/two.jpg'), ('year', '/2026/two.jpg')):
            with self.subTest(action=action):
                plan = self.service.action_plan([self.ids[1]], action, 'Destination')
                self.assertTrue(plan['files'][0]['destination'].startswith('Destination/'))
                self.assertTrue(plan['files'][0]['destination'].endswith(segment))
                self.assertIn('Media Review', plan['destination'])

    def test_destination_collision_does_not_move_or_overwrite_source(self):
        plan = self.service.action_plan([self.ids[1]])
        destination = plan['files'][0]['destination']
        self.provider.files[destination] = item(destination, digest='b' * 32)
        self.service.execute_action(plan['id'], True)
        self.assertEqual('error', self.finish()['action']['phase'])
        self.assertIn('Photos/two.jpg', self.provider.files)
        self.assertEqual('b' * 32, self.provider.files[destination]['Hashes']['MD5'])

    def test_move_has_no_overwrite_flag_and_verified_persistent_restore(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.execute_action(plan['id'], True)
        self.finish()
        move = next(call for call in self.provider.calls if call[0] == 'moveto')
        self.assertIn('--ignore-existing', move)
        self.assertIn('--checksum', move)
        self.assertEqual(1, self.service.action_history()['operations'][0]['moved'])
        self.service.restore_action(plan['id'], True)
        self.assertEqual('complete', self.finish()['action']['phase'])
        self.assertIn('Photos/two.jpg', self.provider.files)
        self.assertNotIn(plan['files'][0]['destination'], self.provider.files)
        self.assertEqual(1, self.service.action_history()['operations'][0]['restored'])

    def test_restore_never_overwrites_an_occupied_original_path(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.execute_action(plan['id'], True)
        self.finish()
        self.provider.files['Photos/two.jpg'] = item('Photos/two.jpg', digest='b' * 32)
        self.service.restore_action(plan['id'], True)
        self.assertEqual('error', self.finish()['action']['phase'])
        self.assertEqual('b' * 32, self.provider.files['Photos/two.jpg']['Hashes']['MD5'])
        self.assertIn(plan['files'][0]['destination'], self.provider.files)

    def test_interrupted_move_keeps_recovery_record_and_can_restore(self):
        plan = self.service.action_plan([self.ids[1]])
        run = self.provider.run

        def interrupted(arguments, **kwargs):
            result = run(arguments, **kwargs)
            if arguments[0] == 'moveto':
                raise CloudError('Generated interruption after server move.')
            return result

        with patch.object(self.service, '_run', side_effect=interrupted):
            self.service.execute_action(plan['id'], True)
            self.assertEqual('error', self.finish()['action']['phase'])
        self.assertEqual(1, self.service.action_history()['operations'][0]['moved'])
        self.service.restore_action(plan['id'], True)
        self.assertEqual('complete', self.finish()['action']['phase'])
        self.assertIn('Photos/two.jpg', self.provider.files)

    def test_restart_after_restore_rename_recognizes_original_without_second_move(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.execute_action(plan['id'], True)
        self.finish()
        destination = plan['files'][0]['destination']
        record = self.provider.files.pop(destination)
        self.provider.files['Photos/two.jpg'] = dict(record, Name='two.jpg', Path='Photos/two.jpg')
        history = self.service._history()
        history[0]['files'][0]['status'] = 'restoring'
        self.service._save_history(history)
        before = sum(call[0] == 'moveto' for call in self.provider.calls)
        self.service.restore_action(plan['id'], True)
        self.assertEqual('complete', self.finish()['action']['phase'])
        self.assertEqual(before, sum(call[0] == 'moveto' for call in self.provider.calls))
        self.assertEqual(1, self.service.action_history()['operations'][0]['restored'])

    def test_history_can_restore_after_service_restart(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.execute_action(plan['id'], True)
        self.finish()
        other = CloudService(data_dir=self.directory, binary=Path(__file__))
        other.accounts_cache = [dict(ACCOUNT, writeEnabled=True)]
        try:
            with patch.object(other, '_run', side_effect=self.provider.run):
                self.assertEqual(1, other.action_history()['operations'][0]['moved'])
                other.restore_action(plan['id'], True)
                other.worker.join(3)
                self.assertEqual('complete', other.state()['action']['phase'])
            self.assertIn('Photos/two.jpg', self.provider.files)
        finally:
            other.close()

    def test_weak_checksum_is_recoverable_only_and_never_permanent_deletion(self):
        self.service.accounts_cache[0]['provider'] = 'onedrive'
        for path, record in self.provider.files.items():
            record['Hashes'] = {'QuickXorHash': 'a' * 40}
        self.service.start_scan(ACCOUNT['id'], 'Photos')
        self.finish()
        ids = [f['id'] for f in self.service.scan['groups'][0]['files']]
        plan = self.service.action_plan([ids[1]])
        self.assertTrue(plan['possibleMatches'])
        self.service.execute_action(plan['id'], True)
        self.assertEqual('complete', self.finish()['action']['phase'])
        self.assertEqual(2, len(self.provider.files))
        self.assertFalse(any(call[0] in {'delete', 'deletefile', 'purge'} for call in self.provider.calls))

    def test_recovery_directory_is_excluded_from_later_scan(self):
        plan = self.service.action_plan([self.ids[1]])
        self.service.execute_action(plan['id'], True)
        self.finish()
        self.service.start_scan(ACCOUNT['id'], '', recursive=True)
        self.assertEqual([], self.finish()['scan']['groups'])

    @unittest.skipUnless(_binary(), 'Bundled rclone is unavailable')
    def test_real_adapter_no_overwrite_move_on_generated_local_files(self):
        fixtures = self.directory / 'local-fixtures'
        fixtures.mkdir()
        source, destination = fixtures / 'source.bin', fixtures / 'destination.bin'
        source.write_bytes(b'generated source content')
        destination.write_bytes(b'generated occupied destination')
        service = CloudService(data_dir=self.directory / 'local-cli', binary=_binary())
        try:
            service._run(['moveto', str(source), str(destination), '--ignore-existing', '--no-traverse', '--checksum'])
            self.assertEqual(b'generated source content', source.read_bytes())
            self.assertEqual(b'generated occupied destination', destination.read_bytes())
            target = fixtures / 'new.bin'
            service._run(['moveto', str(source), str(target), '--ignore-existing', '--no-traverse', '--checksum'])
            self.assertFalse(source.exists())
            self.assertEqual(b'generated source content', target.read_bytes())
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
