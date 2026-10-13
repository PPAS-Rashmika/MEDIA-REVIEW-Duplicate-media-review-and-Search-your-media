"""Behavior tests for photo detection and explicitly selected, recoverable removal.

All photos in this suite are generated in TemporaryDirectory fixtures. Running
these tests never scans, deletes, or restores the user's actual photo collection.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "vendor"))

from PIL import Image, ImageDraw, PngImagePlugin

import scanner
import storage


def sample_image(size=(320, 240), variant=0):
    """A structured, asymmetric image with enough detail to compare resizes."""
    width, height = size
    picture = Image.new("RGB", size)
    pixels = picture.load()
    for y in range(height):
        for x in range(width):
            if variant == 0:
                pixels[x, y] = ((x * 201 // width + y * 47 // height) % 256,
                                (y * 193 // height + x * 23 // width) % 256,
                                (x * 59 // width + y * 89 // height) % 256)
            else:
                pixels[x, y] = ((255 - y * 197 // height) % 256,
                                (x * 189 // width) % 256,
                                (y * 233 // height - x * 73 // width) % 256)
    draw = ImageDraw.Draw(picture)
    if variant == 0:
        draw.rectangle((width * .08, height * .12, width * .38, height * .52), fill=(17, 42, 190))
        draw.ellipse((width * .48, height * .30, width * .90, height * .85), fill=(245, 181, 35))
        draw.line((width * .06, height * .88, width * .72, height * .16), fill=(252, 242, 229), width=7)
    else:
        draw.polygon(((width * .05, height * .05), (width * .95, height * .24),
                      (width * .32, height * .94)), fill=(208, 21, 83))
        draw.rectangle((width * .63, height * .59, width * .94, height * .90), fill=(30, 208, 62))
    return picture


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photo-review-test-", dir=APP_ROOT / "tests")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.root.resolve().relative_to((APP_ROOT / "tests").resolve())
        self.temporary.cleanup()

    def write_exact_pair(self, first="first.png", second="second.png"):
        sample_image().save(self.root / first)
        shutil.copyfile(self.root / first, self.root / second)
        return self.root / first, self.root / second


class ScannerTests(FixtureCase):
    def group_names(self, result, group):
        return {result.photos[photo_id].name for photo_id in group["photos"]}

    def test_identical_bytes_are_an_exact_group(self):
        first, second = self.write_exact_pair()
        result = scanner.scan_folder(self.root)
        exact = [group for group in result.groups if group["kind"] == "exact"]
        self.assertEqual(len(exact), 1)
        self.assertEqual(self.group_names(result, exact[0]), {first.name, second.name})
        self.assertEqual(result.stats["duplicateBytes"], first.stat().st_size)
        self.assertEqual(result.errors, [])

    def test_same_decoded_pixels_in_different_formats_are_grouped(self):
        picture = sample_image()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "Metadata differs; visible pixels are identical.")
        picture.save(self.root / "original.png")
        picture.save(self.root / "metadata.png", pnginfo=metadata)
        picture.save(self.root / "different-format.bmp")
        result = scanner.scan_folder(self.root)
        matching = [group for group in result.groups if group["kind"] == "pixels"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(self.group_names(result, matching[0]),
                         {"original.png", "metadata.png", "different-format.bmp"})
        self.assertFalse(any(group["kind"] == "exact" for group in result.groups))

    def test_resized_recompressed_copy_is_suggested_and_unrelated_is_separate(self):
        picture = sample_image()
        picture.save(self.root / "large.jpg", quality=96)
        picture.resize((160, 120), Image.Resampling.LANCZOS).save(self.root / "small.jpg", quality=75)
        sample_image(variant=1).save(self.root / "unrelated.png")
        result = scanner.scan_folder(self.root, threshold=94)
        similar = [group for group in result.groups if group["kind"] == "similar"]
        self.assertEqual(len(similar), 1)
        self.assertEqual(self.group_names(result, similar[0]), {"large.jpg", "small.jpg"})
        self.assertGreaterEqual(similar[0]["score"], 94)
        self.assertFalse(any("unrelated.png" in self.group_names(result, group) for group in result.groups))
        self.assertEqual(result.stats["duplicateBytes"], 0,
                         "Similarity suggestions must not claim proven duplicate bytes")

    def test_exif_orientation_is_applied_before_pixel_comparison(self):
        picture = sample_image()
        picture.save(self.root / "upright.png")
        raw = picture.transpose(Image.Transpose.ROTATE_90)
        orientation = Image.Exif()
        orientation[274] = 6
        raw.save(self.root / "oriented.png", exif=orientation)
        result = scanner.scan_folder(self.root)
        self.assertEqual([(photo.width, photo.height) for photo in result.photos.values()], [(320, 240), (320, 240)])
        matching = [group for group in result.groups if group["kind"] == "pixels"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(self.group_names(result, matching[0]), {"upright.png", "oriented.png"})

    def test_corrupt_image_reports_issue_without_interrupting_valid_photos(self):
        self.write_exact_pair()
        (self.root / "broken.jpg").write_bytes(b"This is not a JPEG.")
        result = scanner.scan_folder(self.root)
        self.assertEqual(len(result.photos), 3)
        broken = next(photo for photo in result.photos.values() if photo.name == "broken.jpg")
        self.assertFalse(broken.previewAvailable)
        self.assertTrue(broken.issue)
        self.assertTrue(any(error["name"] == "broken.jpg" for error in result.errors))
        self.assertEqual(sum(group["kind"] == "exact" for group in result.groups), 1)

    def test_recursive_choice_and_recovery_folder_exclusion(self):
        sample_image().save(self.root / "root.png")
        nested = self.root / "album"
        nested.mkdir()
        shutil.copyfile(self.root / "root.png", nested / "copy.png")
        trash = self.root / scanner.TRASH_NAME
        trash.mkdir()
        shutil.copyfile(self.root / "root.png", trash / "removed.png")
        flat = scanner.scan_folder(self.root, recursive=False)
        recursive = scanner.scan_folder(self.root, recursive=True)
        self.assertEqual({photo.path for photo in flat.photos.values()}, {"root.png"})
        self.assertEqual({photo.name for photo in recursive.photos.values()}, {"root.png", "copy.png"})
        self.assertEqual(len(recursive.groups), 1)

    def test_threshold_validation_and_explicit_exclusion(self):
        self.write_exact_pair()
        excluded = scanner.scan_folder(self.root, exclude_paths=(self.root / "second.png",))
        self.assertEqual(len(excluded.photos), 1)
        self.assertEqual(excluded.groups, [])
        for value in (84, 100, float("inf"), float("nan")):
            with self.subTest(threshold=value):
                with self.assertRaises(ValueError):
                    scanner.scan_folder(self.root, threshold=value)

    def test_linked_files_are_not_scanned(self):
        external = self.root / "external"
        external.mkdir()
        sample_image().save(external / "photo.png")
        selected = self.root / "selected"
        selected.mkdir()
        try:
            os.symlink(external / "photo.png", selected / "linked.png")
        except OSError as error:
            self.skipTest("Creating symlinks is unavailable on this host: %s" % error)
        result = scanner.scan_folder(selected)
        self.assertEqual(result.photos, {})

    @unittest.skipUnless(scanner.HEIF_AVAILABLE, "Optional HEIC decoder not installed")
    def test_heic_decoder_allows_preview_and_exact_comparison(self):
        import pillow_heif
        try:
            pillow_heif.from_pillow(sample_image()).save(self.root / "first.heic")
        except Exception as error:
            self.skipTest("HEIC encoder unavailable on this host: %s" % error)
        shutil.copyfile(self.root / "first.heic", self.root / "second.heic")
        result = scanner.scan_folder(self.root)
        self.assertEqual(len(result.photos), 2)
        self.assertTrue(all(photo.previewAvailable for photo in result.photos.values()))
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["kind"], "exact")


class StorageTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.first, self.second = self.write_exact_pair()
        self.original_bytes = self.first.read_bytes()
        result = scanner.scan_folder(self.root)
        self.photos = {photo.name: photo for photo in result.photos.values()}
        self.store = storage.TrashStore(self.root)

    def move_first(self):
        return self.store.move(self.photos["first.png"], self.photos["second.png"])

    def test_recoverable_move_and_restore_preserve_exact_original_bytes(self):
        removed = self.move_first()
        self.assertFalse(self.first.exists())
        self.assertEqual(self.second.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.preview_path(removed["id"]).read_bytes(), self.original_bytes)
        self.assertEqual(len(self.store.items()), 1)
        restored = self.store.restore(removed["id"])
        self.assertEqual(Path(restored["path"]), self.first)
        self.assertEqual(self.first.read_bytes(), self.original_bytes)
        self.assertFalse(restored["renamed"])
        self.assertEqual(self.store.items(), [])

    def test_restore_name_conflict_preserves_existing_file(self):
        removed = self.move_first()
        replacement = b"A new file at the old name must survive."
        self.first.write_bytes(replacement)
        restored = self.store.restore(removed["id"])
        self.assertTrue(restored["renamed"])
        self.assertEqual(self.first.read_bytes(), replacement)
        self.assertEqual(Path(restored["path"]).read_bytes(), self.original_bytes)
        self.assertEqual(Path(restored["path"]).parent, self.root)

    def test_recovery_journal_survives_new_store_instance(self):
        removed = self.move_first()
        restarted = storage.TrashStore(self.root)
        self.assertEqual([item["id"] for item in restarted.items()], [removed["id"]])
        restarted.restore(removed["id"])
        self.assertEqual(self.first.read_bytes(), self.original_bytes)

    def test_same_photo_cannot_be_both_removed_and_kept(self):
        with self.assertRaises(storage.StorageError):
            self.store.move(self.photos["first.png"], self.photos["first.png"])
        self.assertEqual(self.first.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.items(), [])

    def test_changed_target_with_same_size_and_timestamp_is_rejected(self):
        info = self.first.stat()
        changed = bytearray(self.original_bytes)
        changed[len(changed) // 2] ^= 1
        self.first.write_bytes(changed)
        os.utime(self.first, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(storage.StorageError):
            self.move_first()
        self.assertTrue(self.first.exists())
        self.assertEqual(self.second.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.items(), [])

    def test_changed_or_missing_keeper_prevents_removal(self):
        self.second.unlink()
        with self.assertRaises(storage.StorageError):
            self.move_first()
        self.assertEqual(self.first.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.items(), [])

    def test_modified_recovery_file_cannot_be_restored(self):
        removed = self.move_first()
        recovered = self.store.preview_path(removed["id"])
        recovered.write_bytes(b"Modified recovery file")
        with self.assertRaises(storage.StorageError):
            self.store.restore(removed["id"])
        self.assertFalse(self.first.exists())
        self.assertTrue(recovered.exists())

    def test_journal_write_failure_before_move_leaves_photos_intact(self):
        with patch.object(self.store, "_save", side_effect=OSError("Simulated full disk")):
            with self.assertRaises(OSError):
                self.move_first()
        self.assertEqual(self.first.read_bytes(), self.original_bytes)
        self.assertEqual(self.second.read_bytes(), self.original_bytes)
        self.assertEqual(self.store.records, [])

    def test_pending_move_is_reconciled_if_final_journal_write_failed(self):
        real_save = self.store._save
        count = 0

        def fail_second_write():
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Simulated crash after file move")
            real_save()

        with patch.object(self.store, "_save", side_effect=fail_second_write):
            removed = self.move_first()
        self.assertFalse(self.first.exists())
        persisted = json.loads(self.store.journal.read_text(encoding="utf-8"))
        self.assertEqual(persisted["items"][0]["status"], "pending")
        restarted = storage.TrashStore(self.root)
        self.assertEqual(restarted.records[0]["status"], "trashed")
        restarted.restore(removed["id"])
        self.assertEqual(self.first.read_bytes(), self.original_bytes)

    def test_restore_is_reconciled_if_final_journal_write_failed(self):
        removed = self.move_first()
        real_save = self.store._save
        count = 0

        def fail_second_write():
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("Simulated crash after restore")
            real_save()

        with patch.object(self.store, "_save", side_effect=fail_second_write):
            self.store.restore(removed["id"])
        restarted = storage.TrashStore(self.root)
        self.assertEqual(restarted.items(), [])
        self.assertEqual(restarted.records[0]["status"], "restored")
        self.assertEqual(self.first.read_bytes(), self.original_bytes)

    def test_recovery_journal_rejects_path_traversal(self):
        removed = self.move_first()
        journal = json.loads(self.store.journal.read_text(encoding="utf-8"))
        journal["items"][0]["relativePath"] = "../outside.png"
        self.store.journal.write_text(json.dumps(journal), encoding="utf-8")
        with self.assertRaises(storage.StorageError):
            storage.TrashStore(self.root)
        self.assertFalse(self.first.exists())
        self.assertEqual(self.second.read_bytes(), self.original_bytes)


class StartupTests(FixtureCase):
    def test_constructor_keeps_folder_unselected_and_performs_no_scan_or_trash_io(self):
        import app
        self.write_exact_pair()
        with patch("app.scan_folder") as scan, patch("app.TrashStore") as trash_store:
            review = app.ReviewApp(self.root)
            state = review.state()
        self.assertEqual(state["root"], "")
        self.assertFalse(state["hasScanned"])
        self.assertFalse(state["scanning"])
        self.assertEqual(state["photos"], [])
        self.assertEqual(state["groups"], [])
        self.assertEqual(state["trashCount"], 0)
        self.assertIsNone(review.scan_thread)
        scan.assert_not_called()
        trash_store.assert_not_called()

    def test_launch_opens_the_interface_without_starting_a_scan(self):
        import app
        from unittest.mock import Mock
        fake_server = Mock()
        fake_server.server_address = ("127.0.0.1", 8765)
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch("app.create_server", return_value=fake_server), patch("builtins.print"):
            app.main(["--root", str(self.root), "--no-browser"])
        fake_server.app.start_scan.assert_not_called()
        fake_server.serve_forever.assert_called_once()
        fake_server.server_close.assert_called_once()


class ApiFixture(FixtureCase):
    def setUp(self):
        super().setUp()
        import app
        self.server = app.create_server(self.root, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        self.token = self.request("GET", "/api/state")[1]["token"]

    def tearDown(self):
        scan_thread = self.server.app.scan_thread
        if scan_thread is not None:
            scan_thread.join(timeout=5)
        self.server.shutdown()
        self.server.server_close()
        self.server.app.close()
        self.thread.join(timeout=3)
        super().tearDown()

    def request(self, method, path, body=None, headers=None, authorized=True):
        final_headers = dict(headers or {})
        if body is not None:
            body = json.dumps(body).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")
        if method == "POST" and authorized:
            final_headers.setdefault("Origin", self.origin)
            final_headers.setdefault("X-Review-Token", self.token)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=final_headers)
            response = connection.getresponse()
            payload = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/json" in content_type:
                payload = json.loads(payload)
            return response.status, payload
        finally:
            connection.close()

    def scan(self):
        status, body = self.request("POST", "/api/scan", {"path": str(self.root), "recursive": True, "threshold": 94})
        self.assertLess(status, 300, body)
        return self.wait_for_scan()

    def wait_for_scan(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, state = self.request("GET", "/api/state")
            self.assertEqual(status, 200, state)
            if not state.get("scanning"):
                return state
            time.sleep(.02)
        self.fail("Fixture scan did not finish within ten seconds")

    def scan_pair(self):
        self.write_exact_pair()
        state = self.scan()
        self.assertEqual(state["errors"], [])
        group = next(group for group in state["groups"] if group["kind"] == "exact")
        names = {photo["name"]: photo["id"] for photo in state["photos"]}
        return group, names

    def delete_payload(self, group, names, selected="first.png", kept="second.png"):
        return {"id": names[selected], "groupId": group["id"], "keepId": names[kept]}


class ApiTests(ApiFixture):
    def test_choosing_folder_does_not_scan_until_explicit_scan_request(self):
        self.write_exact_pair()
        before = self.request("GET", "/api/state")[1]
        self.assertEqual(before["root"], "")
        self.assertFalse(before["hasScanned"])
        with patch("tkinter.Tk"), patch("tkinter.filedialog.askdirectory", return_value=str(self.root)), \
                patch("app.scan_folder") as scan, patch("app.TrashStore") as trash_store:
            status, picked = self.request("POST", "/api/pick-folder", {})
            self.assertEqual(status, 200, picked)
            self.assertEqual(picked["path"], str(self.root))
            self.assertFalse(picked["cancelled"])
            after = self.request("GET", "/api/state")[1]
            self.assertEqual(after["photos"], [])
            self.assertEqual(after["groups"], [])
            self.assertFalse(after["scanning"])
            self.assertFalse(after["hasScanned"])
            scan.assert_not_called()
            trash_store.assert_not_called()
        state = self.scan()
        self.assertTrue(state["hasScanned"])
        self.assertEqual(state["stats"]["total"], 2)
        self.assertEqual(len(state["groups"]), 1)

    def test_api_scan_and_preview_provide_both_comparable_images(self):
        group, names = self.scan_pair()
        self.assertEqual(set(group["photos"]), set(names.values()))
        for photo_id in names.values():
            status, picture = self.request("GET", "/api/photo/" + photo_id + "?size=preview")
            self.assertEqual(status, 200)
            self.assertTrue(picture.startswith(b"\xff\xd8"), "Preview must be a JPEG image")
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["stats"]["total"], 2)
        self.assertTrue(all(not Path(photo["path"]).is_absolute() for photo in state["photos"]))

    def test_explicit_selection_moves_only_chosen_photo_and_restore_recovers_it(self):
        group, names = self.scan_pair()
        original = (self.root / "first.png").read_bytes()
        status, removed = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 200, removed)
        self.assertFalse((self.root / "first.png").exists())
        self.assertEqual((self.root / "second.png").read_bytes(), original)
        self.assertEqual(removed["state"]["stats"]["total"], 1)
        self.assertEqual(removed["state"]["groups"], [])
        status, trash = self.request("GET", "/api/trash")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in trash["items"]], [removed["trashId"]])
        status, preview = self.request("GET", trash["items"][0]["previewUrl"])
        self.assertEqual(status, 200)
        self.assertTrue(preview.startswith(b"\xff\xd8"))
        with patch("app.scan_folder") as scan:
            status, restored = self.request("POST", "/api/restore", {"id": removed["trashId"]})
            scan.assert_not_called()
        self.assertEqual(status, 200, restored)
        self.assertFalse(restored["state"]["scanning"])
        self.assertEqual(restored["state"]["stats"]["total"], 2)
        self.assertEqual(len(restored["state"]["groups"]), 1)
        self.assertEqual(set(restored["state"]["groups"][0]["photos"]), set(names.values()))
        self.assertEqual((self.root / "first.png").read_bytes(), original)
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_restore_preserves_another_group_without_a_directory_scan(self):
        self.write_exact_pair()
        sample_image(variant=1).save(self.root / "unrelated-a.png")
        shutil.copyfile(self.root / "unrelated-a.png", self.root / "unrelated-b.png")
        state = self.scan()
        names = {photo["name"]: photo["id"] for photo in state["photos"]}
        group = next(item for item in state["groups"] if names["first.png"] in item["photos"])
        unrelated = next(item for item in state["groups"] if names["unrelated-a.png"] in item["photos"])
        status, removed = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 200, removed)
        with patch("app.scan_folder") as scan:
            status, restored = self.request("POST", "/api/restore", {"id": removed["trashId"]})
            scan.assert_not_called()
        self.assertEqual(status, 200, restored)
        self.assertFalse(restored["state"]["scanning"])
        self.assertIn(unrelated, restored["state"]["groups"])
        self.assertEqual(len(restored["state"]["groups"]), 2)

    def test_last_remaining_photo_cannot_be_removed_with_stale_comparison(self):
        group, names = self.scan_pair()
        status, removed = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 200, removed)
        status, rejected = self.request("POST", "/api/delete",
                                        self.delete_payload(group, names, "second.png", "first.png"))
        self.assertEqual(status, 409, rejected)
        self.assertTrue((self.root / "second.png").exists())
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 1)

    def test_missing_or_inconsistent_selection_is_rejected(self):
        group, names = self.scan_pair()
        valid = self.delete_payload(group, names)
        invalid = ({}, {"id": names["first.png"]}, dict(valid, groupId="unknown"),
                   dict(valid, keepId=names["first.png"]), dict(valid, keepId="unknown"),
                   dict(valid, id="unknown"))
        for payload in invalid:
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/delete", payload)
                self.assertEqual(status, 409, body)
        self.assertTrue((self.root / "first.png").exists())
        self.assertTrue((self.root / "second.png").exists())
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_unrelated_photo_cannot_be_substituted_as_keeper(self):
        self.write_exact_pair()
        sample_image(variant=1).save(self.root / "unrelated.png")
        state = self.scan()
        group = next(group for group in state["groups"] if group["kind"] == "exact")
        names = {photo["name"]: photo["id"] for photo in state["photos"]}
        status, body = self.request("POST", "/api/delete",
                                   self.delete_payload(group, names, kept="unrelated.png"))
        self.assertEqual(status, 409, body)
        self.assertEqual(len(list(self.root.glob("*.png"))), 3)

    def test_changed_photo_is_rejected_at_mutation_api(self):
        group, names = self.scan_pair()
        (self.root / "first.png").write_bytes(b"Changed since scan")
        status, body = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 409, body)
        self.assertEqual((self.root / "first.png").read_bytes(), b"Changed since scan")
        self.assertTrue((self.root / "second.png").exists())

    def test_missing_wrong_token_or_cross_origin_cannot_mutate(self):
        group, names = self.scan_pair()
        payload = self.delete_payload(group, names)
        invalid_headers = ({"Origin": self.origin},
                           {"Origin": self.origin, "X-Review-Token": "wrong"},
                           {"Origin": "https://example.invalid", "X-Review-Token": self.token},
                           {"X-Review-Token": self.token},
                           {"Origin": self.origin + "/", "X-Review-Token": self.token})
        for headers in invalid_headers:
            with self.subTest(headers=headers):
                status, body = self.request("POST", "/api/delete", payload, headers, authorized=False)
                self.assertEqual(status, 403, body)
        self.assertTrue((self.root / "first.png").exists())
        self.assertTrue((self.root / "second.png").exists())

    def test_foreign_host_is_rejected_even_for_reads(self):
        status, body = self.request("GET", "/api/state", headers={"Host": "attacker.invalid:%d" % self.port})
        self.assertEqual(status, 403, body)

    def test_static_and_photo_routes_reject_path_traversal(self):
        self.scan_pair()
        for path in ("/../scanner.py", "/%2e%2e/scanner.py", "/static/../../storage.py",
                     "/api/photo/../../scanner.py", "/api/photo/%2e%2e%2fscanner.py",
                     "/api/trash/../../storage.py/preview"):
            with self.subTest(path=path):
                status, body = self.request("GET", path)
                self.assertGreaterEqual(status, 400, body)
                self.assertLess(status, 500, body)

    def test_photo_record_cannot_escape_selected_root(self):
        group, names = self.scan_pair()
        photo = self.server.app.result.photos[names["first.png"]]
        photo.path = "../outside.png"
        status, body = self.request("GET", "/api/photo/" + photo.id)
        self.assertEqual(status, 409, body)
        status, body = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 409, body)
        self.assertTrue((self.root / "first.png").exists())

    def test_invalid_scan_parameters_do_not_replace_valid_review(self):
        self.scan_pair()
        for payload in ({"path": str(self.root / "missing")}, {"threshold": 100},
                        {"threshold": "oops"}, {"recursive": "true"}, {"includeAllFiles": "true"}):
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/scan", payload)
                self.assertEqual(status, 400, body)
        state = self.request("GET", "/api/state")[1]
        self.assertEqual(state["stats"]["total"], 2)
        self.assertEqual(len(state["groups"]), 1)

    def test_scan_and_mutations_are_serialized(self):
        group, names = self.scan_pair()
        entered, release = threading.Event(), threading.Event()
        real_scan = scanner.scan_folder

        def paused_scan(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test scan lock was not released")
            return real_scan(*args, **kwargs)

        with patch("app.scan_folder", side_effect=paused_scan):
            try:
                status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
                self.assertEqual(status, 200, body)
                self.assertTrue(entered.wait(timeout=3))
                for route, payload in (("/api/scan", {"path": str(self.root)}),
                                       ("/api/delete", self.delete_payload(group, names)),
                                       ("/api/restore", {"id": "unknown"})):
                    status, body = self.request("POST", route, payload)
                    self.assertEqual(status, 409, body)
                self.assertTrue((self.root / "first.png").exists())
                self.assertTrue((self.root / "second.png").exists())
            finally:
                release.set()
            self.wait_for_scan()

    def test_concurrent_opposite_deletions_always_leave_one_copy(self):
        group, names = self.scan_pair()
        barrier = threading.Barrier(3)
        responses = []

        def remove(selected, kept):
            barrier.wait(timeout=3)
            responses.append(self.request("POST", "/api/delete",
                                          self.delete_payload(group, names, selected, kept)))

        workers = [threading.Thread(target=remove, args=("first.png", "second.png")),
                   threading.Thread(target=remove, args=("second.png", "first.png"))]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=3)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(status for status, _ in responses), [200, 409], responses)
        self.assertEqual(sum((self.root / name).exists() for name in names), 1)
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
