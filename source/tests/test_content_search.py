"""Library search uses generated files, private OCR strings, and fake embeddings."""
from __future__ import annotations

import math
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import app
from tests.test_app import ApiFixture, sample_image


def embedding(similarity=1.0):
    """A normalized fixture whose cosine with the first axis is similarity."""
    return (similarity, math.sqrt(1 - similarity ** 2), *([0.0] * 510))


class ContentSearchTests(ApiFixture):
    def setUp(self):
        super().setUp()
        self.capability = patch("app.visual_search.capabilities", return_value={
            "available": True, "reason": "", "model": "generated-test-model"})
        self.query_engine = patch("app.visual_search.embed_text", return_value=embedding())
        self.capability.start()
        self.embed_text = self.query_engine.start()
        self.addCleanup(self.capability.stop)
        self.addCleanup(self.query_engine.stop)

    def library(self, names=("image.png",)):
        for index, name in enumerate(names):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.suffix.lower() == ".png":
                sample_image(variant=index % 2).save(destination)
            else:
                destination.write_bytes(b"Generated nonimage media " + str(index).encode())
        state = self.scan()
        with self.server.app.lock:
            photos = {photo.path.replace("\\", "/"): photo for photo in self.server.app.result.photos.values()}
            for photo in photos.values():
                photo.ocrText = ""
                photo.ocrSearchStatus = "empty" if photo.mediaType == "image" else "not-image"
                photo.visualEmbedding = None
                photo.visualStatus = "unavailable" if photo.mediaType == "image" else "not-image"
                photo.imageCategories = []
        return state, photos

    def search(self, query, **options):
        status, body = self.request("POST", "/api/search", dict(query=query, **options))
        self.assertEqual(status, 200, body)
        return body

    def test_search_requires_scan_and_never_starts_a_scan_or_reads_a_directory(self):
        with patch("app.scan_folder") as scan, patch("app.TrashStore") as store:
            status, body = self.request("POST", "/api/search", {"query": "Alice"})
            self.assertEqual(status, 409, body)
            self.assertEqual(self.request("GET", "/api/state")[1]["photos"], [])
            self.assertIsNone(self.server.app.scan_thread)
            scan.assert_not_called()
            store.assert_not_called()
            self.embed_text.assert_not_called()

    def test_ocr_finds_unique_file_single_name_number_and_unicode_with_private_index(self):
        state, photos = self.library()
        self.assertEqual(state["groups"], [])
        photo = photos["image.png"]
        photo.ocrText = "Alice 4281 Straße කමල්"
        photo.ocrSearchStatus = "indexed"
        for query in ("alice", "4281", "STRASSE", "කමල්"):
            with self.subTest(query=query):
                body = self.search(query, mode="text")
                self.assertEqual(body["total"], 1)
                self.assertEqual(body["results"][0]["id"], photo.id)
                self.assertEqual(body["results"][0]["matches"], ["text"])
                self.assertEqual(body["textIndexedImages"], 1)
                self.assertNotIn("ocrText", body["results"][0])
                self.assertNotIn("visualEmbedding", body["results"][0])
        public = self.request("GET", "/api/state")[1]
        self.assertNotIn("Alice", str(public))
        self.embed_text.assert_not_called()

    def test_text_search_normalizes_whitespace_and_requires_every_query_word(self):
        _, photos = self.library()
        photos["image.png"].ocrText = "Invoice\nAlice\tCustomer\n4281"
        body = self.search("  4281 \t alice \n", mode="text")
        self.assertEqual(body["query"], "4281 alice")
        self.assertEqual(body["total"], 1)
        self.assertEqual(self.search("Alice Bob", mode="text")["total"], 0)

    def test_filename_search_covers_relative_subfolders_and_all_media_types(self):
        _, photos = self.library(("Holiday/image.png", "alice-recording.mp3", "invoice-4281.custom", "alice-film.mp4"))
        for query, expected in (("holiday", "Holiday/image.png"), ("4281", "invoice-4281.custom")):
            body = self.search(query, mode="filename")
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["results"][0]["id"], photos[expected].id)
            self.assertFalse(Path(body["results"][0]["path"]).is_absolute())
        self.assertEqual(self.search("alice", mode="filename")["total"], 2)
        self.assertEqual(self.search("alice", mode="filename", mediaType="audio")["total"], 1)
        self.assertEqual(self.search("alice", mode="text")["total"], 0)
        self.embed_text.assert_not_called()

    def test_visual_search_is_ranked_ignores_weak_suggestions_and_nonimage_files(self):
        _, photos = self.library(("strong.png", "second.png", "weak.png", "no-image.custom"))
        for path, similarity in (("strong.png", .83), ("second.png", .41), ("weak.png", .19), ("no-image.custom", .98)):
            photos[path].visualEmbedding = embedding(similarity)
        body = self.search("dog on a beach", mode="visual")
        self.assertEqual([result["name"] for result in body["results"]], ["strong.png", "second.png"])
        self.assertEqual([result["matches"] for result in body["results"]], [["visual"], ["visual"]])
        self.assertEqual([result["visualScore"] for result in body["results"]], [.83, .41])
        self.assertEqual(body["visualIndexedImages"], 3)
        self.embed_text.assert_called_once_with("dog on a beach")

    def test_all_content_prioritizes_ocr_then_filename_and_deduplicates_a_file(self):
        _, photos = self.library(("image.png", "dog-picture.png", "visual.png"))
        photos["image.png"].ocrText = "DOG Alice 4281"
        photos["image.png"].ocrSearchStatus = "indexed"
        photos["image.png"].visualEmbedding = embedding(.25)
        photos["dog-picture.png"].visualEmbedding = embedding(.41)
        photos["visual.png"].visualEmbedding = embedding(.90)
        body = self.search("dog")
        self.assertEqual([result["name"] for result in body["results"]], ["image.png", "dog-picture.png", "visual.png"])
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["results"][0]["matches"], ["text", "visual"])
        self.assertEqual(body["results"][1]["matches"], ["filename", "visual"])
        self.assertEqual(len({result["id"] for result in body["results"]}), 3)

    def test_category_filter_applies_to_each_file_and_allows_overlapping_tags(self):
        _, photos = self.library(("first.png", "second.png", "third.png", "fourth.custom"))
        for photo in photos.values():
            photo.ocrText = "Alice"
        photos["first.png"].imageCategories = ["screenshot", "downloaded", "text"]
        photos["second.png"].imageCategories = ["downloaded"]
        for category, expected in (("screenshot", ["first.png"]),
                                   ("downloaded", ["first.png", "second.png"]),
                                   ("text", ["first.png"]), ("other", ["third.png"])):
            body = self.search("alice", mode="text", category=category)
            self.assertEqual(sorted(result["name"] for result in body["results"]), expected)
        self.assertEqual(self.search("fourth", mode="filename", category="other")["total"], 0)

    def test_pagination_is_stable_and_count_describes_total_results(self):
        self.library(tuple(f"alice-{index}.custom" for index in range(5)))
        pages = [self.search("alice", mode="filename", offset=offset, limit=2) for offset in (0, 2, 4, 20)]
        self.assertEqual([page["total"] for page in pages], [5, 5, 5, 5])
        self.assertEqual([len(page["results"]) for page in pages], [2, 2, 1, 0])
        self.assertEqual([page["hasMore"] for page in pages], [True, True, False, False])
        results = [item["name"] for page in pages for item in page["results"]]
        self.assertEqual(results, [f"alice-{index}.custom" for index in range(5)])

    def test_unsupported_input_is_rejected_without_model_execution(self):
        self.library()
        invalid = ({}, {"query": None}, {"query": 42}, {"query": []}, {"query": " "},
                   {"query": "x" * 257}, {"query": "Alice", "mode": []},
                   {"query": "Alice", "mode": "regex"}, {"query": "Alice", "mediaType": {}},
                   {"query": "Alice", "category": "car"}, {"query": "Alice", "offset": -1},
                   {"query": "Alice", "offset": True}, {"query": "Alice", "offset": 1.2},
                   {"query": "Alice", "limit": 0}, {"query": "Alice", "limit": 101},
                   {"query": "Alice", "limit": "50"}, {"query": "Alice", "limit": False})
        for payload in invalid:
            with self.subTest(payload=payload):
                status, body = self.request("POST", "/api/search", payload)
                self.assertEqual(status, 400, body)
        self.embed_text.assert_not_called()

    def test_query_punctuation_is_literal_and_snippets_are_limited(self):
        _, photos = self.library()
        photos["image.png"].ocrText = "Alice 4281 " + "Prefix " * 50 + '<img src=x onerror="x">' + " Suffix" * 50
        literal = '<img src=x onerror="x">'
        body = self.search(literal, mode="text")
        self.assertEqual(body["query"], literal)
        self.assertEqual(body["total"], 1)
        self.assertLessEqual(len(body["results"][0]["snippet"]), 180)
        self.assertIn(literal, body["results"][0]["snippet"])
        self.assertEqual(self.search("[A-Z].*", mode="text")["total"], 0)

    def test_unavailable_model_and_incomplete_ocr_are_explicit_without_fake_matches(self):
        _, photos = self.library()
        photos["image.png"].ocrText = "Alice"
        photos["image.png"].ocrSearchStatus = "error"
        photos["image.png"].visualEmbedding = embedding(.90)
        with patch("app.visual_search.capabilities", return_value={"available": False, "reason": "Offline model missing"}):
            body = self.search("Alice")
            self.assertFalse(body["visualAvailable"])
            self.assertEqual(body["results"][0]["matches"], ["text"])
            self.assertIn("Offline model missing", body["notes"])
            self.assertTrue(any("incomplete" in note for note in body["notes"]))
            self.assertEqual(self.search("dog", mode="visual")["total"], 0)
        self.embed_text.assert_not_called()

    def test_visual_runtime_failure_preserves_filename_and_ocr_search(self):
        _, photos = self.library(("alice.png",))
        photos["alice.png"].visualEmbedding = embedding(.90)
        self.embed_text.return_value = None
        body = self.search("Alice")
        self.assertFalse(body["visualAvailable"])
        self.assertEqual(body["results"][0]["matches"], ["filename"])
        self.assertTrue(body["notes"])

    def test_search_is_rejected_during_scan_and_for_library_changed_during_inference(self):
        _, photos = self.library()
        photos["image.png"].visualEmbedding = embedding(.90)
        self.server.app.scanning = True
        try:
            status, body = self.request("POST", "/api/search", {"query": "dog"})
            self.assertEqual(status, 409, body)
            self.embed_text.assert_not_called()
        finally:
            self.server.app.scanning = False
        def concurrent_change(query):
            self.server.app._invalidate_bulk_plans()
            return embedding()
        self.embed_text.side_effect = concurrent_change
        status, body = self.request("POST", "/api/search", {"query": "dog"})
        self.assertEqual(status, 409, body)
        self.assertIn("changed", body["error"])

    def test_search_and_show_file_require_current_token_origin_and_host(self):
        _, photos = self.library()
        for route, payload in (("/api/search", {"query": "image", "mode": "filename"}),
                               ("/api/show-file", {"id": photos["image.png"].id})):
            for headers in ({}, {"Origin": self.origin, "X-Review-Token": "wrong"},
                            {"Origin": "https://example.invalid", "X-Review-Token": self.token},
                            {"Host": "example.invalid", "Origin": self.origin, "X-Review-Token": self.token}):
                with self.subTest(route=route, headers=headers), patch("app.subprocess.Popen") as launch:
                    status, body = self.request("POST", route, payload, headers=headers, authorized=False)
                    self.assertEqual(status, 403, body)
                    launch.assert_not_called()

    def test_delete_and_restore_change_search_results_without_rescanning(self):
        _, photos = self.library(("alice-first.png", "alice-second.png"))
        # The generated pair has different pixels; replace with an actual
        # exact fixture so recoverable removal follows normal review guards.
        shutil.copyfile(self.root / "alice-first.png", self.root / "alice-second.png")
        state = self.scan()
        ids = {photo["name"]: photo["id"] for photo in state["photos"]}
        group = next(group for group in state["groups"] if group["kind"] == "exact")
        initial = self.search("alice", mode="filename")
        status, deleted = self.request("POST", "/api/delete", {"id": ids["alice-first.png"],
                                       "groupId": group["id"], "keepId": ids["alice-second.png"]})
        self.assertEqual(status, 200, deleted)
        remaining = self.search("alice", mode="filename")
        self.assertEqual([result["name"] for result in remaining["results"]], ["alice-second.png"])
        self.assertGreater(remaining["revision"], initial["revision"])
        with patch("app.scan_folder") as scan:
            status, restored = self.request("POST", "/api/restore", {"id": deleted["trashId"]})
            self.assertEqual(status, 200, restored)
            scan.assert_not_called()
        restored_search = self.search("alice", mode="filename")
        self.assertEqual(restored_search["total"], 2)
        self.assertGreater(restored_search["revision"], remaining["revision"])

    def test_show_file_rejects_unscanned_unknown_changed_missing_and_outside_paths(self):
        with patch("app.subprocess.Popen") as launch:
            status, body = self.request("POST", "/api/show-file", {"id": "unknown"})
            self.assertEqual(status, 409, body)
            _, photos = self.library()
            photo = photos["image.png"]
            status, body = self.request("POST", "/api/show-file", {"id": "unknown"})
            self.assertEqual(status, 404, body)
            photo.path = "../outside.png"
            status, body = self.request("POST", "/api/show-file", {"id": photo.id})
            self.assertEqual(status, 409, body)
            photo.path = "image.png"
            (self.root / "image.png").write_bytes(b"Changed after scanning")
            status, body = self.request("POST", "/api/show-file", {"id": photo.id})
            self.assertEqual(status, 409, body)
            self.assertEqual(self.request("GET", "/api/photo/" + photo.id)[0], 409)
            (self.root / "image.png").unlink()
            status, body = self.request("POST", "/api/show-file", {"id": photo.id})
            self.assertEqual(status, 404, body)
            launch.assert_not_called()

    def test_show_file_uses_fixed_explorer_and_literal_argument_list(self):
        _, photos = self.library(("Photo & Alice.png",))
        photo = photos["Photo & Alice.png"]
        with patch("app.os.name", "nt"), patch("app.subprocess.Popen") as launch:
            status, body = self.request("POST", "/api/show-file", {"id": photo.id})
            self.assertEqual(status, 200, body)
            arguments = launch.call_args.args[0]
            self.assertTrue(arguments[0].lower().endswith("explorer.exe"))
            self.assertEqual(arguments[1], "/select,")
            self.assertEqual(Path(arguments[2]), self.root / "Photo & Alice.png")
            self.assertNotIn("shell", launch.call_args.kwargs)

    def test_close_drops_private_indexes_without_changing_original_files(self):
        _, photos = self.library()
        photo = photos["image.png"]
        before = photo.file.read_bytes()
        photo.ocrText = "Private fixture name"
        photo.visualEmbedding = embedding()
        self.server.app.close()
        self.assertEqual(photo.ocrText, "")
        self.assertIsNone(photo.visualEmbedding)
        self.assertEqual(photo.file.read_bytes(), before)
