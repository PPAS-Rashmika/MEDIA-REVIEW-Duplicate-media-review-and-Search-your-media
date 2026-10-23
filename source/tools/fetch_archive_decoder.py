"""Explicit, verified preparation of 7-Zip's portable archive decoder.

Extract the official installer with the official reduced CLI; never install it.
Only 7z.exe, 7z.dll and the upstream license/notice files are retained.
"""
from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import urllib.request

VERSION = '26.04'
ASSETS = {
    '7zr.exe': ('256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282', 602624),
    '7z2604-x64.exe': ('d54bf805f9f3704d1e8db2fa3498ae7ef2df0312b40b558e7c71c734430a665d', 1664082),
}


def prepare():
    root = Path(__file__).resolve().parent.parent
    destination = root / 'bin' / '7zip'
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='media-review-7zip-') as directory:
        stage = Path(directory)
        for name, (expected_hash, expected_size) in ASSETS.items():
            url = f'https://github.com/ip7z/7zip/releases/download/{VERSION}/{name}'
            request = urllib.request.Request(url, headers={'User-Agent': 'MediaReview-build'})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read(expected_size + 1)
            if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError(f'Verification failed for official archive decoder asset {name}.')
            (stage / name).write_bytes(data)
        unpacked = stage / 'unpacked'
        subprocess.run([str(stage / '7zr.exe'), 'x', '-y', '-o' + str(unpacked),
                        str(stage / '7z2604-x64.exe')], check=True, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for name in ('7z.exe', '7z.dll', 'License.txt', 'readme.txt', 'History.txt'):
            source = unpacked / name
            if not source.is_file():
                raise ValueError('The verified archive decoder package is missing ' + name)
            shutil.copyfile(source, destination / name)
    print(f'Prepared 7-Zip {VERSION}: {destination}')


if __name__ == '__main__':
    prepare()
