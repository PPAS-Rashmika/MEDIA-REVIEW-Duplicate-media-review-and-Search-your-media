"""Desktop lifecycle tests; no native UI or user media is opened.

Generated fixture folders exercise the real loopback backend while a small
webview double records native-window and folder-dialog calls.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "vendor"))

import launcher
from app import ReviewError, create_server


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeWindow:
    def __init__(self):
        self.events = SimpleNamespace(closed=FakeEvent(), closing=FakeEvent(), loaded=FakeEvent())
        self.create_file_dialog = Mock(return_value=None)
        self.restore = Mock()
        self.show = Mock()
        self.focus = Mock()
        self.destroy = Mock()


class FakeWebview:
    def __init__(self):
        self.FileDialog = SimpleNamespace(FOLDER=object())
        self.FOLDER_DIALOG = self.FileDialog.FOLDER
        self.settings = {}
        self.windows = []
        self.create_window = Mock(side_effect=self._create_window)
        self.start = Mock(side_effect=self._start)

    def _create_window(self, *args, **kwargs):
        window = FakeWindow()
        self.windows.append(window)
        return window

    def _start(self, *args, **kwargs):
        # Native start blocks until the user's window closes. Emulate that end
        # of the GUI lifecycle, including events emitted by pywebview.
        for window in self.windows:
            window.events.loaded.fire()
            window.events.closed.fire()


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photo-review-desktop-", dir=APP_ROOT / "tests")
        self.root = Path(self.temporary.name)
        self.session = launcher.DesktopSession(self.root)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.session.close)
        self.webview = FakeWebview()

    def request(self, method, path, body=None):
        port = self.session.server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers = {"Content-Type": "application/json", "Origin": self.session.url,
                       "X-Review-Token": self.session.server.app.token}
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assertServerClosed(self, server, thread):
        self.assertFalse(thread.is_alive(), "Desktop close must stop the backend thread")
        self.assertEqual(server.fileno(), -1, "Desktop close must release its listening socket")

    def test_start_uses_loopback_and_available_port_without_reading_media(self):
        (self.root / "arbitrary.media").write_bytes(b"These fixture bytes must not be scanned at startup")
        server_factory = Mock(wraps=create_server)
        self.session = launcher.DesktopSession(self.root, create_server_func=server_factory)
        self.addCleanup(self.session.close)
        with patch("app.scan_folder") as scan:
            first = self.session.start_server()
            self.assertIs(self.session.start_server(), first)
            status, state = self.request("GET", "/api/state")
            scan.assert_not_called()
        self.assertEqual(status, 200)
        server_factory.assert_called_once()
        self.assertEqual(server_factory.call_args.kwargs.get("port"), 0)
        self.assertEqual(first.server_address[0], "127.0.0.1")
        self.assertGreater(first.server_address[1], 0)
        self.assertEqual(self.session.url, "http://127.0.0.1:%d" % first.server_address[1])
        self.assertFalse(state["scanning"])
        self.assertFalse(state["hasScanned"])
        self.assertEqual(state["root"], "")
        self.assertEqual(state["photos"], [])
        self.assertEqual(state["stats"]["total"], 0)
        self.assertFalse((self.root / ".photo-review-trash").exists())

    def test_run_opens_one_embedded_window_and_never_opens_a_browser_or_tk_controller(self):
        with patch("webbrowser.open", side_effect=AssertionError("External browser opened")) as browser, \
                patch("tkinter.Tk", side_effect=AssertionError("Extra Tk window created")) as tk_root, \
                patch("app.scan_folder") as scan:
            self.session.run(self.webview)
        browser.assert_not_called()
        tk_root.assert_not_called()
        scan.assert_not_called()
        self.webview.create_window.assert_called_once()
        call = self.webview.create_window.call_args
        window_url = call.kwargs.get("url", call.args[1] if len(call.args) > 1 else None)
        self.assertEqual(window_url, self.session.url)
        self.assertTrue(window_url.startswith("http://127.0.0.1:"))
        self.webview.start.assert_called_once()
        self.assertEqual(self.webview.start.call_args.kwargs.get("gui"), "edgechromium")
        self.assertServerClosed(self.session.server, self.session.server_thread)

    def test_native_folder_selection_updates_no_scan_state(self):
        window = self.session.create_window(self.webview)
        window.create_file_dialog.return_value = (str(self.root),)
        before = self.session.server.app.state()
        with patch("app.scan_folder") as scan, patch.object(self.session.server.app, "start_scan") as start_scan, \
                patch("tkinter.Tk", side_effect=AssertionError("Native picker should not use Tk")):
            status, result = self.request("POST", "/api/pick-folder", {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"path": str(self.root), "cancelled": False})
        self.assertEqual(self.session.server.app.state(), before)
        scan.assert_not_called()
        start_scan.assert_not_called()
        dialog = window.create_file_dialog.call_args
        folder_mode = dialog.args[0] if dialog.args else dialog.kwargs.get("dialog_type")
        self.assertIs(folder_mode, self.webview.FileDialog.FOLDER)
        self.webview.create_window.assert_called_once()

    def test_cancel_folder_picker_retains_empty_ready_state(self):
        window = self.session.create_window(self.webview)
        for cancelled in (None, (), []):
            with self.subTest(cancelled=cancelled):
                window.create_file_dialog.return_value = cancelled
                self.assertEqual(self.session.pick_folder(), {"path": "", "cancelled": True})
                state = self.session.server.app.state()
                self.assertEqual(state["root"], "")
                self.assertFalse(state["hasScanned"])
                self.assertFalse(state["scanning"])

    def test_busy_scan_blocks_native_folder_dialog(self):
        window = self.session.create_window(self.webview)
        self.session.server.app.scanning = True
        try:
            with self.assertRaises(ReviewError) as caught:
                self.session.pick_folder()
            self.assertEqual(caught.exception.status, 409)
            window.create_file_dialog.assert_not_called()
        finally:
            self.session.server.app.scanning = False

    def test_window_close_stops_server_and_removes_generated_preview_cache(self):
        window = self.session.create_window(self.webview)
        server, thread = self.session.server, self.session.server_thread
        server.app.media_directory = tempfile.TemporaryDirectory(prefix="preview-", dir=self.root)
        cache = Path(server.app.media_directory.name)
        (cache / "preview.mp4").write_bytes(b"Temporary fixture preview")
        server.app.media_cache["fixture"] = cache / "preview.mp4"
        with patch.object(server, "shutdown", wraps=server.shutdown) as shutdown, \
                patch.object(server, "server_close", wraps=server.server_close) as close_server, \
                patch.object(server.app, "close", wraps=server.app.close) as close_app:
            window.events.closed.fire()
            self.session.close()
        shutdown.assert_called_once()
        close_server.assert_called_once()
        close_app.assert_called_once()
        self.assertServerClosed(server, thread)
        self.assertFalse(cache.exists())
        self.assertIsNone(server.app.media_directory)
        self.assertEqual(server.app.media_cache, {})

    def test_missing_renderer_cleans_backend_without_browser_fallback(self):
        self.webview.start.side_effect = RuntimeError("WebView2 runtime unavailable")
        with patch("webbrowser.open", side_effect=AssertionError("External browser fallback")) as browser:
            with self.assertRaisesRegex(RuntimeError, "WebView2"):
                self.session.run(self.webview)
        browser.assert_not_called()
        self.assertServerClosed(self.session.server, self.session.server_thread)

    def test_window_construction_error_cleans_backend_without_browser_fallback(self):
        self.webview.create_window.side_effect = RuntimeError("Could not create native window")
        with patch("webbrowser.open", side_effect=AssertionError("External browser fallback")) as browser:
            with self.assertRaisesRegex(RuntimeError, "native window"):
                self.session.run(self.webview)
        browser.assert_not_called()
        self.assertServerClosed(self.session.server, self.session.server_thread)

    def test_duplicate_launch_focuses_existing_window_without_server_or_extra_window(self):
        existing = Mock()
        existing.try_acquire.return_value = False
        with patch.object(launcher, "SingleInstance", return_value=existing), \
                patch.object(launcher, "DesktopSession") as session_class, \
                patch.object(sys, "argv", ["Photo Review.exe"]), \
                patch("webbrowser.open", side_effect=AssertionError("Duplicate launch opened browser")) as browser, \
                patch("tkinter.Tk", side_effect=AssertionError("Duplicate launch opened Tk")) as tk_root:
            launcher.main()
        existing.activate_existing.assert_called_once()
        existing.close.assert_called_once()
        session_class.assert_not_called()
        browser.assert_not_called()
        tk_root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
