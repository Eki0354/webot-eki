"""Render a page in a real browser so scripts have already run.

Why this exists: :mod:`src.media.link_fetcher` reads pages with ``urllib``,
which returns the bytes the server sent.  Sites that build their player in
JavaScript — Bilibili, YouTube and most modern video pages — send an empty
shell and inject the ``<video src=...>`` afterwards, so the media address is
simply not in the HTML we parsed.  The note's *copy* may also be painted by
scripting, leaving nothing to read.

A headless Chromium can execute that script and then hand us the finished
DOM.  Measured on this machine: a page whose title is set by ``<script>``
came back with the scripted title, and the ``<video>`` element the script
created was in the output.

Design constraints, all load-bearing:

* **No new dependency.**  The runtime is whatever Chromium the machine
  already has — Edge ships with Windows 10/11, Chrome is common on both.
  Nothing is downloaded, nothing is bundled, and the EXE does not grow.
* **Optional at runtime.**  With no browser installed :func:`available` is
  False and the caller keeps its ``urllib`` behaviour.
* **Never a window.**  Headless still spawns a real process; it inherits the
  same ``CREATE_NO_WINDOW`` treatment as ffmpeg (see
  :mod:`src.utils.subprocess_win`), because a console flashing up is exactly
  what this bot is supposed to avoid.
* **Bounded.**  A fresh throwaway profile, a hard timeout, a virtual time
  budget so a page that polls forever still returns, and a cap on the DOM
  text handed back.
* **The caller owns the SSRF check.**  This module renders whatever URL it
  is given; :func:`render_html` refuses non-HTTP(S) input but deliberately
  does not resolve hosts, because the caller already vetted the URL and
  re-resolving here would introduce a second, differently-timed lookup.
  Note the honest limitation: once a page runs, *its* scripts can fetch
  same-origin or cross-origin resources, and the browser's network stack is
  not filtered by :func:`~src.media.link_fetcher.is_public_url`.  Rendering
  is therefore only offered for URLs that already passed that check, and any
  media address extracted from the result is vetted again.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Optional

from ..utils.subprocess_win import hidden_kwargs

logger = logging.getLogger(__name__)

#: Wall clock for one render.  A cold browser start plus script execution is
#: a couple of seconds; this is the ceiling, not the expectation.
_RENDER_TIMEOUT_SEC = 12

#: Let the page's timers run this long before dumping the DOM.  Long enough
#: for a player bootstrap or an XHR that fills in the body, short enough that
#: a polling page cannot hold the reply path.
_VIRTUAL_TIME_BUDGET_MS = 2500

#: Script-driven pages can be large; the extractor only needs prose, so the
#: tail beyond this is dropped rather than held in memory.
_MAX_DOM_BYTES = 4 * 1024 * 1024

#: Switches common to every Chromium flavour.  These keep the run quiet and
#: independent of the user's own profile — a shared profile would attach to
#: an already-running instance and return nothing.  ``--dump-dom`` and the
#: headless flag are added by the caller, because the legacy fallback needs a
#: different spelling of the latter.
_BASE_ARGS = (
    "--disable-gpu",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
)

_browser_cache: Optional[str] = None
_browser_checked = False


def _windows_candidates() -> list[Path]:
    """Installed Edge/Chrome locations on Windows, Edge first.

    Edge is preferred on purpose: it is present on every Windows 10/11
    install, so a build that ships without a browser requirement still works.
    """
    candidates: list[Path] = []
    env = os.environ
    roots = [
        env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
        env.get("ProgramFiles") or r"C:\Program Files",
        env.get("LOCALAPPDATA") or "",
    ]
    relatives = (
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("Google", "Chrome", "Application", "chrome.exe"),
    )
    for root in roots:
        if not root:
            continue
        for relative in relatives:
            candidates.append(Path(root).joinpath(*relative))
    return candidates


def _posix_candidates() -> list[Path]:
    """Browser locations on macOS/Linux, plus anything on ``PATH``."""
    candidates = [
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/usr/bin/microsoft-edge"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return candidates


def _candidate_browsers() -> list[Path]:
    """Places a Chromium has been known to live, override first."""
    candidates: list[Path] = []
    override = os.environ.get("WEBOT_BROWSER_PATH", "").strip()
    if override:
        candidates.append(Path(override))
    if os.name == "nt":
        candidates.extend(_windows_candidates())
    else:
        candidates.extend(_posix_candidates())
    return candidates


def browser_path() -> Optional[str]:
    """Absolute path to a usable Chromium, or None when there is none.

    Cached after the first call: locating it walks a handful of fixed paths
    and reads an environment variable, none of which change mid-run.
    """
    global _browser_cache, _browser_checked
    if _browser_checked:
        return _browser_cache
    _browser_checked = True
    for candidate in _candidate_browsers():
        try:
            if candidate.is_file():
                _browser_cache = str(candidate)
                break
        except OSError:  # unreadable path, odd drive, …
            continue
    if _browser_cache is None:
        logger.info(
            "Page rendering unavailable: no Edge/Chrome found (looked in %s)",
            ", ".join(str(c) for c in _candidate_browsers()),
        )
    else:
        logger.debug("Page rendering: using %s", _browser_cache)
    return _browser_cache


def reset_browser_cache() -> None:
    """Forget the located browser.  Only for tests that change the search path."""
    global _browser_cache, _browser_checked
    _browser_cache = None
    _browser_checked = False


def available() -> bool:
    """True when a page can be rendered at all."""
    return browser_path() is not None


def _run_browser(exe: str, args: list, profile: Path, timeout: float):
    """Start the browser once.  Returns ``(stdout, returncode)`` or None."""
    command = [exe, *args, f"--user-data-dir={profile}"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            # Headless still creates a console child; without this one black
            # window appears per render.
            **hidden_kwargs(),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Page render timed out after %.0fs (browser kept running?)", timeout,
        )
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Page render failed: %s: %s", type(exc).__name__, exc)
        return None
    return proc.stdout, proc.returncode


def render_html(
    url: str,
    *,
    timeout: float = _RENDER_TIMEOUT_SEC,
    budget_ms: int = _VIRTUAL_TIME_BUDGET_MS,
) -> Optional[str]:
    """Return the page's DOM after its scripts have run, or None.

    Args:
        url: Page to render.  Must already have passed
            :func:`~src.media.link_fetcher.is_public_url`; this function only
            checks the scheme.
        timeout: Hard wall clock for the browser process.
        budget_ms: Virtual time the page is allowed to advance before the
            DOM is dumped.

    Returns:
        The rendered HTML, capped at :data:`_MAX_DOM_BYTES`, or None when no
        browser is installed, the run failed, or nothing was printed.
    """
    exe = browser_path()
    if not exe:
        return None
    if urllib.parse.urlsplit(url or "").scheme.lower() not in ("http", "https"):
        logger.info("Page render skipped: not an HTTP(S) URL (%s)", url[:80])
        return None

    profile = Path(tempfile.mkdtemp(prefix="webot_render_"))
    try:
        args = [
            "--headless=new",
            *[a for a in _BASE_ARGS if not a.startswith("--dump-dom")],
            f"--virtual-time-budget={max(0, int(budget_ms))}",
            "--dump-dom",
            url,
        ]
        result = _run_browser(exe, args, profile, timeout)

        # Chromium older than 112 rejects ``--headless=new``.  One retry with
        # the legacy spelling costs nothing on the failure path that is the
        # only place it can happen.
        if result is None or (result[1] != 0 and not result[0].strip()):
            args = [
                "--headless",
                *[a for a in _BASE_ARGS if not a.startswith("--dump-dom")],
                f"--virtual-time-budget={max(0, int(budget_ms))}",
                "--dump-dom",
                url,
            ]
            retry = _run_browser(exe, args, profile, timeout)
            if retry is not None and retry[0].strip():
                result = retry
    finally:
        # A Chromium profile is a directory tree with locks; leaking one per
        # rendered page would fill the temp dir.
        shutil.rmtree(profile, ignore_errors=True)

    if result is None:
        return None
    stdout, returncode = result
    dom = stdout.decode("utf-8", errors="replace") if stdout else ""
    if not dom.strip():
        logger.warning(
            "Page render produced no DOM for %s (exit %s)", url[:80], returncode,
        )
        return None
    if len(dom) > _MAX_DOM_BYTES:
        logger.debug(
            "Page render DOM truncated: %d bytes (%s)", len(dom), url[:80],
        )
        dom = dom[:_MAX_DOM_BYTES]
    logger.info("Page rendered: %s (%d bytes of DOM)", url[:80], len(dom))
    return dom
