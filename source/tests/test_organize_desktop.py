"""Closing a desktop window must not interrupt a background file move."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import launcher


class OrganizeDesktopTests(unittest.TestCase):
    def session(self, running=False):
        session = launcher.DesktopSession()
        session.server = SimpleNamespace(app=SimpleNamespace(state=Mock(return_value={
            "organization": {"running": running}})))
        return session

    def test_running_move_or_undo_prevents_native_close(self):
        session = self.session(True)
        message = Mock()
        windows = SimpleNamespace(user32=SimpleNamespace(MessageBoxW=message))
        with patch("launcher.os.name", "nt"), patch.object(launcher.ctypes, "windll", windows, create=True):
            self.assertFalse(session.may_close())
        message.assert_called_once()
        self.assertIn("wait", message.call_args.args[1].lower())
        self.assertFalse(session.closed)

    def test_finished_or_idle_operation_allows_close(self):
        session = self.session(False)
        self.assertTrue(session.may_close())
        session.server.app.state.return_value = {"organization": {"running": True}}
        with patch("launcher.os.name", "posix"):
            self.assertFalse(session.may_close())
        session.server.app.state.return_value = {"organization": {"running": False}}
        self.assertTrue(session.may_close())

    def test_uninitialized_desktop_allows_close(self):
        self.assertTrue(launcher.DesktopSession().may_close())
