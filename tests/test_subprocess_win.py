"""Child processes must not flash a console window on screen.

Reported live: while the bot sat quietly in the tray, asking "你能看到这个
视频吗" made four black windows blink on screen.  The packaged build is
``console=False`` (``build.spec``), and Windows then gives every console
child a brand-new console — one for the duration probe plus one per frame.

The guard is a keyword dict splatted into ``subprocess.run``; these tests
pin down its shape on Windows and its absence elsewhere, so no caller has
to branch on the platform itself.
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.subprocess_win import hidden_kwargs


class HiddenWindowTests(unittest.TestCase):
    def test_windows_hides_both_the_console_and_the_window(self):
        with patch("src.utils.subprocess_win.os.name", "nt"):
            kwargs = hidden_kwargs()
        # A console-less parent otherwise gets a fresh console per child.
        self.assertEqual(
            kwargs.get("creationflags"),
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        info = kwargs.get("startupinfo")
        self.assertIsNotNone(info)
        self.assertTrue(info.dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(info.wShowWindow, subprocess.SW_HIDE)

    def test_other_platforms_get_no_extra_keywords(self):
        with patch("src.utils.subprocess_win.os.name", "posix"):
            self.assertEqual(hidden_kwargs(), {})

    def test_missing_attributes_degrade_to_fewer_keywords(self):
        """An interpreter without these constants must not raise."""
        with patch("src.utils.subprocess_win.os.name", "nt"), \
             patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             patch.object(subprocess, "STARTUPINFO", None, create=True):
            self.assertEqual(hidden_kwargs(), {})

    def test_callers_can_splat_it_into_subprocess_run(self):
        """The dict must be valid kwargs, not just plausible-looking names."""
        with patch("src.utils.subprocess_win.os.name", "nt"):
            kwargs = hidden_kwargs()
        completed = subprocess.run(
            [sys.executable, "-c", "print('ok')"],
            capture_output=True, text=True, timeout=60, **kwargs,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
