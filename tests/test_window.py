"""The desktop window: Resolv opens in its own window when Edge or Chrome is there, else in a browser tab."""
import os
import unittest
from unittest import mock

from ayos import __main__ as start


class DesktopWindow(unittest.TestCase):
    def test_finds_edge_in_program_files(self):
        env = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files"}
        edge = os.path.join(r"C:\Program Files (x86)", r"Microsoft\Edge\Application\msedge.exe")
        self.assertEqual(start.find_app_browser(env, exists=lambda p: p == edge, which=lambda n: None), edge)

    def test_prefers_edge_over_chrome_and_handles_none(self):
        env = {"ProgramFiles": r"C:\Program Files", "LocalAppData": r"C:\Users\me\AppData\Local"}
        chrome = os.path.join(r"C:\Users\me\AppData\Local", r"Google\Chrome\Application\chrome.exe")
        self.assertEqual(start.find_app_browser(env, exists=lambda p: p == chrome, which=lambda n: None), chrome)
        self.assertEqual(start.find_app_browser({}, exists=lambda p: False, which=lambda n: None), "")

    def test_opens_its_own_window_with_its_own_profile(self):
        calls = []
        with mock.patch.object(start, "find_app_browser", return_value="msedge.exe"), mock.patch.object(start.webbrowser, "open") as tab:
            used = start.open_window("http://127.0.0.1:8020/", "data", popen=lambda cmd, **kw: calls.append(cmd))
        self.assertEqual(used, "window (direct)")
        self.assertIn("--app=http://127.0.0.1:8020/", calls[0])
        self.assertTrue(any(a.startswith("--user-data-dir=") and a.endswith("window") for a in calls[0]))
        tab.assert_not_called()

    def test_falls_back_to_a_browser_tab(self):
        def broken(cmd, **kw):
            raise OSError("cannot start")

        with mock.patch.object(start, "find_app_browser", return_value="msedge.exe"), mock.patch.object(start.webbrowser, "open") as tab:
            self.assertEqual(start.open_window("http://x/", "data", popen=broken), "browser")
        tab.assert_called_once_with("http://x/")
        with mock.patch.object(start, "find_app_browser", return_value=""), mock.patch.object(start.webbrowser, "open") as tab:
            self.assertEqual(start.open_window("http://x/", "data"), "browser")
        with mock.patch.object(start, "find_app_browser", return_value="msedge.exe"), mock.patch.object(start.webbrowser, "open") as tab:
            self.assertEqual(start.open_window("http://x/", "data", plain_browser=True), "browser")

    def test_command_file_survives_awkward_folder_names(self):
        import tempfile

        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "Ayos main (1) 100%", "data")
            with mock.patch.object(start, "find_app_browser", return_value=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"), \
                    mock.patch.object(start.os, "name", "nt"), mock.patch.object(start.webbrowser, "open"):
                used = start.open_window("http://127.0.0.1:8020/", data, elevated=True, loaded=lambda: True, popen=lambda cmd, **kw: calls.append(cmd))
            self.assertEqual(used, "window")
            self.assertEqual(calls[0][0], "explorer.exe")
            with open(calls[0][1], "r", encoding="utf-8") as f:
                text = f.read()
            self.assertIn('start "" "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe" "--app=http://127.0.0.1:8020/"', text)
            self.assertIn("100%%", text)  # a percent sign must be doubled inside a command file

    def test_starts_it_directly_when_explorer_does_not_show_it(self):
        import tempfile

        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(start, "find_app_browser", return_value="msedge.exe"), mock.patch.object(start.os, "name", "nt"), \
                    mock.patch.object(start.webbrowser, "open"):
                used = start.open_window("http://x/", tmp, elevated=True, loaded=lambda: False, popen=lambda cmd, **kw: calls.append(cmd), wait=0.3)
        self.assertEqual(used, "window (direct)")
        self.assertEqual([c[0] for c in calls], ["explorer.exe", "msedge.exe"])


if __name__ == "__main__":
    unittest.main()
