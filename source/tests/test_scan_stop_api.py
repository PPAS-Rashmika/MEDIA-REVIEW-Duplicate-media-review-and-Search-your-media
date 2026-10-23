"""Stop-scan HTTP checks use only generated files and synchronized workers.

No assertion depends on a photo decoder's speed or a real media collection.
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import app
import scanner
from tests.test_app import ApiFixture


class ScanStopApiTests(ApiFixture):
    def setUp(self):
        super().setUp()
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        super().tearDown()

    def fixture_files(self):
        contents = {
            "a-first.txt": b"Generated duplicate stop-scan fixture\n",
            "b-second.txt": b"Generated duplicate stop-scan fixture\n",
            "z-unread.txt": b"Generated final unique file\n",
        }
        for name, data in contents.items():
            (self.root / name).write_bytes(data)
        return contents

    def start(self):
        status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
        self.assertEqual(status, 200, body)
        return body

    def stop(self):
        status, body = self.request("POST", "/api/scan/stop", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        return body

    def finished(self):
        worker = self.server.app.scan_thread
        self.assertIsNotNone(worker)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "Generated scan worker did not finish")
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200, state)
        self.assertFalse(state["scanning"], state)
        return state

    def blocked_inspect(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        inspect = scanner._inspect

        def controlled(path, root, *args, **kwargs):
            if path.name == "z-unread.txt":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Generated blocked inspection was not released")
            return inspect(path, root, *args, **kwargs)

        return patch("scanner._inspect", side_effect=controlled), entered, release

    def test_idle_stop_is_idempotent_and_never_discovers_or_opens_a_folder(self):
        before = self.request("GET", "/api/state")[1]
        with patch("app.scan_folder") as scan, patch("app.TrashStore") as trash, \
                patch("app.Organizer") as organize:
            for _ in range(2):
                body = self.stop()
                self.assertFalse(body["accepted"], body)
            scan.assert_not_called()
            trash.assert_not_called()
            organize.assert_not_called()
        after = self.request("GET", "/api/state")[1]
        for key in ("root", "hasScanned", "scanning", "photos", "groups", "errors", "revision"):
            self.assertEqual(after[key], before[key], key)
        self.assertFalse(after["stopRequested"])
        self.assertFalse(after["scanStopped"])
        self.assertIsNone(self.server.app.scan_thread)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_stop_route_requires_current_session_origin_host_and_json(self):
        self.fixture_files()
        blocker, entered, release = self.blocked_inspect()
        with blocker:
            self.start()
            self.assertTrue(entered.wait(3), "Generated scan never reached the blocked file")
            attempts = [
                ({}, 403),
                ({"Origin": self.origin}, 403),
                ({"Origin": self.origin, "X-Review-Token": "expired"}, 403),
                ({"Origin": "https://example.invalid", "X-Review-Token": self.token}, 403),
                ({"Host": "example.invalid", "Origin": self.origin,
                  "X-Review-Token": self.token}, 403),
                ({"Origin": self.origin, "X-Review-Token": self.token,
                  "Content-Type": "text/plain"}, 415),
            ]
            for headers, expected in attempts:
                with self.subTest(headers=headers):
                    status, body = self.request("POST", "/api/scan/stop", {},
                                                headers=headers, authorized=False)
                    self.assertEqual(status, expected, body)
                    state = self.request("GET", "/api/state")[1]
                    self.assertTrue(state["scanning"], state)
                    self.assertFalse(state["stopRequested"], state)
            self.assertEqual(self.request("GET", "/api/scan/stop")[0], 404)
            self.assertFalse(self.request("GET", "/api/state")[1]["stopRequested"])
            release.set()
            self.assertFalse(self.finished()["scanStopped"])

    def test_stop_keeps_complete_exact_pair_search_and_read_only_organizer_preview(self):
        original = self.fixture_files()
        blocker, entered, release = self.blocked_inspect()
        with blocker:
            self.start()
            self.assertTrue(entered.wait(3))
            first = self.stop()
            self.assertTrue(first["accepted"], first)
            second = self.stop()
            self.assertIsInstance(second["accepted"], bool)
            state = self.request("GET", "/api/state")[1]
            self.assertTrue(state["scanning"], state)
            self.assertTrue(state["stopRequested"], state)
            self.assertEqual(state["progress"]["phase"], "stopping")
            # Request handling must remain responsive while the decoder/hash
            # worker is blocked, and another scan must not replace it.
            worker = self.server.app.scan_thread
            status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
            self.assertEqual(status, 409, body)
            self.assertIs(self.server.app.scan_thread, worker)
            status, body = self.request("POST", "/api/search", {"query": "first", "mode": "filename"})
            self.assertEqual(status, 409, body)
            release.set()
            state = self.finished()
        self.assertTrue(state["scanStopped"], state)
        self.assertFalse(state["stopRequested"], state)
        self.assertEqual(state["progress"]["phase"], "stopped")
        self.assertEqual(state["stats"]["total"], 2, state)
        self.assertEqual({photo["name"] for photo in state["photos"]}, {"a-first.txt", "b-second.txt"})
        self.assertEqual(state["errors"], [], "A Stop request is not a corrupt file")
        self.assertEqual(len(state["groups"]), 1, state)
        self.assertEqual(state["groups"][0]["kind"], "exact")
        self.assertEqual(set(state["groups"][0]["photos"]), {photo["id"] for photo in state["photos"]})
        status, found = self.request("POST", "/api/search", {"query": "first", "mode": "filename"})
        self.assertEqual(status, 200, found)
        self.assertEqual([photo["name"] for photo in found["results"]], ["a-first.txt"])
        status, plan = self.request("POST", "/api/organize/plan", {"mode": "type"})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["moveCount"], 2, plan)
        self.assertEqual({item["path"] for item in plan["files"]}, {"a-first.txt", "b-second.txt"})
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)
        self.assertFalse((self.root / "Organized by type").exists())
        self.assertFalse((self.root / ".photo-review-trash").exists())

    def test_stopping_discovery_can_finish_with_empty_safe_library(self):
        original = self.fixture_files()
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        discover = scanner.discover

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Generated discovery was not released")
            return discover(*args, **kwargs)

        with patch("scanner.discover", side_effect=blocked):
            self.start()
            self.assertTrue(entered.wait(3))
            self.stop()
            release.set()
            state = self.finished()
        self.assertTrue(state["scanStopped"], state)
        self.assertEqual(state["photos"], [])
        self.assertEqual(state["groups"], [])
        self.assertEqual(state["errors"], [])
        self.assertEqual(state["stats"]["total"], 0)
        self.assertEqual({name: (self.root / name).read_bytes() for name in original}, original)

    def test_new_scan_resets_stop_and_late_idle_stop_cannot_poison_next_scan(self):
        self.fixture_files()
        blocker, entered, release = self.blocked_inspect()
        with blocker:
            self.start()
            self.assertTrue(entered.wait(3))
            self.stop()
            release.set()
            stopped = self.finished()
        self.assertTrue(stopped["scanStopped"])
        self.assertFalse(self.stop()["accepted"])
        self.start()
        complete = self.finished()
        self.assertFalse(complete["scanStopped"], complete)
        self.assertFalse(complete["stopRequested"], complete)
        self.assertEqual(complete["progress"]["phase"], "complete")
        self.assertEqual(complete["stats"]["total"], 3)
        self.assertGreater(complete["revision"], stopped["revision"])
        # A click delivered after the worker has completed is a harmless no-op.
        self.assertFalse(self.stop()["accepted"])
        after = self.request("GET", "/api/state")[1]
        for key in ("photos", "groups", "errors", "stats", "revision", "scanStopped", "progress"):
            self.assertEqual(after[key], complete[key], key)
        self.start()
        self.assertEqual(self.finished()["stats"]["total"], 3)

    def test_close_never_publishes_late_worker_private_indexes(self):
        self.fixture_files()
        generated = scanner.scan_folder(self.root)
        for photo in generated.photos.values():
            photo.ocrText = "Generated secret OCR text"
            photo.visualEmbedding = (1.0,) + (0.0,) * 511
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Generated late scan was not released")
            return generated

        with patch("app.scan_folder", side_effect=delayed):
            self.start()
            self.assertTrue(entered.wait(3))
            # Closing may wait for cooperative work or return immediately;
            # either way a delayed result must never restore private indexes.
            close_started = threading.Event()
            errors = []

            def close():
                try:
                    self.server.app.close()
                except Exception as error:
                    errors.append(error)

            closer = threading.Thread(target=close, daemon=True)
            stop_event = self.server.app.scan_stop_event
            set_stop = stop_event.set

            def closing_requested_stop():
                set_stop()
                close_started.set()

            # The event is signaled by the real close method. Only then may the
            # synthetic worker finish, avoiding a timing-dependent close race.
            with patch.object(stop_event, "set", side_effect=closing_requested_stop):
                closer.start()
                self.assertTrue(close_started.wait(3))
                self.assertTrue(self.server.app.closing)
                release.set()
                closer.join(timeout=5)
            self.assertFalse(closer.is_alive(), "Close did not finish")
            self.assertEqual(errors, [])
            self.finished()
        self.assertTrue(self.server.app.closing)
        for photo in self.server.app.result.photos.values():
            self.assertEqual(photo.ocrText, "")
            self.assertIsNone(photo.visualEmbedding)
        self.assertNotIn("Generated secret OCR", str(self.request("GET", "/api/state")[1]))
        status, body = self.request("POST", "/api/scan", {"path": str(self.root)})
        self.assertEqual(status, 409, body)
