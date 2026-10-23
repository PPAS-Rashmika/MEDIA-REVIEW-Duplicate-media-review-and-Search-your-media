"""Explicitly prepare the verified Windows cloud connector; never run at startup."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

VERSION = '1.75.2'
ARCHIVE = f'rclone-v{VERSION}-windows-amd64.zip'
URL = f'https://github.com/rclone/rclone/releases/download/v{VERSION}/{ARCHIVE}'
SHA256 = '8a9db2bad1dc79a65b3348fd857069598e2aab7cf4d118b77b1df23055aff818'
LICENSE_URL = f'https://raw.githubusercontent.com/rclone/rclone/v{VERSION}/COPYING'
LICENSE_SHA256 = '8cd2e9e750b90a04b7d82dbbca3930c696ae0309d7c10464f90a44f45754cd04'
ROOT = Path(__file__).resolve().parents[1]


def prepare_rclone():
    request = urllib.request.Request(URL, headers={'User-Agent': 'MediaReview-build/2.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(80 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('The cloud connector download did not match its pinned SHA-256 checksum.')
    prefix = ARCHIVE.removesuffix('.zip') + '/'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        binary = archive.read(prefix + 'rclone.exe')
    request = urllib.request.Request(LICENSE_URL, headers={'User-Agent': 'MediaReview-build/2.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        notice = response.read(64 * 1024)
    if hashlib.sha256(notice).hexdigest() != LICENSE_SHA256:
        raise SystemExit('The cloud connector license did not match its pinned SHA-256 checksum.')
    target_dir = ROOT / 'bin'
    target_dir.mkdir(exist_ok=True)
    temporary = target_dir / 'rclone.exe.tmp'
    temporary.write_bytes(binary)
    temporary.replace(target_dir / 'rclone.exe')
    (target_dir / 'Rclone-LICENSE.txt').write_bytes(notice)
    print(f'Prepared rclone {VERSION}; official archive SHA-256 verified.')
    return target_dir / 'rclone.exe'


if __name__ == '__main__':
    prepare_rclone()
