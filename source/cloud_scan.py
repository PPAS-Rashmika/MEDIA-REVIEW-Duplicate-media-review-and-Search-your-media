"""Explicit cloud checksum review and confirmed, recoverable file management.

Scans do not download media. Credentials are encrypted with Windows DPAPI at rest;
only the user's explicit connection step may open an OAuth browser window.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import base64
from contextlib import contextmanager
import configparser
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import uuid
import webbrowser

PROVIDERS = {'drive': 'Google Drive', 'onedrive': 'OneDrive', 'dropbox': 'Dropbox'}
MAX_LIST_BYTES = 64 * 1024 * 1024
MAX_FILES = 100_000
MAX_GROUPS = 1_000
ACTION_LIMIT = 100
ACTION_PLAN_SECONDS = 600
RECOVERY_FOLDER = '.MediaReviewTrash'
APP_DIR = Path(__file__).resolve().parent


class CloudError(ValueError):
    pass


class CloudCancelled(CloudError):
    pass


def _binary():
    candidates = [APP_DIR / 'bin' / 'rclone.exe']
    if getattr(sys, 'frozen', False):
        candidates.append(Path(sys.executable).parent / 'bin' / 'rclone.exe')
    return next((p for p in candidates if p.is_file() and not p.is_symlink()), None)


def _data_directory():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local' / 'share'))) / 'MediaReview' / 'cloud'


def _path(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise CloudError('Enter a cloud folder path of at most 2,048 characters.')
    value = value.replace('\\', '/').strip('/')
    if ':' in value or any(part in {'.', '..'} for part in value.split('/')):
        raise CloudError('Choose a folder inside the connected cloud account.')
    return value


def _protect(data, decrypt=False):
    if sys.platform != 'win32':
        # Windows is the supported release platform. POSIX development files
        # receive mode 0600; no claim of DPAPI protection is made there.
        return data
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    source = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(incoming), None, None, None,
                                                    None, 1, ctypes.byref(outgoing))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(incoming), 'Media Review cloud connection',
                                                  None, None, None, 1, ctypes.byref(outgoing))
    if not ok:
        raise CloudError('The saved cloud connection could not be unlocked for this Windows account.')
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _hash(item):
    hashes = item.get('Hashes')
    if not isinstance(hashes, dict):
        return None
    normalized = {re.sub('[^a-z0-9]', '', str(k).lower()): v for k, v in hashes.items()}
    for name, length, label in [('sha256', 64, 'SHA-256'), ('dropbox', 64, 'Dropbox content hash'),
                                ('dropboxhash', 64, 'Dropbox content hash'), ('sha1', 40, 'SHA-1'),
                                ('md5', 32, 'MD5')]:
        value = normalized.get(name)
        if isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{' + str(length) + '}', value):
            return label, value.lower(), False
    # OneDrive commonly supplies only QuickXor. Keep it useful as an explicitly
    # weak, read-only candidate group, never an exact/verified duplicate.
    for key in ('quickxor', 'quickxorhash'):
        value = normalized.get(key)
        if isinstance(value, str):
            if re.fullmatch('[0-9a-fA-F]{40}', value):
                return 'QuickXor (weak)', value.lower(), True
            try:
                decoded = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                continue
            if len(decoded) == 20:
                return 'QuickXor (weak)', decoded.hex(), True
    # CRC values are not used as content fingerprints.
    return None


class CloudService:
    def __init__(self, data_dir=None, binary=None):
        self.directory = Path(data_dir) if data_dir is not None else _data_directory()
        self.binary = Path(binary) if binary is not None else _binary()
        self.lock = threading.RLock()
        self.config_lock = threading.Lock()
        self.worker = None
        self.stop_event = threading.Event()
        self.closed = False
        self.accounts_cache = None
        self.private_connection = None
        self.connection = {'running': False, 'phase': 'ready', 'message': '', 'question': None}
        self.scan = {'running': False, 'phase': 'ready', 'accountId': '', 'path': '', 'message': '',
                     'stopped': False, 'totalFiles': 0, 'hashedFiles': 0, 'skippedFiles': 0,
                     'groupCount': 0, 'duplicateBytes': 0, 'groups': [], 'truncated': False}
        self.files = {}
        self.action_plans = {}
        self.action = {'running': False, 'phase': 'ready', 'done': 0, 'total': 0, 'message': ''}

    def _accounts(self):
        if self.accounts_cache is None:
            path = self.directory / 'accounts.json'
            try:
                if path.is_symlink() or path.stat().st_size > 64 * 1024:
                    raise CloudError('The saved cloud account list is invalid.')
                entries = json.loads(path.read_text('utf-8'))
            except FileNotFoundError:
                entries = []
            except (ValueError, OSError):
                raise CloudError('The saved cloud account list could not be read.') from None
            if not isinstance(entries, list):
                raise CloudError('The saved cloud account list is invalid.')
            self.accounts_cache = [
                {'id': x['id'], 'provider': x['provider'], 'label': str(x.get('label', ''))[:80],
                 'writeEnabled': x.get('writeEnabled') is True}
                for x in entries if isinstance(x, dict) and x.get('provider') in PROVIDERS
                and isinstance(x.get('id'), str) and re.fullmatch('mr_[0-9a-f]{32}', x['id'])
            ][:50]
        return self.accounts_cache

    def _save_accounts(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / 'accounts.json'
        temporary = path.with_suffix('.tmp')
        if path.is_symlink() or temporary.is_symlink():
            raise CloudError('The cloud account storage is linked and cannot be used.')
        temporary.write_text(json.dumps(self._accounts()), encoding='utf-8')
        temporary.replace(path)

    def _account(self, account_id):
        found = next((a for a in self._accounts() if a['id'] == account_id), None)
        if found is None:
            raise CloudError('Choose one of your connected cloud accounts.')
        return dict(found)

    def state(self):
        with self.lock:
            return {'available': self.binary is not None and self.binary.is_file(),
                    'accounts': [dict(a, providerName=PROVIDERS[a['provider']]) for a in self._accounts()],
                    'connection': copy.deepcopy(self.connection), 'scan': copy.deepcopy(self.scan),
                    'action': copy.deepcopy(self.action),
                    'readOnly': not any(a.get('writeEnabled') for a in self._accounts()),
                    'managementAvailable': True,
                    'credentialProtection': 'Windows account encryption' if sys.platform == 'win32' else 'File permissions'}

    def _idle(self):
        if self.closed:
            raise CloudError('Media Review is closing.')
        if self.binary is None or not self.binary.is_file():
            raise CloudError('The cloud connector is missing. Use the complete Media Review release folder.')
        if self.worker is not None and self.worker.is_alive():
            raise CloudError('Wait for the current cloud action to finish, or stop it first.')

    @contextmanager
    def _configuration(self):
        # rclone may refresh tokens on any API call. Serialize access and seal
        # the updated config again even when a listing is cancelled.
        with self.config_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            sealed = self.directory / 'connections.dat'
            if sealed.is_symlink():
                raise CloudError('The cloud connection store cannot be a link.')
            plain = _protect(sealed.read_bytes(), decrypt=True) if sealed.exists() else b''
            descriptor, temporary_name = tempfile.mkstemp(prefix='oauth-', suffix='.conf', dir=self.directory)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(plain)
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                yield temporary
            finally:
                try:
                    updated = temporary.read_bytes()
                    if updated:
                        encrypted = _protect(updated)
                        pending = self.directory / 'connections.pending'
                        if pending.is_symlink():
                            raise CloudError('The cloud connection store cannot be a link.')
                        pending.write_bytes(encrypted)
                        pending.replace(sealed)
                        try:
                            os.chmod(sealed, 0o600)
                        except OSError:
                            pass
                finally:
                    temporary.unlink(missing_ok=True)

    def _run(self, arguments, timeout=180, extra_env=None):
        if self.stop_event.is_set() or self.closed:
            raise CloudCancelled('Cloud action stopped.')
        with self._configuration() as config:
            env = {k: v for k, v in os.environ.items() if not k.startswith('RCLONE_')}
            env.update(extra_env or {})
            command = [str(self.binary), '--config', str(config), '--log-level', 'ERROR',
                       '--retries', '1', '--low-level-retries', '2', '--contimeout', '15s',
                       '--timeout', '45s', *arguments]
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                           env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if self.stop_event.wait(0.15) or self.closed:
                            raise CloudCancelled('Cloud action stopped.')
                        if time.monotonic() > deadline:
                            raise CloudError('The cloud service took too long. Try again when your connection is ready.')
                        if output.tell() > MAX_LIST_BYTES or errors.tell() > 1024 * 1024:
                            raise CloudError('This cloud listing is too large. Choose a smaller folder.')
                    output.seek(0)
                    data = output.read(MAX_LIST_BYTES + 1)
                    if len(data) > MAX_LIST_BYTES:
                        raise CloudError('This cloud listing is too large. Choose a smaller folder.')
                    if process.returncode:
                        # Provider stderr can contain tokens, IDs and URLs. It
                        # is deliberately not included in API replies/logs.
                        raise CloudError('The cloud action could not finish. Check your connection, account access and folder path.')
                    try:
                        return json.loads(data or b'{}')
                    except (ValueError, UnicodeDecodeError):
                        raise CloudError('The cloud connector returned an invalid response.') from None
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                    if extra_env:
                        # Backend-specific environment overrides are used for
                        # the initial OAuth call. Persist those application
                        # credentials inside the sealed, private config too,
                        # so a later token refresh uses the same OAuth client.
                        parser = configparser.RawConfigParser()
                        parser.read(config, encoding='utf-8')
                        for section in parser.sections():
                            prefix = 'RCLONE_CONFIG_' + section.upper() + '_'
                            for field in ('CLIENT_ID', 'CLIENT_SECRET'):
                                if prefix + field in extra_env:
                                    parser.set(section, field.lower(), extra_env[prefix + field])
                        with config.open('w', encoding='utf-8') as file:
                            parser.write(file)

    def connect(self, provider, label='', client_id='', client_secret=''):
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise CloudError('Choose Google Drive, OneDrive or Dropbox.')
        if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise CloudError('Enter an account label of at most 80 characters.')
        for value in (client_id, client_secret):
            if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
                raise CloudError('The OAuth application details are invalid.')
        if bool(client_id.strip()) != bool(client_secret.strip()):
            raise CloudError('Enter both the OAuth client ID and client secret, or leave both blank.')
        # The shared Google OAuth client is being retired in 2026. Make the
        # working requirement explicit rather than silently relying on it.
        if provider == 'drive' and not client_id.strip():
            raise CloudError('Google Drive needs a Desktop OAuth client ID and client secret. Add your Google application details, then connect.')
        with self.lock:
            self._idle()
            if len(self._accounts()) >= 50:
                raise CloudError('This workspace already has 50 cloud connections.')
            self.stop_event.clear()
            self.action.update(phase='ready', message='')
            account = {'id': 'mr_' + uuid.uuid4().hex, 'provider': provider,
                       'label': label.strip() or PROVIDERS[provider], 'writeEnabled': True}
            self.private_connection = {'account': account, 'state': '', 'option': None,
                                       'clientId': client_id.strip(), 'clientSecret': client_secret.strip()}
            self.connection = {'running': True, 'phase': 'authorizing', 'message': 'Complete sign-in in your browser. Media Review never asks for your account password.', 'question': None}
            self.worker = threading.Thread(target=self._connect_worker, args=(None,), daemon=True, name='cloud-connect')
            self.worker.start()
            return self.state()

    def _connect_worker(self, answer):
        try:
            with self.lock:
                pending = copy.deepcopy(self.private_connection)
            if pending is None:
                raise CloudCancelled('Connection cancelled.')
            account = pending['account']
            options = ['config_is_local', 'true']
            if account['provider'] == 'drive':
                options += ['scope', 'drive', 'config_change_team_drive', 'false']
            if answer is None:
                arguments = ['config', 'create', account['id'], account['provider'], *options, '--non-interactive']
            else:
                arguments = ['config', 'update', account['id'], *options, '--non-interactive', '--continue',
                             '--state', pending['state'], '--result', answer]
            env = {}
            if pending['clientId']:
                key = account['id'].upper()
                # Values travel through the child environment, not process
                # arguments, state responses, HTML or application logs.
                env[f'RCLONE_CONFIG_{key}_CLIENT_ID'] = pending['clientId']
                env[f'RCLONE_CONFIG_{key}_CLIENT_SECRET'] = pending['clientSecret']
            result = self._run(arguments, timeout=300, extra_env=env)
            if not isinstance(result, dict) or 'State' not in result or result.get('Error'):
                raise CloudError('The cloud connection could not be configured.')
            with self.lock:
                if self.closed or self.stop_event.is_set():
                    raise CloudCancelled('Connection cancelled.')
                state = result.get('State', '')
                if not state:
                    self._accounts().append(account)
                    self._save_accounts()
                    self.private_connection = None
                    self.connection = {'running': False, 'phase': 'connected', 'message': 'Account connected. Choose a folder, then click Scan cloud.', 'question': None}
                else:
                    option = result.get('Option')
                    if not isinstance(option, dict):
                        raise CloudError('The cloud provider requested an unsupported connection step.')
                    name = str(option.get('Name', ''))
                    # Credential/token pasting is never requested in the app.
                    if option.get('IsPassword') or name in {'config_token', 'token', 'client_secret'}:
                        raise CloudError('Browser authorization did not finish. Cancel and connect again using your browser.')
                    choices = [{'value': str(x.get('Value', ''))[:2048], 'label': str(x.get('Help', x.get('Value', '')))[:300]}
                               for x in option.get('Examples', [])[:100] if isinstance(x, dict)]
                    question = {'name': name[:100], 'help': str(option.get('Help', 'Choose the cloud connection option.'))[:3000],
                                'default': str(option.get('Default', ''))[:2048], 'choices': choices,
                                'exclusive': bool(option.get('Exclusive')), 'required': bool(option.get('Required'))}
                    self.private_connection['state'] = str(state)
                    self.private_connection['option'] = question
                    self.connection = {'running': False, 'phase': 'question', 'message': 'Finish connecting this account.', 'question': question}
        except CloudCancelled:
            with self.lock:
                self.private_connection = None
                self.connection = {'running': False, 'phase': 'cancelled', 'message': 'Connection cancelled.', 'question': None}
        except Exception as error:
            with self.lock:
                self.private_connection = None
                self.connection = {'running': False, 'phase': 'error', 'message': str(error) if isinstance(error, CloudError) else 'The cloud account could not be connected. Try again.', 'question': None}

    def answer(self, answer):
        if not isinstance(answer, str) or len(answer) > 2048 or any(ord(c) < 32 for c in answer):
            raise CloudError('Choose a valid cloud connection option.')
        with self.lock:
            self._idle()
            if self.connection['phase'] != 'question' or self.private_connection is None:
                raise CloudError('There is no cloud connection question waiting for an answer.')
            option = self.private_connection['option']
            if option['required'] and not answer:
                raise CloudError('Choose an answer to continue.')
            if option['exclusive'] and answer not in {x['value'] for x in option['choices']}:
                raise CloudError('Choose one of the available connection options.')
            self.connection = {'running': True, 'phase': 'authorizing', 'message': 'Completing your cloud connection. Check your browser if sign-in is requested.', 'question': None}
            self.worker = threading.Thread(target=self._connect_worker, args=(answer,), daemon=True, name='cloud-connect')
            self.worker.start()
            return self.state()

    def cancel_connection(self):
        with self.lock:
            if self.connection['phase'] in {'authorizing', 'question'}:
                self.stop_event.set()
                self.private_connection = None
                self.connection = {'running': bool(self.worker and self.worker.is_alive()), 'phase': 'cancelling', 'message': 'Cancelling connection…', 'question': None}
                if not self.connection['running']:
                    self.connection.update(phase='cancelled', message='Connection cancelled.')
            return self.state()

    def folders(self, account_id, path=''):
        path = _path(path)
        with self.lock:
            self._idle()
            if self.private_connection is not None:
                raise CloudError('Finish or cancel the cloud connection first.')
            account = self._account(account_id)
            self.stop_event.clear()
            # Reserve the service while the HTTP thread performs the listing.
            self.worker = threading.current_thread()
        try:
            result = self._run(['lsjson', account['id'] + ':' + path, '--dirs-only', '--max-depth', '1', '--no-mimetype'])
            if not isinstance(result, list):
                raise CloudError('The cloud folders could not be read.')
            folders = []
            for item in result[:5000]:
                if not isinstance(item, dict) or not item.get('IsDir'):
                    continue
                relative = _path(str(item.get('Path', '')))
                if not relative:
                    continue
                folders.append({'name': str(item.get('Name', relative))[:512], 'path': '/'.join(x for x in (path, relative) if x)})
            return {'accountId': account_id, 'path': path, 'folders': folders, 'truncated': len(result) > 5000}
        finally:
            with self.lock:
                self.worker = None

    def start_scan(self, account_id, path='', recursive=True):
        path = _path(path)
        if not isinstance(recursive, bool):
            raise CloudError('Include cloud subfolders must be true or false.')
        with self.lock:
            self._idle()
            if self.private_connection is not None:
                raise CloudError('Finish or cancel the cloud connection first.')
            account = self._account(account_id)
            self.stop_event.clear()
            self.action_plans.clear()
            self.action.update(phase='ready', message='')
            self.files = {}
            self.scan = {'running': True, 'phase': 'listing', 'accountId': account_id, 'path': path,
                         'message': 'Reading cloud file names, sizes and server checksums. Media files are not downloaded.',
                         'stopped': False, 'totalFiles': 0, 'hashedFiles': 0, 'skippedFiles': 0,
                         'groupCount': 0, 'duplicateBytes': 0, 'groups': [], 'truncated': False}
            self.worker = threading.Thread(target=self._scan_worker, args=(account, path, recursive), daemon=True, name='cloud-scan')
            self.worker.start()
            return self.state()

    def _scan_worker(self, account, path, recursive):
        try:
            args = ['lsjson', account['id'] + ':' + path, '--files-only', '--hash', '--no-mimetype']
            args += ['--recursive'] if recursive else ['--max-depth', '1']
            if account['provider'] == 'drive':
                args += ['--drive-skip-gdocs', '--drive-skip-shortcuts']
            result = self._run(args, timeout=3600)
            if not isinstance(result, list):
                raise CloudError('The cloud file listing is invalid.')
            buckets = defaultdict(list)
            total, hashed, skipped = 0, 0, 0
            for item in result[:MAX_FILES]:
                if self.stop_event.is_set() or self.closed:
                    break
                if not isinstance(item, dict) or item.get('IsDir'):
                    continue
                total += 1
                signature = _hash(item)
                size = item.get('Size')
                try:
                    relative = _path(str(item.get('Path', '')))
                except CloudError:
                    skipped += 1
                    continue
                if signature is None or not relative or not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    skipped += 1
                    continue
                hashed += 1
                full_path = '/'.join(x for x in (path, relative) if x)
                if full_path.split('/')[0].casefold() == RECOVERY_FOLDER.casefold():
                    continue
                identity = hashlib.sha256((account['id'] + '\0' + str(item.get('ID', '')) + '\0' + full_path).encode()).hexdigest()[:24]
                file = {'id': identity, 'name': str(item.get('Name', relative.rsplit('/', 1)[-1]))[:512],
                        'path': full_path, 'size': size, 'modified': str(item.get('ModTime', ''))[:100],
                        'provider': account['provider'], 'remoteId': str(item.get('ID', ''))[:2048],
                        'hashType': signature[0], 'hashValue': signature[1], 'possible': signature[2]}
                buckets[(signature[0], signature[1], size, signature[2])].append(file)
            groups = []
            for (algorithm, value, size, possible), files in buckets.items():
                # A provider can return the same object through more than one
                # listing path. Such aliases do not represent extra copies.
                unique = {f['remoteId'] or f['path']: f for f in files}
                files = list(unique.values())
                if len(files) > 1:
                    group_id = hashlib.sha256((algorithm + value + str(size)).encode()).hexdigest()[:24]
                    groups.append({'id': group_id, 'hashType': algorithm, 'size': size, 'possible': possible,
                                   'duplicateBytes': size * (len(files) - 1), 'files': sorted(files, key=lambda f: f['path'].casefold())})
            groups.sort(key=lambda g: (-g['duplicateBytes'], g['id']))
            all_group_count = len(groups)
            groups = groups[:MAX_GROUPS]
            with self.lock:
                self.files = {f['id']: dict(f, accountId=account['id']) for g in groups for f in g['files']}
                self.scan.update(running=False, phase='stopped' if self.stop_event.is_set() else 'complete',
                                 stopped=self.stop_event.is_set(), totalFiles=total, hashedFiles=hashed, skippedFiles=skipped,
                                 groupCount=all_group_count, groups=groups, duplicateBytes=sum(g['duplicateBytes'] for g in groups),
                                 truncated=len(result) > MAX_FILES or all_group_count > MAX_GROUPS,
                                 message='Cloud scan stopped. Results may be incomplete.' if self.stop_event.is_set() else 'Cloud scan complete. Matches use server checksums and sizes; file bytes were not downloaded or verified locally.')
        except CloudCancelled:
            with self.lock:
                self.scan.update(running=False, phase='stopped', stopped=True,
                                 message='Cloud scan stopped before its listing finished. Scan again to read the selected folder.')
        except Exception as error:
            with self.lock:
                self.scan.update(running=False, phase='error',
                                 message=str(error) if isinstance(error, CloudError) else 'The cloud scan could not finish. Try again.')

    def _management_account(self, account_id):
        account = self._account(account_id)
        if not account.get('writeEnabled'):
            raise CloudError('Reconnect this account with Media Review 3.0 to allow confirmed cloud moves. Its existing connection is read-only.')
        return account

    def _listing(self, account_id, folder):
        result = self._run(['lsjson', account_id + ':' + folder, '--max-depth', '1', '--hash', '--no-mimetype'])
        if not isinstance(result, list):
            raise CloudError('The destination folder could not be checked. Browse to an existing cloud folder.')
        return result

    def _verified(self, account_id, record, path=None):
        """Require the same remote identity, checksum, size and modification time."""
        path = _path(path if path is not None else record['path'])
        item = self._run(['lsjson', account_id + ':' + path, '--stat', '--hash', '--no-mimetype'])
        expected = (record.get('hashType'), record.get('hashValue'), bool(record.get('possible')))
        if (not isinstance(item, dict) or item.get('IsDir') or _hash(item) != expected
                or item.get('Size') != record['size'] or not record.get('remoteId')
                or str(item.get('ID', '')) != record['remoteId']
                or str(item.get('ModTime', '')) != record.get('modified', '')):
            raise CloudError('A cloud file changed or its identity could not be verified. Scan again before moving it.')
        # Drive permits same-name objects in a folder. A path-based adapter
        # cannot safely choose between them, even when one stat result matches.
        parent, _, name = path.rpartition('/')
        siblings = [f for f in self._listing(account_id, parent)
                    if str(f.get('Name', f.get('Path', ''))).casefold() == name.casefold()]
        if len(siblings) != 1 or str(siblings[0].get('ID', '')) != record['remoteId']:
            raise CloudError('This cloud path has ambiguous file names. Rename the conflicting objects in the provider before moving them.')
        return item

    @staticmethod
    def _public_action(plan):
        return {key: copy.deepcopy(plan[key]) for key in
                ('id', 'action', 'accountId', 'accountLabel', 'providerName', 'destination', 'files', 'totalSize', 'possibleMatches')}

    def action_plan(self, file_ids, action='trash', destination=''):
        """Network reads only. The returned plan is the exact later confirmation."""
        if (not isinstance(file_ids, list) or not 1 <= len(file_ids) <= ACTION_LIMIT
                or any(not isinstance(i, str) for i in file_ids) or len(set(file_ids)) != len(file_ids)):
            raise CloudError('Select between 1 and 100 different cloud files.')
        if action not in {'trash', 'move', 'type', 'year'}:
            raise CloudError('Choose cloud trash, move, organize by file type or organize by year.')
        destination = _path(destination)
        if destination.split('/')[0].casefold() == RECOVERY_FOLDER.casefold():
            raise CloudError('Choose a destination outside the Media Review cloud recovery folder.')
        with self.lock:
            self._idle()
            if self.private_connection is not None:
                raise CloudError('Finish or cancel the cloud connection first.')
            selected = [copy.deepcopy(self.files.get(i)) for i in file_ids]
            if any(f is None for f in selected):
                raise CloudError('Select files from the current cloud results, then preview again.')
            accounts = {f['accountId'] for f in selected}
            if len(accounts) != 1:
                raise CloudError('Choose files from one cloud account at a time.')
            account_id = next(iter(accounts))
            account = self._management_account(account_id)
            keepers = []
            if action == 'trash':
                chosen = set(file_ids)
                for group in self.scan['groups']:
                    if chosen.intersection(f['id'] for f in group['files']):
                        remaining = [f for f in group['files'] if f['id'] not in chosen]
                        if not remaining:
                            raise CloudError('Keep at least one file in each matching cloud group.')
                        keepers.append(copy.deepcopy(remaining[0]))
            self.stop_event.clear()
            self.worker = threading.current_thread()
        try:
            plan_id = uuid.uuid4().hex
            # A new recovery batch preserves original names and hierarchy.
            base = RECOVERY_FOLDER + '/' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '-' + plan_id[:12]
            if action != 'trash':
                self._listing(account_id, destination)
                base = '/'.join(x for x in (destination, 'Media Review ' + {'move': 'moved', 'type': 'by type', 'year': 'by year'}[action]
                                           + ' ' + plan_id[:12]) if x)
            planned = []
            for file in selected:
                self._verified(account_id, file)
                if action == 'trash':
                    suffix = file['path']
                else:
                    name = file['path'].rsplit('/', 1)[-1]
                    if action == 'type':
                        suffix = (Path(name).suffix.lstrip('.').upper() or 'NO EXTENSION') + '/' + name
                    elif action == 'year':
                        modified = file.get('modified', '')
                        try:
                            year = str(datetime.fromisoformat(modified.replace('Z', '+00:00')).year)
                        except (ValueError, TypeError):
                            year = 'Unknown year'
                        suffix = year + '/' + name
                    else:
                        suffix = name
                target = _path(base + '/' + suffix)
                # Same basenames from different folders receive a stable suffix.
                if target.casefold() in {p['destination'].casefold() for p in planned}:
                    path = Path(target)
                    target = str(path.with_name(path.stem + '-' + file['id'][:8] + path.suffix)).replace('\\', '/')
                planned.append(dict(file, source=file['path'], destination=target))
            for keeper in keepers:
                self._verified(account_id, keeper)
            plan = {'id': plan_id, 'created': time.monotonic(), 'action': action, 'accountId': account_id,
                    'accountLabel': account['label'], 'providerName': PROVIDERS[account['provider']],
                    'destination': base, 'files': planned, 'keepers': keepers,
                    'totalSize': sum(f['size'] for f in planned), 'possibleMatches': any(f.get('possible') for f in selected)}
            with self.lock:
                self.action_plans = {k: v for k, v in self.action_plans.items()
                                     if time.monotonic() - v['created'] <= ACTION_PLAN_SECONDS}
                if len(self.action_plans) >= 8:
                    self.action_plans.pop(next(iter(self.action_plans)))
                self.action_plans[plan_id] = plan
            return self._public_action(plan)
        finally:
            with self.lock:
                self.worker = None

    def _history(self):
        with self.lock:
            path = self.directory / 'cloud-moves.json'
            try:
                if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                    raise CloudError('Cloud move history could not be opened safely.')
                data = json.loads(path.read_text('utf-8'))
                if not isinstance(data, list) or len(data) > 1000:
                    raise ValueError()
                return data
            except FileNotFoundError:
                return []
            except (OSError, ValueError):
                raise CloudError('Cloud move history could not be read. Keep the cloud recovery folder unchanged.') from None

    def _save_history(self, history):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / 'cloud-moves.json'
            temporary = self.directory / 'cloud-moves.pending'
            if path.is_symlink() or temporary.is_symlink():
                raise CloudError('Cloud move history cannot use linked files.')
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(history, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)

    def action_history(self):
        with self.lock:
            return {'operations': [{'id': h['id'], 'action': h['action'], 'accountLabel': h['accountLabel'],
                                    'created': h['created'], 'destination': h['destination'],
                                    'moved': sum(f.get('status') in {'moved', 'uncertain', 'restoring'} for f in h['files']),
                                    'restored': sum(f.get('status') == 'restored' for f in h['files']),
                                    'files': [{'source': f['source'], 'destination': f['destination'], 'status': f.get('status', '')}
                                              for f in h['files']]}
                                   for h in reversed(self._history())]}

    def execute_action(self, plan_id, confirmed=False):
        if confirmed is not True:
            raise CloudError('Review the complete cloud file list and confirm the move first.')
        with self.lock:
            self._idle()
            plan = self.action_plans.get(plan_id) if isinstance(plan_id, str) else None
            if plan is None or time.monotonic() - plan['created'] > ACTION_PLAN_SECONDS:
                raise CloudError('This cloud preview expired. Preview the files again.')
            self._management_account(plan['accountId'])
            if any(f['id'] not in self.files or self.files[f['id']]['path'] != f['source'] for f in plan['files']):
                raise CloudError('The cloud results changed. Preview the files again.')
            history = self._history()
            if len(history) >= 1000:
                retained = [h for h in history if any(f.get('status') in {'moved', 'uncertain', 'restoring'}
                                                     for f in h['files'])]
                if len(retained) >= 1000:
                    raise CloudError('Cloud recovery history is full. Restore an existing batch before creating another.')
                self._save_history(retained)
            self.action_plans.clear()
            self.stop_event.clear()
            self.action = {'running': True, 'phase': 'checking', 'done': 0, 'total': len(plan['files']),
                           'message': 'Checking every source and keeper before moving cloud files…'}
            self.worker = threading.Thread(target=self._action_worker, args=(copy.deepcopy(plan), False),
                                           daemon=True, name='cloud-move')
            self.worker.start()
            return self.state()

    def restore_action(self, operation_id, confirmed=False):
        if confirmed is not True:
            raise CloudError('Review the cloud move history and confirm restoration first.')
        with self.lock:
            self._idle()
            operation = next((h for h in self._history() if h['id'] == operation_id), None)
            if operation is None or not any(f.get('status') in {'moved', 'uncertain', 'restoring'} for f in operation['files']):
                raise CloudError('Choose a cloud move with files available to restore.')
            self._management_account(operation['accountId'])
            self.stop_event.clear()
            self.action_plans.clear()
            self.action = {'running': True, 'phase': 'checking', 'done': 0,
                           'total': sum(f.get('status') in {'moved', 'uncertain', 'restoring'} for f in operation['files']),
                           'message': 'Checking cloud files before restoring their original paths…'}
            self.worker = threading.Thread(target=self._action_worker, args=(operation, True),
                                           daemon=True, name='cloud-restore')
            self.worker.start()
            return self.state()

    def _action_worker(self, operation, restore):
        history = []
        try:
            account_id = operation['accountId']
            records = [f for f in operation['files'] if not restore or f.get('status') in {'moved', 'uncertain', 'restoring'}]
            for keeper in operation.get('keepers', []) if not restore else []:
                self._verified(account_id, keeper)
            # All selected originals must be stable before starting a new batch.
            if not restore:
                for record in records:
                    self._verified(account_id, record)
                operation['created'] = datetime.now(timezone.utc).isoformat()
                operation.pop('keepers', None)
                for record in records:
                    record['status'] = 'pending'
                history = self._history()
                history.append(operation)
                self._save_history(history)
            else:
                history = self._history()
                history = [operation if h['id'] == operation['id'] else h for h in history]
            for index, record in enumerate(records):
                source = record['destination'] if restore else record['source']
                destination = record['source'] if restore else record['destination']
                snapshot = record.get('movedSnapshot', record) if restore else record
                if restore and record.get('status') in {'uncertain', 'restoring'}:
                    # A restart can happen after a successful rename but before
                    # the history commit. Recognize an already-restored original
                    # by its full provider identity rather than moving it twice.
                    try:
                        self._verified(account_id, snapshot, destination)
                    except CloudError:
                        pass
                    else:
                        record['status'] = 'restored'
                        self._save_history(history)
                        with self.lock:
                            self.action['done'] = index + 1
                        continue
                self._verified(account_id, snapshot, source)
                # Refuse all collisions, including a duplicate with identical bytes.
                parent, _, name = destination.rpartition('/')
                if restore:
                    listing = self._listing(account_id, parent)
                    if any(str(f.get('Name', f.get('Path', ''))).casefold() == name.casefold() for f in listing):
                        raise CloudError('An original cloud path is occupied. Nothing at that path was overwritten.')
                else:
                    self._run(['mkdir', account_id + ':' + parent])
                    listing = self._listing(account_id, parent)
                    if any(str(f.get('Name', f.get('Path', ''))).casefold() == name.casefold() for f in listing):
                        raise CloudError('A destination already exists. Nothing at that path was overwritten.')
                record['status'] = 'restoring' if restore else 'uncertain'
                self._save_history(history)
                with self.lock:
                    self.action.update(phase='restoring' if restore else 'moving', done=index,
                                       message=('Restoring ' if restore else 'Moving ') + record['name'])
                self._run(['moveto', account_id + ':' + source, account_id + ':' + destination,
                           '--ignore-existing', '--no-traverse', '--checksum'], timeout=300)
                # rclone's ignore-existing leaves a skipped source unchanged. A
                # matching destination alone is insufficient to claim success.
                remaining = self._listing(account_id, source.rpartition('/')[0])
                if any(str(f.get('Name', f.get('Path', ''))).casefold() == source.rsplit('/', 1)[-1].casefold()
                       for f in remaining):
                    raise CloudError('The cloud source is still present. The move was not confirmed; inspect the listed paths.')
                moved = self._run(['lsjson', account_id + ':' + destination, '--stat', '--hash', '--no-mimetype'])
                if not isinstance(moved, dict) or _hash(moved) != (record['hashType'], record['hashValue'], record['possible']) or moved.get('Size') != record['size']:
                    raise CloudError('The destination could not be verified. Use the cloud move history to inspect both paths.')
                record['status'] = 'restored' if restore else 'moved'
                if not restore:
                    record['movedSnapshot'] = dict(record, remoteId=str(moved.get('ID', '')), modified=str(moved.get('ModTime', '')))
                    record['movedSnapshot'].pop('movedSnapshot', None)
                self._save_history(history)
                with self.lock:
                    self.files.pop(record['id'], None)
                    for group in self.scan['groups']:
                        group['files'] = [f for f in group['files'] if f['id'] != record['id']]
                    self.scan['groups'] = [g for g in self.scan['groups'] if len(g['files']) > 1]
                    for group in self.scan['groups']:
                        group['duplicateBytes'] = group['size'] * (len(group['files']) - 1)
                    self.scan['groupCount'] = len(self.scan['groups'])
                    self.scan['duplicateBytes'] = sum(g['duplicateBytes'] for g in self.scan['groups'])
                    self.action['done'] = index + 1
            with self.lock:
                self.action.update(running=False, phase='complete', message=(
                    'Cloud files restored. Scan again to refresh the review.' if restore else
                    'Cloud move complete. Original paths and recovery details are saved below.'))
        except Exception as error:
            if restore and history:
                for record in operation['files']:
                    if record.get('status') == 'restoring':
                        record['status'] = 'uncertain'
                try:
                    self._save_history(history)
                except Exception:
                    pass
            with self.lock:
                self.action.update(running=False, phase='error', message=(str(error) if isinstance(error, CloudError)
                                   else 'Cloud move interrupted. Inspect the cloud move history before continuing.'))

    def stop_scan(self):
        with self.lock:
            if self.scan['running']:
                self.stop_event.set()
                self.scan.update(phase='stopping', message='Stopping cloud scan…')
            return self.state()

    def open_file(self, file_id):
        if not isinstance(file_id, str) or not re.fullmatch('[0-9a-f]{24}', file_id):
            raise CloudError('Choose a file from the current cloud matches.')
        with self.lock:
            file = self.files.get(file_id)
            if file is None:
                raise CloudError('Choose a file from the current cloud matches.')
            provider = file['provider']
            remote_id = file['remoteId']
            if provider == 'drive' and re.fullmatch('[A-Za-z0-9_-]{1,200}', remote_id):
                url = 'https://drive.google.com/file/d/' + quote(remote_id, safe='') + '/view'
            elif provider == 'dropbox':
                url = 'https://www.dropbox.com/home/' + quote(file['path'], safe='/')
            elif provider == 'onedrive':
                # Web UI lookup does not create a public/shared link or fetch
                # file contents. The account browser can ask for sign-in.
                url = 'https://onedrive.live.com/?qt=search&q=' + quote(file['name'], safe='')
            else:
                raise CloudError('This file cannot be opened in the cloud browser.')
        webbrowser.open(url)
        return {'ok': True}

    def close(self):
        with self.lock:
            self.closed = True
            self.stop_event.set()
            worker = self.worker
            self.private_connection = None
        if worker and worker is not threading.current_thread():
            worker.join(timeout=8)
