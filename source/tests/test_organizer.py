"""Generated-file regressions for explicit organization and durable recovery."""
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from organizer import (JOURNAL_DIRECTORY, TYPE_DIRECTORY, YEAR_DIRECTORY,
                       OrganizeError, Organizer, _move_without_replace, photo_id)
from scanner import Photo, discover, file_signature, hash_file


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="media-review-organize-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.organizer = Organizer(self.root)

    def photo(self, relative, content=b"generated content", image=False):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return Photo(photo_id(path), path, path.name, str(path.relative_to(self.root)),
                     len(content), file_signature(path), sha256=hash_file(path),
                     mediaType="image" if image else "other", ocrText="PRIVATE OCR WORDS")

    def run_plan(self, photos, mode="type", basis="taken"):
        plan = self.organizer.create_plan(photos, mode, basis)
        operation_id = uuid.uuid4().hex
        result = self.organizer.execute(plan, operation_id)
        return plan, result

    def test_preview_reads_all_unique_files_and_creates_nothing(self):
        photos = [self.photo("a.JPG", b"photo"), self.photo("b.mp4", b"video"), self.photo("plain", b"file")]
        plan = self.organizer.create_plan(photos, "type")
        self.assertEqual({group["name"] for group in plan["groups"]}, {"JPG", "MP4", "No extension"})
        self.assertEqual(plan["moveCount"], 3)
        self.assertEqual(plan["totalSize"], 14)
        self.assertFalse((self.root / JOURNAL_DIRECTORY).exists())
        self.assertFalse((self.root / TYPE_DIRECTORY).exists())
        self.assertTrue(all(photo.file.is_file() for photo in photos))

    def test_move_and_restart_undo_preserve_contents_and_modified_time(self):
        photo = self.photo("originals/report.txt", b"never lose this content")
        modified = datetime(2011, 5, 2, 15, 0).timestamp()
        os.utime(photo.file, (modified, modified))
        photo.signature = file_signature(photo.file)
        before = photo.file.stat().st_mtime_ns
        plan, result = self.run_plan([photo])
        destination = self.root / plan["files"][0]["destination"]
        self.assertTrue(result["ok"])
        self.assertFalse(photo.file.exists())
        self.assertEqual(destination.read_bytes(), b"never lose this content")
        self.assertEqual(destination.stat().st_mtime_ns, before)
        reopened = Organizer(self.root)
        self.assertTrue(reopened.history()[0]["undoAvailable"])
        undo = reopened.undo(result["operationId"])
        self.assertTrue(undo["ok"])
        self.assertEqual(photo.file.read_bytes(), b"never lose this content")
        self.assertEqual(photo.file.stat().st_mtime_ns, before)
        self.assertFalse(destination.exists())
        self.assertFalse(Organizer(self.root).history()[0]["undoAvailable"])
        journal_text = "".join(path.read_text(encoding="utf-8") for path in (self.root / JOURNAL_DIRECTORY).iterdir())
        self.assertNotIn("PRIVATE OCR WORDS", journal_text)
        self.assertNotIn("visualEmbedding", journal_text)

    def test_collisions_are_deterministic_and_existing_files_survive(self):
        first = self.photo("one/a.txt", b"first")
        second = self.photo("two/a.txt", b"second")
        existing = self.photo(f"{TYPE_DIRECTORY}/TXT/a.txt", b"unrelated")
        plan = self.organizer.create_plan([second, existing, first], "type")
        self.assertEqual(plan["alreadyOrganizedCount"], 1)
        self.assertEqual(plan["renamedCount"], 2)
        self.assertEqual([Path(item["destination"]).name for item in plan["files"]], ["a (2).txt", "a (3).txt"])
        result = self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertTrue(result["ok"])
        self.assertEqual(existing.file.read_bytes(), b"unrelated")
        self.assertEqual((existing.file.parent / "a (2).txt").read_bytes(), b"first")
        self.assertEqual((existing.file.parent / "a (3).txt").read_bytes(), b"second")

    def test_changed_later_file_rejects_entire_preflight(self):
        photos = [self.photo("a.txt", b"aaaa"), self.photo("b.txt", b"bbbb")]
        plan = self.organizer.create_plan(photos, "type")
        before = photos[1].file.stat()
        photos[1].file.write_bytes(b"cccc")
        os.utime(photos[1].file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(OrganizeError):
            self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertTrue(all(photo.file.exists() for photo in photos))
        self.assertFalse((self.root / TYPE_DIRECTORY).exists())

    def test_destination_created_after_preview_rejects_all_moves(self):
        photos = [self.photo("a.txt"), self.photo("b.txt")]
        plan = self.organizer.create_plan(photos, "type")
        target = self.root / plan["files"][-1]["destination"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unrelated race")
        with self.assertRaises(OrganizeError):
            self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertTrue(all(photo.file.exists() for photo in photos))
        self.assertEqual(target.read_bytes(), b"unrelated race")

    def test_no_replace_atomic_move_handles_a_destination_race(self):
        photo = self.photo("a.txt", b"keep source")
        plan = self.organizer.create_plan([photo], "type")
        def race(source, destination):
            destination.write_bytes(b"racing unrelated file")
            raise FileExistsError("Destination won the race")
        with patch("organizer._move_without_replace", side_effect=race):
            result = self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertFalse(result["ok"])
        self.assertEqual(result["movedCount"], 0)
        self.assertEqual(photo.file.read_bytes(), b"keep source")
        self.assertEqual((self.root / plan["files"][0]["destination"]).read_bytes(), b"racing unrelated file")

    def test_actual_move_primitive_never_clobbers_an_existing_file(self):
        photo = self.photo("source.txt", b"source bytes")
        unrelated = self.photo("destination.txt", b"unrelated bytes")
        with self.assertRaises(FileExistsError):
            _move_without_replace(photo.file, unrelated.file)
        self.assertEqual(photo.file.read_bytes(), b"source bytes")
        self.assertEqual(unrelated.file.read_bytes(), b"unrelated bytes")

    def test_undo_occupied_original_preflight_preserves_whole_operation(self):
        photos = [self.photo("a.txt", b"original a"), self.photo("b.txt", b"original b")]
        plan, result = self.run_plan(photos)
        photos[1].file.write_bytes(b"new unrelated b")
        with self.assertRaises(OrganizeError):
            self.organizer.undo(result["operationId"])
        self.assertFalse(photos[0].file.exists())
        self.assertEqual(photos[1].file.read_bytes(), b"new unrelated b")
        self.assertTrue(all((self.root / item["destination"]).is_file() for item in plan["files"]))

    def test_undo_changed_file_preflight_preserves_other_files(self):
        photos = [self.photo("a.txt"), self.photo("b.txt")]
        plan, result = self.run_plan(photos)
        (self.root / plan["files"][-1]["destination"]).write_bytes(b"changed")
        with self.assertRaises(OrganizeError):
            self.organizer.undo(result["operationId"])
        self.assertTrue(all(not photo.file.exists() for photo in photos))
        self.assertTrue(all((self.root / item["destination"]).exists() for item in plan["files"]))

    def test_valid_exif_taken_year_and_invalid_date_modified_fallback(self):
        photos = []
        for name, date in [("taken.jpg", "2017:04:30 12:34:56"), ("bad.jpg", "2017:02:30 12:34:56")]:
            path = self.root / name
            exif = Image.Exif()
            exif[36867] = date
            Image.new("RGB", (8, 8), "red").save(path, exif=exif)
            modified = datetime(2022, 1, 1, 12, 0).timestamp()
            os.utime(path, (modified, modified))
            photos.append(Photo(photo_id(path), path, path.name, name, path.stat().st_size,
                                file_signature(path), sha256=hash_file(path), mediaType="image"))
        plan = self.organizer.create_plan(photos, "year", "taken")
        by_name = {item["name"]: item for item in plan["files"]}
        self.assertEqual((by_name["taken.jpg"]["group"], by_name["taken.jpg"]["dateSource"]), ("2017", "taken"))
        self.assertEqual((by_name["bad.jpg"]["group"], by_name["bad.jpg"]["dateSource"]), ("2022", "modified"))
        modified_plan = self.organizer.create_plan(photos, "year", "modified")
        self.assertEqual({item["group"] for item in modified_plan["files"]}, {"2022"})
        self.assertFalse((self.root / YEAR_DIRECTORY).exists())

    def test_crash_after_move_before_completed_event_is_reconciled(self):
        photo = self.photo("a.txt", b"durably recoverable")
        plan = self.organizer.create_plan([photo], "type")
        original_status = self.organizer._record_status
        def crash_completed(record, status, **updates):
            if status == "moved":
                raise OSError("simulated crash after rename")
            return original_status(record, status, **updates)
        with patch.object(self.organizer, "_record_status", side_effect=crash_completed):
            result = self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertEqual(result["movedCount"], 1)
        reopened = Organizer(self.root)
        self.assertTrue(reopened.history()[0]["undoAvailable"])
        reopened.undo(result["operationId"])
        self.assertEqual(photo.file.read_bytes(), b"durably recoverable")

    def test_nested_phone_exif_original_date_takes_priority(self):
        path = self.root / "phone.jpg"
        exif = Image.Exif()
        exif[306] = "2025:10:09 15:00:00"  # Edit time must not replace date taken.
        exif[34665] = {36867: "2015:03:21 08:09:10"}
        Image.new("RGB", (8, 8), "blue").save(path, exif=exif)
        photo = Photo(photo_id(path), path, path.name, path.name, path.stat().st_size,
                      file_signature(path), sha256=hash_file(path), mediaType="image")
        plan = self.organizer.create_plan([photo], "year", "taken")
        self.assertEqual(plan["files"][0]["group"], "2015")
        self.assertEqual(plan["files"][0]["dateSource"], "taken")

    def test_crash_after_undo_rename_reconciles_as_restored(self):
        photo = self.photo("a.txt", b"recover restored name")
        _, result = self.run_plan([photo])
        original_status = self.organizer._record_status
        def crash_completed(record, status, **updates):
            if status == "restored":
                raise OSError("simulated crash after undo rename")
            return original_status(record, status, **updates)
        with patch.object(self.organizer, "_record_status", side_effect=crash_completed):
            undo = self.organizer.undo(result["operationId"])
        self.assertEqual(undo["movedCount"], 1)
        reopened = Organizer(self.root)
        self.assertFalse(reopened.history()[0]["undoAvailable"])
        self.assertEqual(photo.file.read_bytes(), b"recover restored name")

    def test_incomplete_last_event_is_ignored_and_compacted_before_undo(self):
        photo = self.photo("a.txt")
        _, result = self.run_plan([photo])
        event_file = self.organizer.directory / ("events-" + self.organizer.generation + ".jsonl")
        with event_file.open("ab") as output:
            output.write(b'{"operationId":')
        reopened = Organizer(self.root)
        self.assertNotEqual(reopened.generation, self.organizer.generation)
        self.assertTrue(reopened.undo(result["operationId"])["ok"])
        self.assertFalse(Organizer(self.root).history()[0]["undoAvailable"])

    def test_forged_journal_outside_root_is_rejected_without_changes(self):
        photo = self.photo("a.txt")
        _, result = self.run_plan([photo])
        journal = json.loads(self.organizer.journal.read_text(encoding="utf-8"))
        journal["operations"][0]["files"][0]["source"] = "../outside.txt"
        self.organizer.journal.write_text(json.dumps(journal), encoding="utf-8")
        with self.assertRaises(OrganizeError):
            Organizer(self.root)
        self.assertFalse(photo.file.exists())
        self.assertTrue((self.root / TYPE_DIRECTORY / "TXT" / "a.txt").is_file())

    def test_support_directory_is_excluded_and_cannot_be_planned(self):
        photo = self.photo(JOURNAL_DIRECTORY + "/private.txt")
        files, _ = discover(self.root, True, include_all_files=True)
        self.assertNotIn(photo.file, files)
        with self.assertRaises(OrganizeError):
            self.organizer.create_plan([photo], "type")

    def test_linked_destination_is_rejected_without_following_link(self):
        photo = self.photo("a.txt")
        with tempfile.TemporaryDirectory(prefix="media-review-organize-link-") as outside:
            link = self.root / TYPE_DIRECTORY
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symbolic links requires Windows developer mode or permission.")
            try:
                with self.assertRaises(OrganizeError):
                    self.organizer.create_plan([photo], "type")
                self.assertTrue(photo.file.is_file())
                self.assertEqual(list(Path(outside).iterdir()), [])
            finally:
                link.unlink()

    def test_events_avoid_rewriting_the_full_journal_per_file(self):
        photos = [self.photo(f"{number}.txt", str(number).encode()) for number in range(12)]
        plan = self.organizer.create_plan(photos, "type")
        with patch.object(self.organizer, "_save", wraps=self.organizer._save) as save:
            result = self.organizer.execute(plan, uuid.uuid4().hex)
        self.assertTrue(result["ok"])
        self.assertEqual(save.call_count, 1)
        event_file = self.organizer.directory / ("events-" + self.organizer.generation + ".jsonl")
        self.assertEqual(len(event_file.read_text(encoding="utf-8").splitlines()), 25)


if __name__ == "__main__":
    unittest.main()
