"""Spawn a child process without flashing a console window.

Why this exists: the packaged bot is built with ``console=False`` (see
``build.spec``), so the process owns no console.  Windows answers that by
giving *every* console child — ``ffmpeg.exe`` here — a brand-new console
window of its own.  Sampling a shared video runs one probe plus one
extraction per frame, so a single "你能看到这个视频吗" used to pop four
black windows on screen while the bot was supposedly running silently in
the background.

``CREATE_NO_WINDOW`` tells the loader to start the child without a console
at all; the ``STARTUPINFO`` twin (``STARTF_USESHOWWINDOW`` + ``SW_HIDE``)
is belt and braces for the cases where a window would still be created
(some third-party launchers ignore the creation flag).  Both together are
what "silent" means for a Windows subprocess.

Everything is a no-op off Windows, so callers need no platform branch:

    subprocess.run(cmd, capture_output=True, timeout=30, **hidden_kwargs())

This module has no side effects and no dependencies beyond the stdlib.
"""

import os
import subprocess
from typing import Any, Dict


def hidden_kwargs() -> Dict[str, Any]:
    """Keyword arguments that keep a child process from showing a window.

    Returns:
        A dict to splat into ``subprocess.run``/``Popen``.  Empty off
        Windows (there is nothing to hide), and never raises: a platform
        that lacks one of the attributes simply gets fewer of them.
    """
    if os.name != "nt":
        return {}

    kwargs: Dict[str, Any] = {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        try:
            info = startupinfo_cls()
            info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            info.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = info
        except Exception:  # pragma: no cover - exotic interpreter/platform
            pass

    return kwargs
