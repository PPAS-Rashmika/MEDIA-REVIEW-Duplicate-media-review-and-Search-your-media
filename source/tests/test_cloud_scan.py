"""Cloud review tests use generated metadata; never a personal cloud account."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from cloud_scan import CloudCancelled, CloudError, CloudService, _binary, _hash, _path, _protect


ACCOUNT = {'id': 'mr_' + 'a' * 32, 'provider': 'drive', 'label': 'Generated test account'}


def item(path, digest='a' * 32, size=12, kind='MD5', identity=None):
    return {'Path': path, 'Name': path.rsplit('/', 1)[-1], 'Size': size, 'Hashes': {kind: digest},
            'ID': identity or path, 'IsDir': False, 'ModTime': '2026-01-01T00:00:00Z'}


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='media-review-cloud-')
        self.directory = Path(self.temporary.name)
        self.service = CloudService(data_dir=self.directory / 'private', binary=Path(__file__))
        self.service.accounts_cache = [dict(ACCOUNT)]

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def finish(self):
        worker = self.service.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        return self.service.state()

    def test_construct_and_state_do_not_create_files_or_start_network(self):
        service = CloudService(data_dir=self.directory / 'new', binary=Path(__file__))
        with patch.object(service, '_run', side_effect=AssertionError('No network')):
            state = service.state()
        self.assertFalse(service.directory.exists())
        self.assertEqual([], state['accounts'])
        self.assertFalse(state['scan']['running'])
        service.close()

    def test_bad_provider_root_and_recursive_are_rejected(self):
        for provider in ['local', 'http', '', None, [], {}]:
            with self.assertRaises(CloudError):
                self.service.connect(provider)
        for value in ['../outside', 'folder/../outside', 'D:/private', 'folder\x00bad']:
            with self.assertRaises(CloudError):
                _path(value)
        with self.assertRaises(CloudError):
            self.service.start_scan(ACCOUNT['id'], recursive='yes')
        self.assertEqual('Photos/2026', _path('/Photos/2026/'))

    def test_strong_hash_validation_and_quickxor_are_distinct(self):
        self.assertEqual(('MD5', 'a' * 32, False), _hash(item('a')))
        self.assertIsNone(_hash(item('a', digest='zzzz')))
        self.assertIsNone(_hash(item('a', digest='12345678', kind='CRC32')))
        quick = base64.b64encode(bytes(range(20))).decode()
        self.assertEqual(('QuickXor (weak)', bytes(range(20)).hex(), True), _hash(item('a', quick, kind='QuickXorHash')))
        self.assertEqual(('SHA-256', 'b' * 64, False), _hash({'Hashes': {'QuickXorHash': quick, 'SHA-256': 'b' * 64}}))

    def test_server_checksums_group_by_hash_and_size_without_downloads(self):
        records = [item('one.jpg'), item('nested/two.jpg'), item('different-size.jpg', size=13),
                   item('different-hash.jpg', digest='b' * 32), item('no-hash.jpg', digest=''),
                   item('unsafe/../name.jpg')]
        with patch.object(self.service, '_run', return_value=records) as run:
            self.service.start_scan(ACCOUNT['id'], path='Photos')
            state = self.finish()
        scan = state['scan']
        self.assertEqual('complete', scan['phase'])
        self.assertEqual(6, scan['totalFiles'])
        self.assertEqual(4, scan['hashedFiles'])
        self.assertEqual(2, scan['skippedFiles'])
        self.assertEqual(1, scan['groupCount'])
        self.assertEqual(12, scan['duplicateBytes'])
        self.assertFalse(scan['groups'][0]['possible'])
        self.assertEqual(['Photos/nested/two.jpg', 'Photos/one.jpg'], [f['path'] for f in scan['groups'][0]['files']])
        args = run.call_args.args[0]
        self.assertEqual('lsjson', args[0])
        self.assertIn('--hash', args)
        self.assertNotIn('copy', args)
        self.assertNotIn('cat', args)
        self.assertTrue(state['readOnly'])

    def test_onedrive_quickxor_is_possible_match_only(self):
        self.service.accounts_cache = [dict(ACCOUNT, provider='onedrive')]
        quick = 'a' * 40
        with patch.object(self.service, '_run', return_value=[item('one', quick, kind='quickxor'), item('two', quick, kind='quickxor')]):
            self.service.start_scan(ACCOUNT['id'])
            scan = self.finish()['scan']
        self.assertEqual(1, scan['groupCount'])
        self.assertTrue(scan['groups'][0]['possible'])
        self.assertIn('weak', scan['groups'][0]['hashType'])

    def test_same_remote_object_alias_is_not_counted_twice(self):
        with patch.object(self.service, '_run', return_value=[item('one', identity='same-id'), item('alias', identity='same-id')]):
            self.service.start_scan(ACCOUNT['id'])
            scan = self.finish()['scan']
        self.assertEqual([], scan['groups'])

    def test_folder_browse_has_no_scan_side_effect(self):
        with patch.object(self.service, '_run', return_value=[{'Path': '2026', 'Name': '2026', 'IsDir': True}]) as run:
            result = self.service.folders(ACCOUNT['id'], 'Photos')
        self.assertEqual([{'name': '2026', 'path': 'Photos/2026'}], result['folders'])
        self.assertEqual('ready', self.service.state()['scan']['phase'])
        self.assertIn('--dirs-only', run.call_args.args[0])

    def test_stop_cancels_listing_and_repeated_stop_is_idempotent(self):
        started = threading.Event()

        def listing(*args, **kwargs):
            started.set()
            self.service.stop_event.wait(3)
            raise CloudCancelled()

        with patch.object(self.service, '_run', side_effect=listing):
            self.service.start_scan(ACCOUNT['id'])
            self.assertTrue(started.wait(1))
            self.service.stop_scan()
            self.service.stop_scan()
            scan = self.finish()['scan']
        self.assertFalse(scan['running'])
        self.assertTrue(scan['stopped'])
        self.assertEqual('stopped', scan['phase'])

    def test_google_oauth_needs_configured_application(self):
        with self.assertRaisesRegex(CloudError, 'Desktop OAuth'):
            self.service.connect('drive')
        with self.assertRaises(CloudError):
            self.service.connect('drive', client_id='only-one-field')

    def test_connect_does_not_echo_credentials_or_oauth_state(self):
        with patch.object(self.service, '_run', return_value={'State': '', 'Option': None, 'Error': ''}) as run:
            self.service.connect('drive', 'My account', 'generated-client', 'generated-secret')
            state = self.finish()
        encoded = json.dumps(state)
        self.assertNotIn('generated-client', encoded)
        self.assertNotIn('generated-secret', encoded)
        self.assertNotIn('generated-secret', json.dumps(run.call_args.args))
        self.assertEqual('connected', state['connection']['phase'])
        self.assertIsNone(self.service.private_connection)
        self.assertNotIn('secret', (self.service.directory / 'accounts.json').read_text())

    def test_continuation_question_and_allowed_answer(self):
        question = {'State': 'private-flow-state', 'Error': '', 'Option': {
            'Name': 'config_driveid', 'Help': 'Choose a drive.', 'Exclusive': True, 'Required': True,
            'Examples': [{'Value': 'drive-id', 'Help': 'Personal drive'}], 'Default': 'drive-id'}}
        with patch.object(self.service, '_run', side_effect=[question, {'State': '', 'Error': ''}]):
            self.service.connect('onedrive')
            state = self.finish()
            self.assertNotIn('private-flow-state', json.dumps(state))
            self.assertEqual('question', state['connection']['phase'])
            with self.assertRaises(CloudError):
                self.service.answer('not-available')
            self.service.answer('drive-id')
            state = self.finish()
        self.assertEqual('connected', state['connection']['phase'])

    def test_token_paste_prompt_is_not_exposed(self):
        question = {'State': 'private', 'Option': {'Name': 'config_token', 'Help': 'secret value'}}
        with patch.object(self.service, '_run', return_value=question):
            self.service.connect('dropbox')
            state = self.finish()
        self.assertEqual('error', state['connection']['phase'])
        self.assertNotIn('secret value', json.dumps(state))

    def test_open_uses_provider_browser_without_public_link_creation(self):
        records = [item('one.jpg'), item('two.jpg')]
        with patch.object(self.service, '_run', return_value=records):
            self.service.start_scan(ACCOUNT['id'])
            state = self.finish()
        file_id = state['scan']['groups'][0]['files'][0]['id']
        # Generated IDs contain dots and are not actual Drive IDs.
        self.service.files[file_id]['remoteId'] = 'generated_valid_id'
        with patch('cloud_scan.webbrowser.open') as open_browser, patch.object(self.service, '_run') as run:
            self.service.open_file(file_id)
        self.assertEqual('https://drive.google.com/file/d/generated_valid_id/view', open_browser.call_args.args[0])
        run.assert_not_called()
        with self.assertRaises(CloudError):
            self.service.open_file('arbitrary-id')
        with self.assertRaises(CloudError):
            self.service.open_file([])

    def test_configuration_sealed_and_temporary_plaintext_removed(self):
        with self.service._configuration() as path:
            path.write_bytes(b'[generated]\ntoken = generated-test-only-secret\n')
            plain_path = path
        self.assertFalse(plain_path.exists())
        sealed = self.service.directory / 'connections.dat'
        self.assertTrue(sealed.is_file())
        self.assertEqual(b'[generated]\ntoken = generated-test-only-secret\n', _protect(sealed.read_bytes(), decrypt=True))
        if __import__('sys').platform == 'win32':
            self.assertNotIn(b'generated-test-only-secret', sealed.read_bytes())

    @unittest.skipUnless(_binary(), 'Bundled rclone is unavailable')
    def test_real_cli_reads_only_generated_local_metadata(self):
        fixtures = self.directory / 'fixtures'
        fixtures.mkdir()
        (fixtures / 'first.bin').write_bytes(b'Generated cloud adapter test only')
        (fixtures / 'second.bin').write_bytes(b'Generated cloud adapter test only')
        service = CloudService(data_dir=self.directory / 'cli-private', binary=_binary())
        try:
            listing = service._run(['lsjson', str(fixtures), '--hash', '--files-only'], timeout=15)
            self.assertEqual(2, len(listing))
            self.assertEqual(_hash(listing[0]), _hash(listing[1]))
            self.assertEqual(b'Generated cloud adapter test only', (fixtures / 'first.bin').read_bytes())
            self.assertEqual([], list(service.directory.glob('oauth-*.conf')))
        finally:
            service.close()

    @unittest.skipUnless(_binary(), 'Bundled rclone is unavailable')
    def test_real_cli_oauth_protocol_without_browser_or_account(self):
        # Asking for remote authorization stops at a JSON token question. No
        # browser, network account, password or real token is involved.
        import configparser
        for index, provider in enumerate(['drive', 'onedrive', 'dropbox']):
            with self.subTest(provider=provider):
                service = CloudService(data_dir=self.directory / ('protocol-' + provider), binary=_binary())
                name = 'mr_' + str(index) * 32
                environment = {
                    'RCLONE_CONFIG_' + name.upper() + '_CLIENT_ID': 'generated-client-only',
                    'RCLONE_CONFIG_' + name.upper() + '_CLIENT_SECRET': 'generated-secret-only',
                }
                try:
                    arguments = ['config', 'create', name, provider, 'config_is_local', 'false', '--non-interactive']
                    if provider == 'drive':
                        arguments += ['scope', 'drive.metadata.readonly']
                    result = service._run(arguments, timeout=15, extra_env=environment)
                    self.assertTrue(result['State'])
                    self.assertEqual('config_token', result['Option']['Name'])
                    self.assertFalse(result['Error'])
                    with service._configuration() as config:
                        parser = configparser.RawConfigParser()
                        parser.read(config, encoding='utf-8')
                        self.assertEqual('generated-client-only', parser.get(name, 'client_id'))
                        self.assertEqual('generated-secret-only', parser.get(name, 'client_secret'))
                    self.assertEqual([], list(service.directory.glob('oauth-*.conf')))
                finally:
                    service.close()


if __name__ == '__main__':
    unittest.main()
