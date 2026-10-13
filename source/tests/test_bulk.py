"""Bulk recovery tests use only generated files in temporary fixture folders."""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

import scanner
import storage
from tests.test_app import ApiFixture, sample_image


class BulkTrashTests(ApiFixture):
    def plan(self, groups, keepers=None):
        payload = {"groupIds": [group["id"] for group in groups]}
        if keepers is not None:
            payload["keepers"] = keepers
        status, body = self.request("POST", "/api/bulk-delete-plan", payload)
        self.assertEqual(status, 200, body)
        return body

    def commit(self, plan):
        return self.request("POST", "/api/bulk-delete", {"planId": plan["planId"], "confirmed": True})

    def exact_triple(self):
        self.write_exact_pair("a.png", "b.png")
        shutil.copyfile(self.root / "a.png", self.root / "c.png")
        state = self.scan()
        names = {photo["name"]: photo["id"] for photo in state["photos"]}
        group = next(item for item in state["groups"] if item["kind"] == "exact")
        return state, group, names

    def mixed_groups(self):
        # Unknown/extensionless media participates in exact matching too.
        (self.root / "exact-source").write_bytes(b"generated binary duplicate\x00\xff")
        shutil.copyfile(self.root / "exact-source", self.root / "exact-copy.custom")
        picture = sample_image()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "Generated metadata makes bytes differ.")
        picture.save(self.root / "pixels-a.png")
        picture.save(self.root / "pixels-b.png", pnginfo=metadata)
        similar = sample_image(variant=1)
        similar.save(self.root / "similar-large.jpg", quality=96)
        similar.resize((160, 120), Image.Resampling.LANCZOS).save(self.root / "similar-small.jpg", quality=75)
        (self.root / "unrelated.custom").write_bytes(b"generated unique file")
        state = self.scan()
        self.assertEqual({group["kind"] for group in state["groups"]}, {"exact", "pixels", "similar"})
        return state

    def replace_groups(self, names, definitions):
        """Force overlapping graph edges independently of similarity scoring."""
        groups = [{"id": group_id, "kind": kind, "score": 99,
                   "photos": [names[name] for name in members]}
                  for group_id, kind, members in definitions]
        with self.server.app.lock:
            self.server.app.result.groups = groups
            self.server.app.result.stats = scanner.calculate_stats(
                self.server.app.result.photos, groups)
        return groups

    def assert_all_present(self, names):
        for name in names:
            self.assertTrue((self.root / name).exists(), name)

    def assert_rejected_without_moves(self, plan, names):
        with patch.object(self.server.app.store, "move", wraps=self.server.app.store.move) as move:
            status, body = self.commit(plan)
        self.assertEqual(status, 409, body)
        move.assert_not_called()
        self.assert_all_present(names)
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_plan_only_previews_removals_and_keeps_one_per_group(self):
        state, group, names = self.exact_triple()
        before = {name: (self.root / name).read_bytes() for name in names}
        with patch.object(self.server.app.store, "move") as move:
            plan = self.plan([group])
        move.assert_not_called()
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["groupCount"], 1)
        self.assertEqual(plan["keepCount"], 1)
        self.assertEqual(plan["totalBytes"], sum(item["size"] for item in plan["files"]))
        self.assertEqual(len({item["id"] for item in plan["files"]}), 2)
        self.assertTrue(all(item["keepName"] and item["keepPath"] for item in plan["files"]))
        self.assertEqual(self.request("GET", "/api/state")[1], state)
        self.assertFalse(self.server.app.store.directory.exists())
        self.assertEqual({name: (self.root / name).read_bytes() for name in names}, before)

    def test_all_filter_removes_copies_from_every_kind_and_preserves_unique_media(self):
        state = self.mixed_groups()
        original = {photo["path"]: (self.root / photo["path"]).read_bytes() for photo in state["photos"]}
        plan = self.plan(state["groups"])
        self.assertEqual(plan["count"], 3)
        self.assertEqual(plan["groupCount"], 3)
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["deletedCount"], 3)
        self.assertEqual(removed["failures"], [])
        self.assertEqual(removed["state"]["stats"]["total"], 4)
        self.assertEqual(removed["state"]["groups"], [])
        self.assertEqual(removed["trashCount"], 3)
        self.assertEqual((self.root / "unrelated.custom").read_bytes(), original["unrelated.custom"])
        for kept in plan["kept"]:
            self.assertEqual((self.root / kept["path"]).read_bytes(), original[kept["path"]])

    def test_exact_filter_includes_pixel_matches_and_leaves_similar_photos(self):
        state = self.mixed_groups()
        groups = [group for group in state["groups"] if group["kind"] != "similar"]
        plan = self.plan(groups)
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["groupCount"], 2)
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["deletedCount"], 2)
        self.assert_all_present(["similar-large.jpg", "similar-small.jpg", "unrelated.custom"])
        self.assertEqual([group["kind"] for group in removed["state"]["groups"]], ["similar"])

    def test_similar_filter_leaves_exact_and_pixel_groups(self):
        state = self.mixed_groups()
        plan = self.plan([group for group in state["groups"] if group["kind"] == "similar"])
        self.assertEqual(plan["count"], 1)
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["deletedCount"], 1)
        self.assert_all_present(["exact-source", "exact-copy.custom", "pixels-a.png", "pixels-b.png"])
        self.assertEqual({group["kind"] for group in removed["state"]["groups"]}, {"exact", "pixels"})

    def test_active_keeper_choice_is_respected(self):
        _, group, names = self.exact_triple()
        # Pick a file other than the first recommended group member.
        preferred = group["photos"][-1]
        plan = self.plan([group], {group["id"]: preferred})
        self.assertEqual([item["id"] for item in plan["kept"]], [preferred])
        self.assertNotIn(preferred, [item["id"] for item in plan["files"]])
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertEqual({photo["id"] for photo in removed["state"]["photos"]}, {preferred})

    def test_overlapping_group_keepers_are_protected_across_whole_batch(self):
        _, _, names = self.exact_triple()
        groups = self.replace_groups(names, [("edge-one", "exact", ["a.png", "b.png"]),
                                             ("edge-two", "similar", ["b.png", "c.png"])])
        plan = self.plan(groups, {"edge-one": names["b.png"], "edge-two": names["c.png"]})
        self.assertEqual(plan["keepCount"], 2)
        self.assertEqual({item["id"] for item in plan["files"]}, {names["a.png"]})
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["deletedCount"], 1)
        self.assert_all_present(["b.png", "c.png"])

    def test_copy_in_multiple_groups_is_moved_once(self):
        _, _, names = self.exact_triple()
        groups = self.replace_groups(names, [("wide", "pixels", ["a.png", "b.png", "c.png"]),
                                             ("edge", "similar", ["a.png", "b.png"])])
        plan = self.plan(groups, {"wide": names["a.png"], "edge": names["a.png"]})
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["keepCount"], 1)
        with patch.object(self.server.app.store, "move", wraps=self.server.app.store.move) as move:
            status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertEqual(move.call_count, 2)
        self.assertEqual({item["sourceId"] for item in removed["deleted"]}, {names["b.png"], names["c.png"]})
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 2)

    def test_every_bulk_removal_can_be_restored_without_rescanning(self):
        state = self.mixed_groups()
        original = {photo["path"]: (self.root / photo["path"]).read_bytes() for photo in state["photos"]}
        status, removed = self.commit(self.plan(state["groups"]))
        self.assertEqual(status, 200, removed)
        with patch("app.scan_folder") as scan:
            for item in removed["deleted"]:
                status, restored = self.request("POST", "/api/restore", {"id": item["id"]})
                self.assertEqual(status, 200, restored)
                self.assertFalse(restored["state"]["scanning"])
            scan.assert_not_called()
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)
        final = self.request("GET", "/api/state")[1]
        self.assertEqual({group["id"] for group in final["groups"]}, {group["id"] for group in state["groups"]})
        self.assertEqual(final["stats"], state["stats"])
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_commit_requires_literal_confirmation(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group])
        for value in (False, None, "true", 1):
            with self.subTest(confirmed=value):
                status, body = self.request("POST", "/api/bulk-delete", {"planId": plan["planId"], "confirmed": value})
                self.assertGreaterEqual(status, 400, body)
                self.assertLess(status, 500, body)
        status, body = self.request("POST", "/api/bulk-delete", {"planId": plan["planId"]})
        self.assertGreaterEqual(status, 400, body)
        self.assertLess(status, 500, body)
        self.assert_all_present(names)
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_invalid_group_or_unrelated_keeper_cannot_create_actionable_plan(self):
        _, group, names = self.exact_triple()
        for payload in ({"groupIds": []}, {"groupIds": ["stale-group"]},
                        {"groupIds": "all"}, {"groupIds": [group["id"], "stale-group"]},
                        {"groupIds": [group["id"]], "keepers": {group["id"]: "unknown-file"}}):
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/bulk-delete-plan", payload)
                self.assertGreaterEqual(status, 400, body)
                self.assertLess(status, 500, body)
        self.assert_all_present(names)

    def test_rescan_and_completed_batch_invalidate_old_plans(self):
        _, group, names = self.exact_triple()
        stale = self.plan([group])
        state = self.scan()
        self.assert_rejected_without_moves(stale, names)
        plan = self.plan(state["groups"])
        status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        status, body = self.commit(plan)
        self.assertEqual(status, 409, body)
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 2)

    def test_single_delete_and_restore_invalidate_pending_batch(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group], {group["id"]: names["a.png"]})
        status, removed = self.request("POST", "/api/delete", self.delete_payload(group, names, "b.png", "a.png"))
        self.assertEqual(status, 200, removed)
        status, body = self.commit(plan)
        self.assertEqual(status, 409, body)
        current = self.request("GET", "/api/state")[1]
        pending = self.plan(current["groups"])
        status, restored = self.request("POST", "/api/restore", {"id": removed["trashId"]})
        self.assertEqual(status, 200, restored)
        self.assert_rejected_without_moves(pending, names)

    def test_changed_candidate_hash_blocks_batch_before_first_move(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group], {group["id"]: names["a.png"]})
        # Changing the final candidate proves that earlier ones were preflighted
        # together rather than moved as each file was checked.
        candidate = self.root / plan["files"][-1]["path"]
        signature = candidate.stat()
        changed = bytearray(candidate.read_bytes())
        changed[len(changed) // 2] ^= 1
        candidate.write_bytes(changed)
        os.utime(candidate, ns=(signature.st_atime_ns, signature.st_mtime_ns))
        self.assert_rejected_without_moves(plan, names)

    def test_changed_keeper_hash_blocks_whole_batch(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group], {group["id"]: names["a.png"]})
        keeper = self.root / "a.png"
        signature = keeper.stat()
        changed = bytearray(keeper.read_bytes())
        changed[len(changed) // 2] ^= 1
        keeper.write_bytes(changed)
        os.utime(keeper, ns=(signature.st_atime_ns, signature.st_mtime_ns))
        self.assert_rejected_without_moves(plan, names)

    def test_missing_late_candidate_blocks_whole_batch(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group], {group["id"]: names["a.png"]})
        missing = plan["files"][-1]["path"]
        (self.root / missing).unlink()
        self.assert_rejected_without_moves(plan, [name for name in names if name != missing])

    def test_scan_in_progress_blocks_plan_and_commit(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group])
        entered, release = threading.Event(), threading.Event()
        real_scan = scanner.scan_folder

        def paused_scan(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test scan was not released")
            return real_scan(*args, **kwargs)

        with patch("app.scan_folder", side_effect=paused_scan):
            try:
                status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
                self.assertEqual(status, 200, body)
                self.assertTrue(entered.wait(timeout=3))
                for route, payload in (("/api/bulk-delete-plan", {"groupIds": [group["id"]]}),
                                       ("/api/bulk-delete", {"planId": plan["planId"], "confirmed": True})):
                    status, body = self.request("POST", route, payload)
                    self.assertEqual(status, 409, body)
                self.assert_all_present(names)
            finally:
                release.set()
            self.wait_for_scan()

    def test_plan_and_commit_require_same_origin_token(self):
        _, group, names = self.exact_triple()
        plan = self.plan([group])
        invalid_headers = ({"Origin": self.origin},
                           {"Origin": self.origin, "X-Review-Token": "wrong"},
                           {"Origin": "https://example.invalid", "X-Review-Token": self.token},
                           {"X-Review-Token": self.token})
        for route, payload in (("/api/bulk-delete-plan", {"groupIds": [group["id"]]}),
                               ("/api/bulk-delete", {"planId": plan["planId"], "confirmed": True})):
            for headers in invalid_headers:
                with self.subTest(route=route, headers=headers):
                    status, body = self.request("POST", route, payload, headers, authorized=False)
                    self.assertEqual(status, 403, body)
        self.assert_all_present(names)
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_partial_io_failure_reports_actual_moves_and_retains_failed_copy(self):
        _, group, names = self.exact_triple()
        original = {name: (self.root / name).read_bytes() for name in names}
        plan = self.plan([group], {group["id"]: names["a.png"]})
        failed_path = self.root / plan["files"][-1]["path"]
        real_rename = os.rename

        def failed_rename(source, destination, *args, **kwargs):
            if Path(source) == failed_path:
                raise OSError("Generated test file is locked")
            return real_rename(source, destination, *args, **kwargs)

        with patch("storage.os.rename", side_effect=failed_rename):
            status, removed = self.commit(plan)
        self.assertEqual(status, 200, removed)
        self.assertTrue(removed["ok"])
        self.assertEqual(removed["deletedCount"], 1)
        self.assertEqual(len(removed["failures"]), 1)
        self.assertEqual(removed["failures"][0]["path"], failed_path.name)
        self.assertEqual(failed_path.read_bytes(), original[failed_path.name])
        self.assertEqual((self.root / "a.png").read_bytes(), original["a.png"])
        self.assertEqual(removed["state"]["stats"]["total"], 2)
        remaining_ids = {photo["id"] for photo in removed["state"]["photos"]}
        self.assertEqual(remaining_ids, {names["a.png"], names[failed_path.name]})
        trash = self.request("GET", "/api/trash")[1]["items"]
        self.assertEqual(len(trash), 1)
        self.assertEqual(trash[0]["id"], removed["deleted"][0]["id"])
        status, body = self.commit(plan)
        self.assertEqual(status, 409, body)
        status, restored = self.request("POST", "/api/restore", {"id": trash[0]["id"]})
        self.assertEqual(status, 200, restored)
        self.assertEqual({name: (self.root / name).read_bytes() for name in names}, original)

    def test_final_journal_save_failure_still_reports_and_recovers_completed_move(self):
        group, names = self.scan_pair()
        original = (self.root / "first.png").read_bytes()
        plan = self.plan([group], {group["id"]: names["second.png"]})
        store = self.server.app.store
        real_save = store._save
        save_calls = []

        def fail_final_save_once():
            save_calls.append(store.records[-1]["status"])
            if len(save_calls) == 2:
                self.assertEqual(store.records[-1]["status"], "trashed")
                self.assertFalse((self.root / "first.png").exists())
                self.assertTrue(store._stored(store.records[-1]).is_file())
                raise storage.StorageError("Generated final journal write failure")
            return real_save()

        with patch.object(store, "_save", side_effect=fail_final_save_once):
            status, removed = self.commit(plan)
        self.assertEqual(save_calls, ["pending", "trashed"])
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["deletedCount"], 1)
        self.assertEqual(removed["failures"], [])
        self.assertEqual(removed["state"]["groups"], [])
        self.assertEqual({photo["id"] for photo in removed["state"]["photos"]}, {names["second.png"]})
        self.assertEqual((self.root / "second.png").read_bytes(), original)
        journal = json.loads(store.journal.read_text(encoding="utf-8"))
        self.assertEqual(journal["items"][0]["status"], "pending")

        reopened = storage.TrashStore(self.root)
        items = reopened.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], removed["deleted"][0]["id"])
        self.assertEqual(reopened.records[0]["status"], "trashed")
        with self.server.app.lock:
            self.server.app.store = reopened
        with patch("app.scan_folder") as scan:
            status, restored = self.request("POST", "/api/restore", {"id": items[0]["id"]})
            scan.assert_not_called()
        self.assertEqual(status, 200, restored)
        self.assertEqual((self.root / "first.png").read_bytes(), original)
        self.assertEqual((self.root / "second.png").read_bytes(), original)
        self.assertEqual(restored["state"]["stats"]["total"], 2)
        self.assertEqual(reopened.items(), [])

    def test_concurrent_bulk_and_opposite_single_delete_leave_a_survivor(self):
        group, names = self.scan_pair()
        plan = self.plan([group], {group["id"]: names["first.png"]})
        barrier = threading.Barrier(3)
        responses = []

        def bulk_remove():
            barrier.wait(timeout=3)
            responses.append(self.commit(plan))

        def single_remove():
            barrier.wait(timeout=3)
            responses.append(self.request("POST", "/api/delete", self.delete_payload(group, names, "first.png", "second.png")))

        workers = [threading.Thread(target=bulk_remove), threading.Thread(target=single_remove)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=3)
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(status for status, _ in responses), [200, 409], responses)
        self.assertEqual(sum((self.root / name).exists() for name in names), 1)
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 1)
