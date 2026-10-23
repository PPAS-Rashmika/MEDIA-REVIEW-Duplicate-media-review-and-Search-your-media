"""Export a source-only repository and ZIP without local media or diagnostics."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "app.py", "launcher.py", "scanner.py", "storage.py", "version.py", "image_filters.py", "visual_search.py", "organizer.py",
    'scan_options.py', 'archive_scan.py', 'audio_fingerprint.py', 'metadata_match.py',
    'session_store.py', 'cloud_scan.py', 'advanced_runtime_check.py', 'v3_runtime_check.py',
    'file_tools.py', 'image_transforms.py', 'scan_cache.py', 'video_match.py', 'workspace.py',
    "build_windows.py", "app.ico", "requirements.txt", "requirements-dev.txt",
    "README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
    "THIRD_PARTY_NOTICES.md", ".gitignore", ".gitattributes",
    "bin/FFmpeg-LICENSE.txt",
    'bin/Rclone-LICENSE.txt', 'bin/7zip/License.txt', 'bin/7zip/readme.txt', 'bin/7zip/History.txt',
)
DIRECTORIES = ("static", "assets", ".github", "licenses", "docs")
EXCLUDED_DIRECTORIES = {
    '__pycache__', '.git', '.venv', 'node_modules', 'vendor', 'build', 'dist',
    '.pytest_cache', 'ui-fixtures', 'media', 'photos', 'sample-media', 'test-media',
    '.photo-review-trash', '.media-review-organize', 'scan-cache',
}
EXCLUDED_SUFFIXES = {'.exe', '.dll', '.pyc', '.pyo', '.onnx', '.log', '.tmp', '.sqlite', '.sqlite3'}


def include_directory_file(path):
    """Keep deliberate source assets, never runtime output or copied fixtures."""
    relative = path.relative_to(ROOT)
    return (path.is_file() and not path.is_symlink()
            and not EXCLUDED_DIRECTORIES.intersection(relative.parts)
            and path.suffix.casefold() not in EXCLUDED_SUFFIXES)


def export_source(destination, archive):
    destination = Path(destination).resolve()
    archive = Path(archive).resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Export into a separate folder outside the working source directory.")
    if archive == ROOT or ROOT in archive.parents or archive == destination or destination in archive.parents:
        raise ValueError("Save the ZIP outside both the working source and export folders.")
    if archive.exists():
        raise ValueError("The archive already exists; choose a new name to preserve it.")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("The export folder must be empty; existing files will not be overwritten.")
    files = [ROOT / name for name in ROOT_FILES]
    files.extend(path for directory in DIRECTORIES for path in (ROOT / directory).rglob("*")
                 if include_directory_file(path))
    files.extend((ROOT / "tests").glob("test_*.py"))
    files.extend((ROOT / "tests").glob("test_*.cjs"))
    files.extend((ROOT / "tools").glob("*.py"))
    files.append(ROOT / "tests" / "__init__.py")
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        raise ValueError("Missing source files: " + ", ".join(missing))
    destination.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for source in sorted(set(files)):
            relative = source.relative_to(ROOT)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            output.write(target, Path("media-review") / relative)
    print(f"Exported {len(set(files))} source files to {destination.name}/ and {archive.name}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT.parent / "media-review")
    parser.add_argument("--archive", type=Path, default=ROOT.parent / "media-review-source.zip")
    args = parser.parse_args()
    export_source(args.output, args.archive)


if __name__ == "__main__":
    main()
