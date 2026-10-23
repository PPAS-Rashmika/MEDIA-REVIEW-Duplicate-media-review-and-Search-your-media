"""Bounded, read-only ZIP/RAR/7z/TAR access without path-based extraction."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
import os
import stat
import queue
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

MAX_MEMBERS = 10000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_RATIO = 200
ARCHIVE_SUFFIXES = ('.zip', '.7z', '.rar', '.tar', '.tgz', '.tar.gz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')


class ArchiveError(ValueError):
    pass


class ArchiveCancelled(Exception):
    pass


@dataclass(frozen=True)
class Member:
    name: str
    size: int
    modified: float


def _check(should_stop):
    if should_stop and should_stop():
        raise ArchiveCancelled()


def _signature(path):
    info = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ArchiveError('Linked archives are not scanned.')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def is_archive(path):
    return str(path).lower().endswith(ARCHIVE_SUFFIXES)


def safe_name(name):
    normalized = name.replace('\\', '/')
    parts = PurePosixPath(normalized).parts
    return bool(name and len(name) <= 2048 and not any(ord(c) < 32 for c in name) and ':' not in name
                and not normalized.startswith('/') and not any(p in ('..', '.') for p in normalized.split('/'))
                and parts)


def archive_decoder():
    for root in (Path(__file__).resolve().parent, Path(sys.executable).parent):
        binary = root / 'bin' / '7zip' / '7z.exe'
        library = binary.with_name('7z.dll')
        if binary.is_file() and library.is_file() and not binary.is_symlink() and not library.is_symlink():
            return binary
    return None


def _run_decoder(arguments, maximum, should_stop=None, destination=None):
    """Drain bounded stdout with a responsive coordinator; never extract paths."""
    binary = archive_decoder()
    if binary is None:
        raise ArchiveError('RAR and 7z scanning needs the bundled 7-Zip decoder.')
    _check(should_stop)
    process = subprocess.Popen([str(binary), *arguments], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    chunks = queue.Queue(maxsize=4)
    cancelled = threading.Event()

    def read_output():
        try:
            while not cancelled.is_set():
                block = process.stdout.read(65536)
                while not cancelled.is_set():
                    try:
                        chunks.put(block, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                if not block:
                    return
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read_output, daemon=True, name='archive-output')
    reader.start()
    size, data, started = 0, bytearray(), time.monotonic()
    try:
        while True:
            _check(should_stop)
            if time.monotonic() - started > 240:
                raise ArchiveError('Archive decoder exceeded its processing time limit.')
            try:
                block = chunks.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    raise ArchiveError('Archive decoder stopped before returning a complete result.')
                continue
            if not block:
                break
            size += len(block)
            if size > maximum:
                raise ArchiveError('Archive decoder exceeded the safe output size limit.')
            if destination is None:
                data.extend(block)
            else:
                destination.write(block)
        while process.poll() is None:
            _check(should_stop)
            if time.monotonic() - started > 240:
                raise ArchiveError('Archive decoder exceeded its processing time limit.')
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise ArchiveError('Cannot read this RAR or 7z archive. Encrypted, incomplete and unsupported archives are skipped.')
        _check(should_stop)
        return bytes(data) if destination is None else size
    finally:
        cancelled.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        reader.join(timeout=1)


def _list_decoder_members(container, before, should_stop):
    raw = _run_decoder(['l', '-slt', '-ba', '-sccUTF-8', '-pMEDIA_REVIEW_NO_PASSWORD',
                        '--', str(container)], 8 * 1024 * 1024, should_stop)
    try:
        text = raw.decode('utf-8', 'strict').replace('\r', '')
    except UnicodeDecodeError as error:
        raise ArchiveError('Archive member names could not be read safely.') from error
    members, errors, seen, total = [], [], set(), 0
    records = [record for record in text.split('\n\n') if record.strip()]
    if len(records) > MAX_MEMBERS:
        raise ArchiveError('Archive contains too many entries.')
    for record in records:
        _check(should_stop)
        values = {}
        for line in record.splitlines():
            if ' = ' not in line:
                raise ArchiveError('Archive member names contain unsupported control characters.')
            key, value = line.split(' = ', 1)
            if key in values:
                raise ArchiveError('Ambiguous archive directory entry skipped.')
            values[key] = value
        name = values.get('Path', '')
        attributes = values.get('Attributes', '')
        if values.get('Folder') == '+' or attributes.startswith('D'):
            continue
        issue = None
        try:
            size = int(values.get('Size', '-1'))
        except ValueError as error:
            raise ArchiveError('Archive member size is invalid.') from error
        if not safe_name(name) or any(values.get(k) for k in ('Symbolic Link', 'Hard Link', 'Reparse Point')):
            issue = 'Unsafe path or linked archive member skipped.'
        elif any(part.startswith('l') for part in attributes.split()):
            issue = 'Linked archive member skipped.'
        elif values.get('Encrypted') == '+':
            issue = 'Encrypted archive member skipped.'
        elif size < 0 or size > MAX_MEMBER_BYTES:
            raise ArchiveError('Archive member exceeds the 256 MB size limit.')
        elif is_archive(name):
            issue = 'Nested archive skipped.'
        elif name in seen:
            raise ArchiveError('Archive has ambiguous duplicate member names.')
        method = values.get('Method', '')
        for dictionary_bits in re.findall(r'LZMA2?:(\d+)', method):
            if int(dictionary_bits) > 28:
                raise ArchiveError('Archive decoder dictionary exceeds the 256 MB safety limit.')
        if issue:
            errors.append({'name': name[:2048], 'message': issue})
            continue
        seen.add(name)
        total += size
        if total > MAX_TOTAL_BYTES or total > max(1024 * 1024, before[2] * MAX_RATIO):
            raise ArchiveError('Archive expanded size exceeds the safe limit.')
        modified = before[3] / 1e9
        if values.get('Modified'):
            try:
                modified = datetime.fromisoformat(values['Modified']).timestamp()
            except (ValueError, OverflowError, OSError):
                pass
        members.append(Member(name, size, modified))
    return members, errors


def list_members(container, should_stop=None):
    container = Path(container)
    before = _signature(container)
    members, errors, seen = [], [], set()
    total = 0
    try:
        if container.suffix.lower() in {'.rar', '.7z'}:
            members, errors = _list_decoder_members(container, before, should_stop)
        elif container.suffix.lower() == '.zip':
            with zipfile.ZipFile(container) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_MEMBERS:
                    raise ArchiveError(f'Archive has more than {MAX_MEMBERS:,} entries.')
                for item in entries:
                    _check(should_stop)
                    if item.is_dir():
                        continue
                    mode = item.external_attr >> 16
                    issue = None
                    if not safe_name(item.filename) or stat.S_ISLNK(mode):
                        issue = 'Unsafe path or link skipped.'
                    elif item.flag_bits & 1:
                        issue = 'Encrypted archive member skipped.'
                    elif item.file_size > MAX_MEMBER_BYTES or item.file_size > max(1024 * 1024, item.compress_size * MAX_RATIO):
                        issue = 'Archive member exceeds the safe size or compression-ratio limit.'
                    elif is_archive(item.filename):
                        issue = 'Nested archive skipped.'
                    elif item.filename in seen:
                        issue = 'Duplicate archive member name skipped.'
                    if issue:
                        errors.append({'name': item.filename[:2048], 'message': issue})
                        continue
                    seen.add(item.filename)
                    total += item.file_size
                    if total > MAX_TOTAL_BYTES:
                        raise ArchiveError('Archive expanded size exceeds the 1 GB scan limit.')
                    try:
                        modified = datetime(*item.date_time).timestamp()
                    except (ValueError, OverflowError, OSError):
                        modified = before[3] / 1e9
                    members.append(Member(item.filename, item.file_size, modified))
        else:
            with tarfile.open(container, mode='r:*') as archive:
                for index, item in enumerate(archive):
                    _check(should_stop)
                    if index >= MAX_MEMBERS:
                        raise ArchiveError(f'Archive has more than {MAX_MEMBERS:,} entries.')
                    if item.isdir():
                        continue
                    issue = None
                    if not item.isfile() or not safe_name(item.name):
                        issue = 'Unsafe path or non-regular archive member skipped.'
                    elif item.size > MAX_MEMBER_BYTES:
                        # Do not advance through a huge compressed TAR body to
                        # reach the next header after rejecting its size.
                        raise ArchiveError('Archive member exceeds the 256 MB size limit.')
                    elif is_archive(item.name):
                        issue = 'Nested archive skipped.'
                    elif item.name in seen:
                        issue = 'Duplicate archive member name skipped.'
                    if issue:
                        errors.append({'name': item.name[:2048], 'message': issue})
                        continue
                    seen.add(item.name)
                    total += item.size
                    if total > MAX_TOTAL_BYTES or total > max(1024 * 1024, before[2] * MAX_RATIO):
                        raise ArchiveError('Archive expanded size exceeds the safe limit.')
                    members.append(Member(item.name, item.size, float(item.mtime)))
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as error:
        raise ArchiveError('Cannot read this ZIP or TAR archive.') from error
    if _signature(container) != before:
        raise ArchiveError('Archive changed during discovery. Scan again.')
    return members, errors


@contextmanager
def materialize_member(container, member_name, expected_signature=None, should_stop=None):
    container = Path(container)
    before = _signature(container)
    if expected_signature is not None and tuple(expected_signature) != before:
        raise ArchiveError('Archive changed since it was scanned. Scan again.')
    if not safe_name(member_name) or is_archive(member_name):
        raise ArchiveError('This archive member is not supported.')
    _check(should_stop)
    with tempfile.TemporaryDirectory(prefix='media-review-member-') as directory:
        target = Path(directory) / ('member' + PurePosixPath(member_name.replace('\\', '/')).suffix[:32])
        if container.suffix.lower() in {'.rar', '.7z'}:
            members, _ = _list_decoder_members(container, before, should_stop)
            matching = [m for m in members if m.name == member_name]
            if len(matching) != 1:
                raise ArchiveError('Missing, ambiguous or unsafe archive member.')
            with target.open('wb') as destination:
                written = _run_decoder(['x', '-so', '-bd', '-y', '-spd', '-ssc', '-mmt2',
                                        '-pMEDIA_REVIEW_NO_PASSWORD', '--', str(container), member_name],
                                       matching[0].size, should_stop, destination)
            if written != matching[0].size or _signature(container) != before:
                raise ArchiveError('Archive changed or its member is incomplete. Scan again.')
            _check(should_stop)
            yield target
            if _signature(container) != before:
                raise ArchiveError('Archive changed while preparing its preview. Scan again.')
            return
        elif container.suffix.lower() == '.zip':
            archive = zipfile.ZipFile(container)
            entries = archive.infolist()
            if len(entries) > MAX_MEMBERS:
                archive.close()
                raise ArchiveError('Archive contains too many entries.')
            matches = [i for i in entries if i.filename == member_name]
            if len(matches) != 1:
                archive.close()
                raise ArchiveError('Missing or ambiguous archive member.')
            item = matches[0]
            if (item.is_dir() or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16)
                    or item.file_size > MAX_MEMBER_BYTES
                    or item.file_size > max(1024 * 1024, item.compress_size * MAX_RATIO)):
                archive.close()
                raise ArchiveError('Unsafe archive member skipped.')
            source = archive.open(item)
            size = item.file_size
        else:
            archive = tarfile.open(container, mode='r:*')
            matches = []
            total = 0
            for index, item in enumerate(archive):
                _check(should_stop)
                if index >= MAX_MEMBERS:
                    archive.close()
                    raise ArchiveError('Archive contains too many entries.')
                total += max(0, item.size)
                if (item.size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES
                        or total > max(1024 * 1024, before[2] * MAX_RATIO)):
                    archive.close()
                    raise ArchiveError('Archive expanded size exceeds the safe limit.')
                if item.name == member_name:
                    matches.append(item)
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > MAX_MEMBER_BYTES:
                archive.close()
                raise ArchiveError('Missing, ambiguous or unsafe archive member.')
            item = matches[0]
            source = archive.extractfile(item)
            size = item.size
        try:
            written = 0
            with source, target.open('wb') as destination:
                while True:
                    _check(should_stop)
                    block = source.read(min(1024 * 1024, size - written + 1))
                    if not block:
                        break
                    written += len(block)
                    if written > size or written > MAX_MEMBER_BYTES:
                        raise ArchiveError('Archive member exceeded its declared size.')
                    destination.write(block)
            if written != size:
                raise ArchiveError('Archive member is incomplete.')
            if _signature(container) != before:
                raise ArchiveError('Archive changed while reading a member. Scan again.')
            _check(should_stop)
            yield target
            if _signature(container) != before:
                raise ArchiveError('Archive changed while preparing its preview. Scan again.')
        finally:
            archive.close()
