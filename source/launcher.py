"""Single-window Windows entry point for local media review.

The review UI runs inside WebView2. A private loopback server connects that UI
to the existing media backend; the system browser is never launched.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR / "vendor"))
WINDOW_TITLE = "Media Review"
MUTEX_NAME = r"Local\PhotoReview.SingleDesktopWindow.v2"


def default_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR.parent


class SingleInstance:
    """Keep repeated double-clicks on the existing desktop window."""

    def __init__(self, title=WINDOW_TITLE):
        self.title = title
        self.handle = None
        self.kernel32 = None

    def try_acquire(self) -> bool:
        if os.name != "nt":
            return True
        from ctypes import wintypes

        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        self.handle = self.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

    def activate_existing(self) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        # A second click can arrive while WebView2 is still starting.
        until = time.monotonic() + 5
        while time.monotonic() < until:
            existing = user32.FindWindowW(None, self.title)
            if existing:
                user32.ShowWindow(existing, 9)  # SW_RESTORE
                user32.SetForegroundWindow(existing)
                return True
            time.sleep(0.1)
        return False

    def close(self) -> None:
        if self.handle and self.kernel32:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


class DesktopSession:
    """Own one embedded window, its server, and its disposable previews."""

    def __init__(self, root_path=None, create_server_func=None):
        self.root_path = Path(root_path) if root_path is not None else default_root()
        self.create_server_func = create_server_func
        self.server = None
        self.server_thread = None
        self.url = None
        self.window = None
        self.webview = None
        self.closed = False
        self._close_lock = threading.Lock()
        self._picker_lock = threading.Lock()

    def start_server(self):
        if self.closed:
            raise RuntimeError("This desktop session has already closed.")
        if self.server is not None:
            return self.server
        factory = self.create_server_func
        if factory is None:
            from app import create_server

            factory = create_server
        # An available port prevents an old browser edition from conflicting
        # with the new desktop edition. Merely constructing the app never scans.
        self.server = factory(self.root_path, port=0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
            daemon=True, name="media-review-server",
        )
        self.server_thread.start()
        return self.server

    def create_window(self, webview_module):
        if self.window is not None:
            return self.window
        self.webview = webview_module
        self.start_server()
        self.webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
        self.webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        self.webview.settings["ALLOW_DOWNLOADS"] = False
        self.webview.settings["ALLOW_FILE_URLS"] = False
        self.window = self.webview.create_window(
            WINDOW_TITLE, url=self.url, width=1340, height=900,
            min_size=(1040, 700), resizable=True, text_select=True,
            background_color="#151c21", confirm_close=False,
        )
        # The existing HTTP route retains all token and origin checks. Replace
        # only its picker implementation so no hidden Tk control window exists.
        self.server.app.pick_folder = self.pick_folder
        self.window.events.closed += self.close
        return self.window

    def pick_folder(self) -> dict:
        from app import ReviewError

        if self.closed or self.window is None:
            raise ReviewError("The media review window is unavailable. Open the app again.", 409)
        with self.server.app.lock:
            if self.server.app.scanning:
                raise ReviewError("Wait for the scan to finish before choosing another folder.", 409)
            directory = str(self.server.app.root or self.server.app.picker_root)
        if not self._picker_lock.acquire(blocking=False):
            raise ReviewError("A folder chooser is already open.", 409)
        try:
            result = []

            def choose():
                result.append(self.window.create_file_dialog(
                    self.webview.FileDialog.FOLDER, directory=directory, allow_multiple=False,
                ))

            # HTTP requests arrive on background threads. WinForms folder
            # dialogs must execute on the owning STA thread, including COM.
            native = getattr(self.window, "native", None)
            if native is not None and getattr(native, "InvokeRequired", False):
                from System import Action

                native.Invoke(Action(choose))
            else:
                choose()
            selected = result[0] if result else None
            path = str(selected[0]) if selected else ""
            # Return the selection to the UI only. Scan is a separate explicit
            # action and choosing/cancelling a folder never changes app state.
            return {"path": path, "cancelled": not bool(path)}
        except ReviewError:
            raise
        except Exception as error:
            raise ReviewError(f"Could not open the folder chooser. Paste a folder path instead. {error}") from error
        finally:
            self._picker_lock.release()

    def run(self, webview_module=None, loaded_callback=None) -> None:
        try:
            if webview_module is None:
                import webview as webview_module

            window = self.create_window(webview_module)
            if loaded_callback is not None:
                window.events.loaded += loaded_callback
            icon = APP_DIR / "app.ico"
            webview_module.start(
                gui="edgechromium", debug=False, private_mode=True,
                icon=str(icon) if icon.is_file() else None,
            )
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            if self.server is not None:
                try:
                    if self.server_thread is not None and self.server_thread.is_alive():
                        self.server.shutdown()
                        self.server_thread.join(timeout=3)
                finally:
                    try:
                        self.server.server_close()
                    finally:
                        self.server.app.close()


def self_test(report_path=None) -> None:
    """Verify the packaged runtime using generated media only, without UI."""
    import subprocess
    import tempfile
    import clr  # noqa: F401: verifies Python.NET and the Windows .NET runtime
    import webview
    from PIL import Image, ImageDraw, ImageFont
    from scanner import HEIF_AVAILABLE, oriented_image, scan_folder
    from app import create_server
    from image_filters import classify_image, ocr_capabilities
    import visual_search
    import winrt.windows.storage.streams  # verifies the dynamically loaded bitmap buffer namespace

    test_parent = report_path.parent if report_path else None
    with tempfile.TemporaryDirectory(prefix="photo-review-selftest-", dir=test_parent) as folder:
        sample = Path(folder) / "sample.png"
        Image.new("RGB", (48, 32), "orange").save(sample)
        result = scan_folder(folder)
        server = create_server(folder, port=0)
        try:
            initial = server.app.state()
            assert not initial["scanning"] and not initial.get("hasScanned", True)
            assert initial["root"] == "" and initial["stats"]["total"] == 0
        finally:
            server.server_close()
            server.app.close()
        assert len(result.photos) == 1
        assert oriented_image(sample).size == (48, 32)
        assert HEIF_AVAILABLE, "HEIC decoder missing from executable"
        text_sample = Image.new("RGB", (720, 240), "white")
        ImageDraw.Draw(text_sample).multiline_text((24, 30), "MEDIA REVIEW SAMPLE\nLOCAL IMAGE TEXT FILTER",
                                                   font=ImageFont.load_default(size=34), fill="black", spacing=18)
        text_sample_path = Path(folder) / "Screenshot sample.png"
        text_sample.save(text_sample_path)
        categories, _, text_status = classify_image(text_sample_path, text_sample)
        assert "screenshot" in categories, "Screenshot labeling is unavailable"
        ocr = ocr_capabilities()
        if ocr["available"]:
            assert text_status == "detected" and "text" in categories, "Local text detection failed"
        visual = visual_search.capabilities()
        assert visual["available"], visual["reason"]
        query_vector = visual_search.embed_text("an orange square")
        assert query_vector is not None and len(query_vector) == 512, "Local visual query inference failed"
        assert result.photos[next(iter(result.photos))].visualStatus == "indexed", "Local image inference failed"
        search_server = create_server(folder, port=0)
        try:
            search_server.app.start_scan(folder)
            search_server.app.scan_thread.join(timeout=60)
            assert not search_server.app.scanning, "Generated content scan timed out"
            filename_matches = search_server.app.search("sample.png", mode="filename")
            assert filename_matches["total"] >= 1, "Unique file name search failed"
            text_matches = search_server.app.search("MEDIA REVIEW", mode="text")
            if ocr["available"]:
                assert text_matches["total"] == 1, "Packaged OCR content search failed"
            visual_matches = search_server.app.search("an orange square", mode="visual")
            assert visual_matches["visualAvailable"] and visual_matches["visualIndexedImages"] == 2, "Packaged visual search failed"
        finally:
            search_server.server_close()
            search_server.app.close()
        decoder = APP_DIR / "bin" / "ffmpeg.exe"
        assert decoder.is_file(), "Media decoder missing from executable"
        version = subprocess.run(
            [str(decoder), "-version"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert version.returncode == 0
        view_dir = Path(webview.__file__).resolve().parent
        assert (view_dir / "lib" / "Microsoft.Web.WebView2.Core.dll").is_file(), "Embedded browser DLL missing"
        assert (view_dir / "lib" / "Microsoft.Web.WebView2.WinForms.dll").is_file(), "Native window DLL missing"
        assert (view_dir / "js" / "api.js").is_file(), "Embedded browser scripts missing"
        report = {
            "ok": True, "manualScan": True, "heic": HEIF_AVAILABLE,
            "mediaDecoder": version.stdout.splitlines()[0], "photos": len(result.photos),
            "static": (APP_DIR / "static" / "index.html").is_file(), "desktopRuntime": True,
            "imageFilters": True, "textDetection": text_status == "detected", "ocrLanguage": ocr["language"],
            "textSearch": bool(text_matches["total"]), "uniqueFileSearch": True,
            "visualSearch": visual_matches["visualAvailable"], "visualIndexedImages": visual_matches["visualIndexedImages"],
            "visualModel": visual["model"], "embeddingSize": len(query_vector),
        }
        if report_path:
            report_path.write_text(json.dumps(report), encoding="utf-8")


def desktop_check(session, report_path) -> dict:
    """Open the real embedded runtime, verify its empty UI, then close it."""
    outcome = {}
    finished = threading.Event()

    def loaded():
        try:
            # run_js uses WebView2's direct ExecuteScriptAsync and does not
            # relax the page CSP or use the eval-based evaluate_js wrapper.
            until = time.monotonic() + 15
            ui = None
            while time.monotonic() < until:
                ui = session.window.run_js("""({
                    title: document.title,
                    ready: document.readyState,
                    folder: document.getElementById('folder-path')?.value,
                    scanDisabled: document.getElementById('scan-button')?.disabled,
                    chooseFolder: !!document.getElementById('browse-button'),
                    bulkButton: !!document.getElementById('bulk-delete-button'),
                    bulkDisabled: document.getElementById('bulk-delete-button')?.disabled,
                    bulkDialog: !!document.getElementById('bulk-delete-dialog'),
                    imageCategoryFilter: !!document.getElementById('image-category-filter'),
                    imageCategory: document.getElementById('image-category-filter')?.value,
                    searchLibrary: !!document.getElementById('library-query'),
                    searchDisabled: document.getElementById('library-search-button')?.disabled,
                    searchMode: document.getElementById('library-mode')?.value,
                    errorVisible: document.getElementById('error-banner') ? !document.getElementById('error-banner').hidden : true
                })""")
                if (isinstance(ui, dict) and ui.get("chooseFolder") and ui.get("scanDisabled")
                        and ui.get("bulkButton") and ui.get("bulkDisabled") and ui.get("bulkDialog")
                        and ui.get("imageCategoryFilter") and ui.get("imageCategory") == "all"
                        and not ui.get("errorVisible")):
                    break
                time.sleep(0.1)
            assert isinstance(ui, dict), "The embedded browser did not render the interface."
            assert ui["chooseFolder"] and ui["scanDisabled"] and not ui.get("folder"), ui
            assert ui["bulkButton"] and ui["bulkDisabled"] and ui["bulkDialog"], ui
            assert ui["imageCategoryFilter"] and ui["imageCategory"] == "all", ui
            assert ui["searchLibrary"] and ui["searchDisabled"] and ui["searchMode"] == "all", ui
            assert not ui["errorVisible"], ui
            state = session.server.app.state()
            assert state["root"] == "" and not state["hasScanned"] and not state["scanning"]
            assert state["stats"]["total"] == 0 and not state["photos"]
            renderer = session.window.gui.renderer
            assert renderer == "edgechromium", f"Unexpected browser engine: {renderer}"
            assert len(session.webview.windows) == 1, "More than one app window was created."
            outcome.update({
                "ok": True, "desktop": True, "renderer": renderer,
                "windowTitle": session.window.title, "windows": 1,
                "manualScan": True, "total": 0, "ui": ui,
            })
        except Exception as error:
            outcome.update({"ok": False, "error": str(error)})
        finally:
            finished.set()
            if report_path:
                report_path.write_text(json.dumps(outcome), encoding="utf-8")
            session.window.destroy()

    def timeout():
        if not finished.wait(40):
            outcome.update({"ok": False, "error": "The embedded browser did not become ready within 40 seconds."})
            if report_path:
                report_path.write_text(json.dumps(outcome), encoding="utf-8")
            if session.window:
                session.window.destroy()

    threading.Thread(target=timeout, name="desktop-check-timeout", daemon=True).start()
    session.run(loaded_callback=loaded)
    if not outcome.get("ok"):
        raise RuntimeError(outcome.get("error", "The embedded desktop window was closed before it was ready."))
    outcome["serverClosed"] = session.server.fileno() == -1
    if report_path:
        report_path.write_text(json.dumps(outcome), encoding="utf-8")
    return outcome


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Compare local media in one desktop window.")
    parser.add_argument("--self-test", action="store_true", help="Verify generated fixtures and bundled dependencies without opening UI")
    parser.add_argument("--desktop-check", action="store_true", help="Verify the real desktop window and close it automatically")
    parser.add_argument("--report", type=Path, help="Save a verification report")
    parser.add_argument("--root", type=Path, help="Initial folder chooser location only; does not scan")
    args = parser.parse_args(argv)
    report_path = args.report.resolve() if args.report else None
    if args.self_test:
        self_test(report_path)
        return
    instance = SingleInstance()
    try:
        if not instance.try_acquire():
            instance.activate_existing()
            return
        session = DesktopSession(args.root)
        if args.desktop_check:
            desktop_check(session, report_path)
        else:
            session.run()
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        import traceback

        import tempfile

        error_dir = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "MediaReview" / "logs"
        error_path = error_dir / "media-review-error.log"
        try:
            error_dir.mkdir(parents=True, exist_ok=True)
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        if "--self-test" not in sys.argv and "--desktop-check" not in sys.argv:
            if os.name == "nt":
                ctypes.windll.user32.MessageBoxW(
                    None, f"Media Review could not start.\n\n{error}\n\nDetails: {error_path}",
                    WINDOW_TITLE, 0x10,
                )
        raise SystemExit(1)
