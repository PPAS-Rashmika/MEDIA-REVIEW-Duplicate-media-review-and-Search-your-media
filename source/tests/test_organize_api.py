"""Organizer API tests touch only generated files in bounded temporary folders."""
from __future__ import annotations

import os
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import app
from tests.test_app import ApiFixture, sample_image


class OrganizeApiTests(ApiFixture):
    def setUp(self):
        super().setUp()
        # These tests exercise moves and the library index, without loading
        # image models or depending on an installed Windows OCR language.
        for name, value in (("scanner.classify_image", ([], [], "unavailable")),
                            ("scanner.extract_image_text", ("", "unavailable")),
                            ("scanner.visual_search.embed_image", None),
                            ("scanner.visual_search.capabilities", {"available": False})):
            replacement = patch(name, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def files(self, names=("one.txt", "nested/two.CUSTOM", "extensionless")):
        for index, name in enumerate(names):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.suffix.lower() == ".png":
                sample_image().save(destination)
            else:
                destination.write_bytes(b"Generated organizer fixture " + str(index).encode())
        state = self.scan()
        self.assertEqual(state["stats"]["total"], len(names), state)
        return state, {str(Path(name)): (self.root / name).read_bytes() for name in names}

    def plan(self, mode="type", year_basis="taken"):
        status, body = self.request("POST", "/api/organize/plan", {
            "mode": mode, "yearBasis": year_basis})
        self.assertEqual(status, 200, body)
        return body

    def execute(self, plan):
        return self.request("POST", "/api/organize/execute", {"planId": plan["planId"]})

    def undo(self, operation_id):
        return self.request("POST", "/api/organize/undo", {"operationId": operation_id})

    def finished(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, state = self.request("GET", "/api/state")
            self.assertEqual(status, 200, state)
            if not state["organization"]["running"]:
                result = state["organization"]["lastResult"]
                self.assertIsInstance(result, dict, state)
                return state, result
            time.sleep(.01)
        self.fail("Generated organizer job did not finish within ten seconds")

    def organize(self, plan):
        status, started = self.execute(plan)
        self.assertEqual(status, 200, started)
        state, result = self.finished()
        self.assertEqual(result["failures"], [], result)
        self.assertEqual(result["movedCount"], plan["moveCount"], result)
        return state, result

    def test_all_routes_require_scan_and_do_not_implicitly_scan_or_create_folders(self):
        before = set(self.root.iterdir())
        with patch("app.scan_folder") as scan:
            for route, payload in (("/api/organize/plan", {"mode": "type"}),
                                   ("/api/organize/execute", {"planId": "invalid"}),
                                   ("/api/organize/undo", {"operationId": "invalid"})):
                status, body = self.request("POST", route, payload)
                self.assertEqual(status, 409, (route, body))
            scan.assert_not_called()
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertIsNone(self.server.app.scan_thread)

    def test_all_routes_require_current_token_origin_and_host(self):
        self.files()
        for route, payload in (("/api/organize/plan", {"mode": "type"}),
                               ("/api/organize/execute", {"planId": "invalid"}),
                               ("/api/organize/undo", {"operationId": "invalid"})):
            for headers in ({}, {"Origin": self.origin, "X-Review-Token": "wrong"},
                            {"Origin": "https://example.invalid", "X-Review-Token": self.token},
                            {"Host": "example.invalid", "Origin": self.origin,
                             "X-Review-Token": self.token}):
                with self.subTest(route=route, headers=headers):
                    status, body = self.request("POST", route, payload, headers=headers, authorized=False)
                    self.assertEqual(status, 403, body)

    def test_plan_is_read_only_and_contains_unique_and_extensionless_files(self):
        state, original = self.files()
        before = {str(path.relative_to(self.root)) for path in self.root.rglob("*")}
        plan = self.plan()
        self.assertEqual(plan["mode"], "type")
        self.assertEqual(plan["destinationRoot"], "Organized by type")
        self.assertEqual(plan["moveCount"], 3)
        self.assertEqual(plan["totalSize"], sum(map(len, original.values())))
        self.assertEqual({item["id"] for item in plan["files"]}, {photo["id"] for photo in state["photos"]})
        self.assertEqual({item["path"] for item in plan["files"]}, set(original))
        self.assertEqual(sum(group["count"] for group in plan["groups"]), 3)
        self.assertEqual(len({item["destination"].casefold() for item in plan["files"]}), 3)
        for item in plan["files"]:
            relative = Path(item["destination"])
            self.assertFalse(relative.is_absolute())
            self.assertNotIn("..", relative.parts)
            self.assertEqual(relative.parts[0], plan["destinationRoot"])
        self.assertEqual({str(path.relative_to(self.root)) for path in self.root.rglob("*")}, before)
        after = self.request("GET", "/api/state")[1]
        for key in ("photos", "groups", "stats", "errors", "revision", "trashCount"):
            self.assertEqual(after[key], state[key], key)
        self.assertFalse(after["organization"]["running"])
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)

    def test_type_move_keeps_every_byte_and_remaps_search_paths_and_preview_ids(self):
        before, original = self.files(("image.png", "nested/song.mp3", "document.custom", "extensionless"))
        old_image_id = next(photo["id"] for photo in before["photos"] if photo["name"] == "image.png")
        plan = self.plan()
        after, result = self.organize(plan)
        self.assertEqual(after["stats"]["total"], len(original))
        self.assertGreater(after["revision"], before["revision"])
        self.assertFalse(after["scanning"])
        self.assertEqual(after["trashCount"], 0)
        self.assertFalse((self.root / ".photo-review-trash").exists())
        for item in plan["files"]:
            self.assertFalse((self.root / item["path"]).exists())
            self.assertEqual((self.root / item["destination"]).read_bytes(), original[item["path"]])
        self.assertEqual({photo["path"] for photo in after["photos"]}, {item["destination"] for item in plan["files"]})
        status, old_preview = self.request("GET", "/api/photo/" + old_image_id)
        self.assertEqual(status, 404, old_preview)
        new_image = next(photo for photo in after["photos"] if photo["name"] == "image.png")
        self.assertNotEqual(new_image["id"], old_image_id)
        self.assertEqual(self.request("GET", "/api/photo/" + new_image["id"])[0], 200)
        status, found = self.request("POST", "/api/search", {"query": "image.png", "mode": "filename"})
        self.assertEqual(status, 200, found)
        self.assertEqual([photo["path"] for photo in found["results"]], [new_image["path"]])
        status, repeated = self.execute(plan)
        self.assertEqual(status, 409, repeated)

    def test_modified_year_groups_use_selected_timestamps_and_move_unique_files(self):
        self.files()
        years = {str(Path(name)): year for name, year in
                 (("one.txt", 2019), ("nested/two.CUSTOM", 2022), ("extensionless", 2019))}
        for relative, year in years.items():
            stamp = datetime(year, 6, 15, 12).timestamp()
            os.utime(self.root / relative, (stamp, stamp))
        self.scan()
        plan = self.plan("year", "modified")
        self.assertEqual(plan["destinationRoot"], "Organized by year")
        for item in plan["files"]:
            self.assertEqual(item["group"], str(years[item["path"]]))
            self.assertEqual(item["dateSource"], "modified")
        self.assertEqual({group["name"]: group["count"] for group in plan["groups"]}, {"2019": 2, "2022": 1})
        self.organize(plan)

    def test_repeated_type_preview_has_no_moves_and_internal_journal_is_never_scanned(self):
        _, original = self.files()
        plan = self.plan()
        after, _ = self.organize(plan)
        repeated = self.plan()
        self.assertEqual(repeated["moveCount"], 0)
        self.assertEqual(repeated["alreadyOrganizedCount"], len(original))
        self.assertEqual(repeated["files"], [])
        self.assertEqual(self.execute(repeated)[0], 409)
        rescanned = self.scan()
        self.assertEqual(rescanned["stats"]["total"], len(original), rescanned)
        self.assertEqual({photo["path"] for photo in rescanned["photos"]},
                         {photo["path"] for photo in after["photos"]})
        self.assertTrue(rescanned["organization"]["history"])

    def test_expired_preview_cannot_move_files_and_unscanned_new_files_are_left_alone(self):
        _, original = self.files()
        expired = self.plan()
        self.server.app.organize_plans[expired["planId"]]["expires"] = 0
        self.assertEqual(self.execute(expired)[0], 409)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)
        late = self.root / "created-after-scan.txt"
        late.write_bytes(b"Generated after scan, outside the current library index")
        plan = self.plan()
        self.assertEqual(plan["moveCount"], len(original))
        self.assertNotIn(late.name, {item["path"] for item in plan["files"]})
        self.organize(plan)
        self.assertEqual(late.read_bytes(), b"Generated after scan, outside the current library index")

    def test_bad_modes_basis_and_execute_ids_are_rejected_without_moves(self):
        _, original = self.files()
        for payload in ({}, {"mode": "folder"}, {"mode": "../escape"}, {"mode": 1}, {"mode": []},
                        {"mode": "year", "yearBasis": "created"}, {"mode": "year", "yearBasis": []}):
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/organize/plan", payload)
                self.assertEqual(status, 400, body)
        for route, key in (("/api/organize/execute", "planId"), ("/api/organize/undo", "operationId")):
            for value in (None, "", "../escape", 1, []):
                status, body = self.request("POST", route, {key: value})
                self.assertIn(status, (400, 409), body)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)

    def test_rescan_and_review_removal_invalidate_previews(self):
        group, names = self.scan_pair()
        plan = self.plan()
        status, deleted = self.request("POST", "/api/delete", self.delete_payload(group, names))
        self.assertEqual(status, 200, deleted)
        self.assertEqual(self.execute(plan)[0], 409)
        self.assertTrue((self.root / "second.png").exists())
        fresh = self.plan()
        self.scan()
        self.assertEqual(self.execute(fresh)[0], 409)
        self.assertTrue((self.root / "second.png").exists())

    def test_changed_same_size_and_timestamp_source_stops_entire_preflight(self):
        _, original = self.files()
        plan = self.plan()
        path = self.root / "one.txt"
        info = path.stat()
        damaged = bytearray(original["one.txt"])
        damaged[-1] ^= 1
        path.write_bytes(damaged)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        status, started = self.execute(plan)
        self.assertEqual(status, 200, started)
        _, result = self.finished()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["movedCount"], 0, result)
        self.assertTrue(result["failures"], result)
        for item in plan["files"]:
            self.assertTrue((self.root / item["path"]).exists())
            self.assertFalse((self.root / item["destination"]).exists())

    def test_execution_never_overwrites_file_created_after_preview(self):
        _, original = self.files()
        plan = self.plan()
        first = plan["files"][0]
        collision = self.root / first["destination"]
        collision.parent.mkdir(parents=True, exist_ok=True)
        collision.write_bytes(b"A new unrelated file created after preview")
        status, started = self.execute(plan)
        self.assertEqual(status, 200, started)
        _, result = self.finished()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["movedCount"], 0, result)
        self.assertEqual(collision.read_bytes(), b"A new unrelated file created after preview")
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)

    def test_destination_that_becomes_a_reparse_point_is_rejected_before_any_move(self):
        import organizer
        _, original = self.files()
        plan = self.plan()
        forbidden = self.root / Path(plan["files"][0]["destination"]).parent
        forbidden.mkdir(parents=True, exist_ok=True)
        actual_reparse = organizer.is_reparse

        def became_link(path):
            return Path(path) == forbidden or actual_reparse(path)

        with patch("organizer.is_reparse", side_effect=became_link):
            status, started = self.execute(plan)
            self.assertEqual(status, 200, started)
            _, result = self.finished()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["movedCount"], 0, result)
        self.assertTrue(result["failures"], result)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)

    def test_forged_journal_paths_fail_rescan_without_changing_media_or_current_library(self):
        self.files()
        plan = self.plan()
        current, _ = self.organize(plan)
        journal = self.server.app.organizer.journal
        valid = journal.read_bytes()
        for field, forged in (("source", "../outside.txt"),
                              ("destination", "Organized by type/TXT/../../outside.txt"),
                              ("destination", ".photo-review-trash/journal.json")):
            with self.subTest(field=field, forged=forged):
                data = json.loads(valid)
                data["operations"][0]["files"][0][field] = forged
                journal.write_text(json.dumps(data), encoding="utf-8")
                before = {item["destination"]: (self.root / item["destination"]).read_bytes()
                          for item in plan["files"]}
                status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
                self.assertEqual(status, 409, body)
                state = self.request("GET", "/api/state")[1]
                self.assertEqual(state["photos"], current["photos"])
                self.assertEqual(state["revision"], current["revision"])
                self.assertEqual({name: (self.root / name).read_bytes() for name in before}, before)
                journal.write_bytes(valid)

    def test_undo_after_rescan_rejects_occupied_original_before_restoring_any_file(self):
        _, original = self.files()
        plan = self.plan()
        _, moved = self.organize(plan)
        (self.root / "one.txt").write_bytes(b"New unrelated original-location file")
        self.scan()
        status, started = self.undo(moved["operationId"])
        self.assertEqual(status, 200, started)
        state, result = self.finished()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["movedCount"], 0, result)
        self.assertTrue(result["failures"], result)
        self.assertEqual((self.root / "one.txt").read_bytes(), b"New unrelated original-location file")
        for item in plan["files"]:
            self.assertEqual((self.root / item["destination"]).read_bytes(), original[item["path"]])
        self.assertFalse((self.root / "nested/two.CUSTOM").exists())
        self.assertFalse((self.root / "extensionless").exists())
        # Removing only our generated collision permits retrying the durable
        # operation, including after a new explicit scan reloaded its journal.
        (self.root / "one.txt").unlink()
        self.scan()
        status, started = self.undo(moved["operationId"])
        self.assertEqual(status, 200, started)
        _, restored = self.finished()
        self.assertEqual(restored["movedCount"], len(original), restored)
        self.assertEqual(restored["failures"], [], restored)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)
        self.assertEqual(self.undo(moved["operationId"])[0], 409)

    def test_restart_flat_media_only_scan_then_undo_indexes_only_restored_visible_files(self):
        _, original = self.files(("image.png", "note.txt", "nested/inside.png"))
        plan = self.plan()
        _, moved = self.organize(plan)
        self.server.app.close()
        with patch("app.Organizer") as organizer:
            self.server.app = app.ReviewApp(self.root)
            startup = self.request("GET", "/api/state")[1]
            organizer.assert_not_called()
        self.token = startup["token"]
        self.assertFalse(startup["hasScanned"])
        self.assertEqual(startup["organization"]["history"], [])
        status, started = self.request("POST", "/api/scan", {
            "path": str(self.root), "recursive": False, "includeAllFiles": False})
        self.assertEqual(status, 200, started)
        flat = self.wait_for_scan()
        self.assertEqual(flat["photos"], [])
        self.assertTrue(any(item["id"] == moved["operationId"] and item["undoAvailable"]
                            for item in flat["organization"]["history"]))
        status, started = self.undo(moved["operationId"])
        self.assertEqual(status, 200, started)
        restored, result = self.finished()
        self.assertEqual(result["movedCount"], len(original), result)
        self.assertEqual(result["failures"], [], result)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)
        self.assertEqual([photo["path"] for photo in restored["photos"]], ["image.png"])
        self.assertEqual(restored["stats"]["total"], 1)
        status, found = self.request("POST", "/api/search", {"query": "image.png", "mode": "filename"})
        self.assertEqual(status, 200, found)
        self.assertEqual([photo["path"] for photo in found["results"]], ["image.png"])
        self.assertEqual(self.request("GET", "/api/photo/" + found["results"][0]["id"])[0], 200)

    def test_scan_blocks_preview_execute_and_undo(self):
        self.files()
        plan = self.plan()
        self.server.app.scanning = True
        try:
            for route, payload in (("/api/organize/plan", {"mode": "type"}),
                                   ("/api/organize/execute", {"planId": plan["planId"]}),
                                   ("/api/organize/undo", {"operationId": "invalid"})):
                status, body = self.request("POST", route, payload)
                self.assertEqual(status, 409, (route, body))
        finally:
            self.server.app.scanning = False

    def test_background_preflight_keeps_state_responsive_and_blocks_conflicting_actions(self):
        import organizer
        self.files()
        plan = self.plan()
        entered = threading.Event()
        release = threading.Event()
        actual_hash = organizer.hash_file

        def slow_preflight(path):
            entered.set()
            if not release.wait(8):
                raise OSError("Generated preflight fixture timed out")
            return actual_hash(path)

        try:
            with patch("organizer.hash_file", side_effect=slow_preflight):
                status, started = self.execute(plan)
                self.assertEqual(status, 200, started)
                self.assertTrue(entered.wait(3), "Preflight did not run in the background")
                began = time.monotonic()
                status, state = self.request("GET", "/api/state")
                self.assertEqual(status, 200, state)
                self.assertLess(time.monotonic() - began, 2, "State polling waited for a file hash")
                self.assertTrue(state["organization"]["running"])
                for route, payload in (("/api/organize/plan", {"mode": "type"}),
                                       ("/api/organize/execute", {"planId": plan["planId"]}),
                                       ("/api/organize/undo", {"operationId": "invalid"}),
                                       ("/api/scan", {"path": str(self.root)}),
                                       ("/api/search", {"query": "one", "mode": "filename"}),
                                       ("/api/restore", {"id": "invalid"})):
                    status, body = self.request("POST", route, payload)
                    self.assertEqual(status, 409, (route, body))
                release.set()
                _, result = self.finished()
                self.assertEqual(result["movedCount"], plan["moveCount"], result)
                self.assertEqual(result["failures"], [], result)
        finally:
            release.set()

    def test_source_changed_during_intent_write_is_kept_and_other_moves_can_be_undone(self):
        import organizer
        _, original = self.files()
        plan = self.plan()
        target = plan["files"][0]
        source = self.root / target["path"]
        actual_status = organizer.Organizer._record_status
        mutated = bytearray(original[target["path"]])
        mutated[-1] ^= 1

        def save_and_change(store, record, status, **updates):
            actual_status(store, record, status, **updates)
            if status == "pending" and record["source"] == target["path"]:
                info = source.stat()
                source.write_bytes(mutated)
                os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))

        with patch.object(organizer.Organizer, "_record_status", autospec=True, side_effect=save_and_change):
            status, started = self.execute(plan)
            self.assertEqual(status, 200, started)
            state, result = self.finished()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["movedCount"], plan["moveCount"] - 1, result)
        self.assertEqual(result["skippedCount"], 1, result)
        self.assertEqual(len(result["failures"]), 1, result)
        self.assertEqual(source.read_bytes(), mutated)
        self.assertFalse((self.root / target["destination"]).exists())
        self.assertIn(target["path"], {photo["path"] for photo in state["photos"]})
        status, started = self.undo(result["operationId"])
        self.assertEqual(status, 200, started)
        _, restored = self.finished()
        self.assertEqual(restored["movedCount"], plan["moveCount"] - 1, restored)
        self.assertEqual(restored["failures"], [], restored)
        self.assertEqual(source.read_bytes(), mutated)
        for name, contents in original.items():
            if name != target["path"]:
                self.assertEqual((self.root / name).read_bytes(), contents)
