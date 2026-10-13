"""Scoped trash tests operate only on generated temporary duplicate images."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import scanner
from tests.test_app import ApiFixture, sample_image


class FilteredBulkTrashTests(ApiFixture):
    def classified_copies(self, count=3):
        names = ["original.png", "Screenshot-copy.png", "ordinary-copy.png", "downloaded-copy.png"][:count]
        sample_image().save(self.root / names[0])
        for name in names[1:]:
            shutil.copyfile(self.root / names[0], self.root / name)
        state = self.scan()
        ids = {photo["name"]: photo["id"] for photo in state["photos"]}
        group = next(group for group in state["groups"] if group["kind"] == "exact")
        # Isolate candidate scope from OCR/metadata detection: all test tags are
        # explicit fixture metadata, never inferred from user images.
        with self.server.app.lock:
            for name, member in ids.items():
                self.server.app.result.photos[member].imageCategories = (
                    ["screenshot"] if name.startswith("Screenshot") else
                    ["downloaded"] if name.startswith("downloaded") else [])
        return group, ids

    def plan(self, groups, ids, candidates):
        payload = {"groupIds": [group["id"] for group in groups],
                   "keepers": {group["id"]: ids["original.png"] for group in groups},
                   "candidateIds": candidates}
        status, body = self.request("POST", "/api/bulk-delete-plan", payload)
        self.assertEqual(status, 200, body)
        return body

    def commit(self, plan):
        return self.request("POST", "/api/bulk-delete", {"planId": plan["planId"], "confirmed": True})

    def assert_present(self, *names):
        for name in names:
            self.assertTrue((self.root / name).is_file(), name)

    def test_screenshot_filter_moves_only_matching_copy_and_retains_other_copies(self):
        group, ids = self.classified_copies()
        before = {name: (self.root / name).read_bytes() for name in ids}
        plan = self.plan([group], ids, [ids["Screenshot-copy.png"]])
        self.assertEqual(plan["count"], 1)
        self.assertEqual([file["id"] for file in plan["files"]], [ids["Screenshot-copy.png"]])
        self.assert_present(*ids)
        status, result = self.commit(plan)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deletedCount"], 1)
        self.assertFalse((self.root / "Screenshot-copy.png").exists())
        self.assert_present("original.png", "ordinary-copy.png")
        for name in ("original.png", "ordinary-copy.png"):
            self.assertEqual((self.root / name).read_bytes(), before[name])
        self.assertEqual({photo["id"] for photo in result["state"]["photos"]},
                         {ids["original.png"], ids["ordinary-copy.png"]})
        self.assertEqual(len(result["state"]["groups"]), 1)
        trash_id = result["deleted"][0]["id"]
        status, restored = self.request("POST", "/api/restore", {"id": trash_id})
        self.assertEqual(status, 200, restored)
        self.assertEqual((self.root / "Screenshot-copy.png").read_bytes(), before["Screenshot-copy.png"])

    def test_matching_keeper_alone_produces_zero_removals(self):
        group, ids = self.classified_copies()
        status, plan = self.request("POST", "/api/bulk-delete-plan", {
            "groupIds": [group["id"]], "keepers": {group["id"]: ids["Screenshot-copy.png"]},
            "candidateIds": [ids["Screenshot-copy.png"]]})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["files"], [])
        self.assertEqual([file["id"] for file in plan["kept"]], [ids["Screenshot-copy.png"]])
        with patch.object(self.server.app.store, "move") as move:
            status, response = self.commit(plan)
        self.assertEqual(status, 409, response)
        move.assert_not_called()
        self.assert_present(*ids)

    def test_empty_candidate_list_is_safe_and_does_not_fall_back_to_all_copies(self):
        group, ids = self.classified_copies()
        plan = self.plan([group], ids, [])
        self.assertEqual(plan["count"], 0)
        self.assertEqual(plan["totalBytes"], 0)
        self.assertEqual(plan["files"], [])
        with patch.object(self.server.app.store, "move") as move:
            status, response = self.commit(plan)
        self.assertEqual(status, 409, response)
        move.assert_not_called()
        self.assertFalse(self.server.app.store.directory.exists())
        self.assert_present(*ids)

    def test_invalid_or_outside_candidates_reject_without_creating_a_plan(self):
        group, ids = self.classified_copies()
        sample_image(variant=1).save(self.root / "unique.png")
        state = self.scan()
        group = next(group for group in state["groups"] if group["kind"] == "exact")
        unique = next(photo["id"] for photo in state["photos"] if photo["name"] == "unique.png")
        invalid = (None, "all", True, [""], [42], [[]], ["unknown-file"],
                   [unique], [ids["Screenshot-copy.png"], ids["Screenshot-copy.png"]])
        for candidates in invalid:
            with self.subTest(candidates=candidates):
                status, response = self.request("POST", "/api/bulk-delete-plan", {
                    "groupIds": [group["id"]], "candidateIds": candidates})
                self.assertEqual(status, 409, response)
        self.assertEqual(len(self.server.app.bulk_plans), 0)
        self.assert_present(*ids, "unique.png")
        self.assertEqual(self.request("GET", "/api/trash")[1]["items"], [])

    def test_omitted_candidates_preserve_existing_bulk_plan_payload(self):
        group, ids = self.classified_copies()
        payload = {"groupIds": [group["id"]], "keepers": {group["id"]: ids["original.png"]}}
        status, original = self.request("POST", "/api/bulk-delete-plan", payload)
        self.assertEqual(status, 200, original)
        status, explicit = self.request("POST", "/api/bulk-delete-plan", dict(payload, candidateIds=list(ids.values())))
        self.assertEqual(status, 200, explicit)
        self.assertEqual({key: value for key, value in original.items() if key != "planId"},
                         {key: value for key, value in explicit.items() if key != "planId"})
        self.assertEqual(original["count"], 2)
        self.assertEqual(set(original), {"planId", "count", "totalBytes", "groupCount", "keepCount", "files", "kept"})

    def test_overlapping_groups_deduplicate_candidates_and_protect_every_keeper(self):
        group, ids = self.classified_copies(4)
        groups = [{"id": "first-edge", "kind": "similar", "score": 99,
                   "photos": [ids["original.png"], ids["Screenshot-copy.png"], ids["ordinary-copy.png"]]},
                  {"id": "second-edge", "kind": "similar", "score": 99,
                   "photos": [ids["Screenshot-copy.png"], ids["ordinary-copy.png"], ids["downloaded-copy.png"]]}]
        with self.server.app.lock:
            self.server.app.result.groups = groups
            self.server.app.result.stats = scanner.calculate_stats(self.server.app.result.photos, groups)
        candidates = [ids["Screenshot-copy.png"], ids["ordinary-copy.png"], ids["downloaded-copy.png"]]
        status, plan = self.request("POST", "/api/bulk-delete-plan", {
            "groupIds": [group["id"] for group in groups],
            "keepers": {"first-edge": ids["original.png"], "second-edge": ids["downloaded-copy.png"]},
            "candidateIds": candidates})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["keepCount"], 2)
        self.assertEqual({file["id"] for file in plan["files"]},
                         {ids["Screenshot-copy.png"], ids["ordinary-copy.png"]})
        with patch.object(self.server.app.store, "move", wraps=self.server.app.store.move) as move:
            status, result = self.commit(plan)
        self.assertEqual(status, 200, result)
        self.assertEqual(move.call_count, 2)
        self.assertEqual(len({file["sourceId"] for file in result["deleted"]}), 2)
        self.assert_present("original.png", "downloaded-copy.png")

    def test_candidate_list_is_frozen_at_confirmation_preparation(self):
        group, ids = self.classified_copies()
        candidates = [ids["Screenshot-copy.png"]]
        plan = self.server.app.bulk_delete_plan([group["id"]], {group["id"]: ids["original.png"]}, candidates)
        candidates.append(ids["ordinary-copy.png"])
        status, result = self.commit(plan)
        self.assertEqual(status, 200, result)
        self.assertEqual([file["sourceId"] for file in result["deleted"]], [ids["Screenshot-copy.png"]])
        self.assert_present("original.png", "ordinary-copy.png")

    def test_filtered_batch_defaults_keep_nonmatching_originals_even_for_larger_candidates(self):
        first_original = self.root / "first-original.png"
        second_original = self.root / "second-original.png"
        sample_image().resize((160, 120)).save(first_original)
        sample_image(variant=1).resize((160, 120)).save(second_original)
        sample_image().save(self.root / "Screenshot-first.png")
        sample_image(variant=1).save(self.root / "Screenshot-second.png")
        state = self.scan()
        ids = {photo["name"]: photo["id"] for photo in state["photos"]}
        candidates = [ids["Screenshot-first.png"], ids["Screenshot-second.png"]]
        originals = [ids["first-original.png"], ids["second-original.png"]]
        groups = [{"id": "first-size", "kind": "similar", "score": 99,
                   "photos": [originals[0], candidates[0]]},
                  {"id": "second-size", "kind": "similar", "score": 99,
                   "photos": [originals[1], candidates[1]]}]
        with self.server.app.lock:
            self.server.app.result.groups = groups
            self.server.app.result.stats = scanner.calculate_stats(self.server.app.result.photos, groups)
        status, plan = self.request("POST", "/api/bulk-delete-plan", {
            "groupIds": [group["id"] for group in groups], "candidateIds": candidates})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["count"], 2)
        self.assertEqual({file["id"] for file in plan["kept"]}, set(originals))
        self.assertEqual({file["id"] for file in plan["files"]}, set(candidates))
        status, result = self.commit(plan)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deletedCount"], 2)
        self.assert_present("first-original.png", "second-original.png")

    def test_changed_classification_invalidates_confirmation_before_any_move(self):
        group, ids = self.classified_copies()
        plan = self.plan([group], ids, [ids["Screenshot-copy.png"]])
        self.server.app.result.photos[ids["Screenshot-copy.png"]].imageCategories = []
        with patch.object(self.server.app.store, "move") as move:
            status, response = self.commit(plan)
        self.assertEqual(status, 409, response)
        move.assert_not_called()
        self.assert_present(*ids)

    def test_changed_nonmatching_group_member_still_blocks_stale_confirmation(self):
        group, ids = self.classified_copies()
        plan = self.plan([group], ids, [ids["Screenshot-copy.png"]])
        excluded_copy = self.root / "ordinary-copy.png"
        original_stat = excluded_copy.stat()
        changed = bytearray(excluded_copy.read_bytes())
        changed[len(changed) // 2] ^= 1
        excluded_copy.write_bytes(changed)
        os.utime(excluded_copy, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with patch.object(self.server.app.store, "move") as move:
            status, response = self.commit(plan)
        self.assertEqual(status, 409, response)
        move.assert_not_called()
        self.assert_present(*ids)

    def test_filtered_partial_failure_retains_failed_and_nonmatching_copies(self):
        group, ids = self.classified_copies(4)
        plan = self.plan([group], ids, [ids["Screenshot-copy.png"], ids["downloaded-copy.png"]])
        real_rename = os.rename

        def refuse_one(source, destination, *args, **kwargs):
            if Path(source) == self.root / "downloaded-copy.png":
                raise OSError("Generated filtered copy is locked")
            return real_rename(source, destination, *args, **kwargs)

        with patch("storage.os.rename", side_effect=refuse_one):
            status, result = self.commit(plan)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deletedCount"], 1)
        self.assertEqual(result["failures"][0]["id"], ids["downloaded-copy.png"])
        self.assert_present("original.png", "ordinary-copy.png", "downloaded-copy.png")
        self.assertFalse((self.root / "Screenshot-copy.png").exists())
        self.assertEqual(len(self.request("GET", "/api/trash")[1]["items"]), 1)

