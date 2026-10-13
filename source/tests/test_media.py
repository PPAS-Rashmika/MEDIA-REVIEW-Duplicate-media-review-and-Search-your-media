"""Media coverage uses generated fixtures; the user's media is never accessed."""

from __future__ import annotations

import http.client
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

try:
    from . import test_app as fixtures
except ImportError:
    import test_app as fixtures
import scanner


def write_wav(path):
    frames = b"".join(struct.pack("<h", int(8000 * math.sin(index * 2 * math.pi * 440 / 8000)))
                      for index in range(4000))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(frames)
    return path.read_bytes()


FFMPEG = next((path for path in (fixtures.APP_ROOT / "bin" / "ffmpeg.exe",
                               fixtures.APP_ROOT / "bin" / "ffmpeg") if path.is_file()), None)
if FFMPEG is None:
    configured = shutil.which("ffmpeg")
    FFMPEG = Path(configured) if configured else None


class MediaScannerTests(fixtures.FixtureCase):
    def test_all_file_types_have_exact_duplicate_detection_even_without_decoders(self):
        expected = {}
        for index, (extension, media_type) in enumerate(((".MP4", "video"), (".mxf", "video"),
                                                        (".mts", "video"), (".opus", "audio"),
                                                        (".aiff", "audio"), (".NEF", "image"),
                                                        (".unregistered", "other"), ("", "other"))):
            content = b"Fixture bytes with no installed decoder required. " + bytes([index])
            names = {"first-%d%s" % (index, extension), "second-%d%s" % (index, extension)}
            for name in names:
                (self.root / name).write_bytes(content)
                expected[name] = media_type
        result = scanner.scan_folder(self.root)
        self.assertEqual({photo.name: photo.mediaType for photo in result.photos.values()}, expected)
        self.assertEqual(len(result.groups), 8)
        self.assertTrue(all(group["kind"] == "exact" for group in result.groups))
        self.assertEqual(sum(len(group["photos"]) for group in result.groups), len(expected))
        self.assertTrue(all(photo.sha256 for photo in result.photos.values()))
        self.assertTrue(all(not photo.pixel_hash for photo in result.photos.values()))
        for photo in result.photos.values():
            self.assertIn(photo.previewMode, {"image", "video", "audio", "fallback"})
            self.assertIn("mime", photo.public())
        self.assertEqual(result.stats["total"], len(expected))

    def test_nonimages_never_go_through_the_image_decoder(self):
        for extension in (".mp4", ".mp3", ".opaque", ""):
            (self.root / ("fixture" + extension)).write_bytes(b"Exact comparison only")
        with patch.object(scanner.Image, "open", side_effect=AssertionError("Image decoder used for nonimage")) as decoder:
            result = scanner.scan_folder(self.root)
        decoder.assert_not_called()
        self.assertEqual(len(result.photos), 4)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["kind"], "exact")

    def test_known_media_filter_is_optional_and_recovery_files_stay_excluded(self):
        names = {"sound.mp3", "video.mxf", "camera.nef", "unknown.custom", "extensionless"}
        for name in names:
            (self.root / name).write_bytes(b"Same bytes across media file types")
        recovery = self.root / scanner.TRASH_NAME
        recovery.mkdir()
        (recovery / "discarded.custom").write_bytes(b"Same bytes across media file types")
        everything = scanner.scan_folder(self.root, include_all_files=True)
        known = scanner.scan_folder(self.root, include_all_files=False)
        self.assertEqual({photo.name for photo in everything.photos.values()}, names)
        self.assertEqual({photo.name for photo in known.photos.values()}, {"sound.mp3", "video.mxf", "camera.nef"})
        self.assertEqual(len(everything.groups), 1)
        self.assertEqual(len(known.groups), 1)


class MediaApiFixture(fixtures.ApiFixture):
    def media_request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request("GET", path, headers=dict(headers or {}))
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def native_audio(self):
        original = write_wav(self.root / "sound.wav")
        state = self.scan()
        photo = next(photo for photo in state["photos"] if photo["name"] == "sound.wav")
        self.assertEqual(photo["mediaType"], "audio")
        self.assertEqual(photo["previewMode"], "audio")
        return original, photo


class MediaApiTests(MediaApiFixture):
    def test_audio_streaming_ranges_allow_playback_and_seeking(self):
        original, photo = self.native_audio()
        route = "/api/media/" + photo["id"]
        with patch("app.ffmpeg_binary", return_value=None):
            status, full, headers = self.media_request(route)
            self.assertEqual(status, 200)
            self.assertEqual(full, original)
            self.assertTrue(headers["Content-Type"].startswith("audio/"))
            self.assertEqual(headers["Accept-Ranges"], "bytes")
            self.assertEqual(int(headers["Content-Length"]), len(original))
            for header, start, end in (("bytes=0-31", 0, 31),
                                       ("bytes=12-", 12, len(original) - 1),
                                       ("bytes=-16", len(original) - 16, len(original) - 1)):
                with self.subTest(range=header):
                    status, piece, headers = self.media_request(route, {"Range": header})
                    self.assertEqual(status, 206)
                    self.assertEqual(piece, original[start:end + 1])
                    self.assertEqual(headers["Content-Range"], "bytes %d-%d/%d" % (start, end, len(original)))
                    self.assertEqual(int(headers["Content-Length"]), len(piece))
            for header in ("bytes=%d-" % len(original), "bytes=20-10", "bytes=oops", "bytes=0-1,4-5"):
                with self.subTest(invalid_range=header):
                    status, _, headers = self.media_request(route, {"Range": header})
                    self.assertEqual(status, 416)
                    self.assertEqual(headers["Content-Range"], "bytes */%d" % len(original))

    def test_media_route_rejects_changed_files_and_paths_outside_scan(self):
        original, photo = self.native_audio()
        route = "/api/media/" + photo["id"]
        with patch("app.ffmpeg_binary", return_value=None):
            for invalid in ("/api/media/../../scanner.py", "/api/media/%2e%2e%2fscanner.py", "/api/media/unknown"):
                status, _, _ = self.media_request(invalid)
                self.assertGreaterEqual(status, 400)
                self.assertLess(status, 500)
            record = self.server.app.result.photos[photo["id"]]
            old_path = record.path
            record.path = "../outside.wav"
            status, _, _ = self.media_request(route)
            self.assertEqual(status, 409)
            record.path = old_path
            (self.root / "sound.wav").write_bytes(original + b"Changed after explicit scan")
            status, _, _ = self.media_request(route)
            self.assertEqual(status, 409)

    def test_unknown_file_pair_can_be_selected_removed_and_restored_without_autoscan(self):
        original = b"Unknown file type still supports exact duplicate review."
        (self.root / "first.custom").write_bytes(original)
        (self.root / "second").write_bytes(original)
        state = self.scan()
        self.assertEqual(state["stats"]["total"], 2)
        group = state["groups"][0]
        names = {photo["name"]: photo["id"] for photo in state["photos"]}
        self.assertTrue(all(photo["mediaType"] == "other" and photo["previewMode"] == "fallback"
                            for photo in state["photos"]))
        status, removed = self.request("POST", "/api/delete",
                                       self.delete_payload(group, names, "first.custom", "second"))
        self.assertEqual(status, 200, removed)
        self.assertFalse((self.root / "first.custom").exists())
        self.assertEqual((self.root / "second").read_bytes(), original)
        with patch("app.scan_folder") as scan:
            status, restored = self.request("POST", "/api/restore", {"id": removed["trashId"]})
            scan.assert_not_called()
        self.assertEqual(status, 200, restored)
        self.assertFalse(restored["state"]["scanning"])
        self.assertEqual((self.root / "first.custom").read_bytes(), original)
        self.assertEqual(len(restored["state"]["groups"]), 1)


@unittest.skipUnless(FFMPEG, "Optional FFmpeg decoder is unavailable")
class ConvertedMediaTests(MediaApiFixture):
    def setUp(self):
        super().setUp()
        self.server.app.media_directory = tempfile.TemporaryDirectory(prefix="preview-cache-", dir=self.root)

    def assert_decoded_codec(self, media, extension, codec):
        output = self.root / ("served-preview" + extension)
        output.write_bytes(media)
        decoded = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(output), "-f", "null", "-"],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(decoded.returncode, 0, decoded.stderr[-2000:])
        self.assertIn(codec, decoded.stderr)

    def test_old_video_format_produces_playable_h264_mp4_preview(self):
        generated = subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                                    "testsrc=size=128x96:rate=10", "-t", "0.5", "-c:v", "mpeg4", "-an",
                                    str(self.root / "old-format.avi")], capture_output=True, text=True, timeout=30)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        state = self.scan()
        photo = next(photo for photo in state["photos"] if photo["name"] == "old-format.avi")
        self.assertEqual(photo["mediaType"], "video")
        self.assertEqual(photo["previewMode"], "video")
        status, preview, headers = self.media_request("/api/media/" + photo["id"])
        self.assertEqual(status, 200, preview[:300])
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(preview[4:8], b"ftyp")
        self.assert_decoded_codec(preview, ".mp4", "Video: h264")

    def test_audio_produces_playable_mp3_preview(self):
        write_wav(self.root / "sound.wav")
        state = self.scan()
        photo = next(photo for photo in state["photos"] if photo["name"] == "sound.wav")
        status, preview, headers = self.media_request("/api/media/" + photo["id"])
        self.assertEqual(status, 200, preview[:300])
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assert_decoded_codec(preview, ".mp3", "Audio: mp3")


if __name__ == "__main__":
    unittest.main()
