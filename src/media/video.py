"""Read a shared video's *picture*: find the file, grab a few frames as JPEG.

Why this exists: a group member shares a video and asks "你能看到这个视频吗".
A chat completion receives text only, so the honest answer used to be "看不到".
The note's *copy* is readable from the page (see :mod:`src.media.link_fetcher`),
but the video itself is a separate fetch: the page carries the media address in
its embedded state, and turning that into something a vision model can look at
needs a decoder.

Measured on a Xiaohongshu video share: the mobile page's ``masterUrl`` pointed
at a 1.99 MB, 20 s, 720x1280 H.264 clip; three frames sampled across it came
back at 54-66 KB each and described cleanly by the vision model.

Design constraints, all load-bearing:

* **No new Python dependency.**  Decoding runs ``ffmpeg.exe`` as a child
  process.  PyAV is installed on the dev box but deliberately excluded from
  the packaged EXE (``build.spec``), and a second decoder path would be
  untestable in the build that actually ships.
* **ffmpeg is optional at runtime.**  Without the binary the module reports
  it is unavailable and the caller keeps the text-only behaviour — the same
  trade-off as voice recognition.  Nothing here imports a decoder eagerly.
* **Every byte is untrusted.**  The address comes from a page a stranger
  shared, so it goes through the same SSRF vetting as article links, the
  transfer is capped, and the child process gets a timeout.

This module does network and process I/O but no AI calls and no config reads.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from ..utils.subprocess_win import hidden_kwargs
from .link_fetcher import is_public_url

logger = logging.getLogger(__name__)

#: Frames handed to the vision model.  Three is what "这个视频讲了啥" needs —
#: enough to catch a scene change, cheap enough for any multimodal endpoint.
DEFAULT_FRAME_COUNT = 3

#: Frames are downscaled before upload.  A phone clip is 720x1280 or larger;
#: at _MAX_FRAME_WIDTH the JPEGs are 50-70 KB each and the model loses nothing
#: it would have used.
_MAX_FRAME_WIDTH = 720

#: Cap on the transfer.  A shared clip is seconds to a couple of minutes long;
#: this refuses a mislabelled 500 MB file instead of stalling the reply path.
_MAX_VIDEO_BYTES = 48 * 1024 * 1024

_DOWNLOAD_TIMEOUT_SEC = 20
_PROBE_TIMEOUT_SEC = 15
_FFMPEG_TIMEOUT_SEC = 40

#: A JPEG below this is a placeholder/blank frame, not a picture.
_MIN_FRAME_BYTES = 2048

#: Where the addresses live.  Xiaohongshu puts them in ``masterUrl`` /
#: ``backupUrl`` inside its embedded state; other sites use a ``<video>`` or
#: ``<source>`` tag.  The last pattern is the general "any .mp4 URL in the
#: markup" fallback, which is what catches a player configured by script.
#:
#: Bilibili is the reason the array spellings exist: its ``window.__playinfo__``
#: writes ``"baseUrl": "…m4s"`` and ``"backupUrl": ["…", "…"]``, so a pattern
#: that only accepts ``"backupUrl": "…"`` matches nothing at all there
#: (measured: 0 hits in a 1.5 MB rendered DOM that carried 9 baseUrl and 9
#: backupUrl arrays).
_URL_PATTERNS = (
    re.compile(r'"masterUrl"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'"backupUrl"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    # Arrays: take the first entry, which is the rendition the site plays.
    re.compile(r'"backupUrl"\s*:\s*\[\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'"videoUrl"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    # Bilibili/clone players: snake_case alongside the camelCase spelling.
    re.compile(r'"baseUrl"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'"base_url"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r'<video[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'<source[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I),
    re.compile(r'(https?:(?:\\u002F|\\/|/){2}[^"\'\\\s]+?\.mp4[^"\'\\\s]*)', re.I),
)

#: Ports :func:`~src.media.link_fetcher.is_public_url` will fetch.  Kept in
#: sync by name rather than imported, because the fence that matters is the
#: ordering decision below, not the list itself.
_STANDARD_PORTS = (None, 80, 443)


def _port_rank(url: str) -> int:
    """Sort key: 0 for a standard port, 1 for an odd one, 2 for unparsable.

    Bilibili hands out ``baseUrl`` on CDN ports 4483 and 8082 — addresses
    that *work* but that :func:`is_public_url` refuses, by design, as a probe
    fence.  The same clip is listed on ``upos-sz-mirror*.bilivideo.com`` under
    the ``backupUrl`` array on port 443, which the fence allows (measured: the
    odd-port copy 403s the fence, the standard-port copy downloads 1.6 MB and
    samples three frames).  Ordering the standard-port copies first lets the
    sampler reach the working address without relaxing the SSRF rule at all.
    """
    try:
        port = urllib.parse.urlsplit(url).port
    except ValueError:
        return 2
    return 0 if port in _STANDARD_PORTS else 1

#: Hosts whose CDN refuses a referer other than the site root.  Bilibili is
#: the measured case: the share link a chat carries is the short ``b23.tv``
#: form, and passing it through as the referer answers 403 — as does sending
#: none at all.  ``https://www.bilibili.com/`` answers 206 and the clip
#: downloads whole (measured: 1.6 MB, 26.3 s, three frames sampled).  So for
#: these hosts the referer is the site root, never the page the link came
#: from.
_ROOT_REFERER_HOSTS = {
    "bilivideo.com": "https://www.bilibili.com/",
    "bilivideo.cn": "https://www.bilibili.com/",
    "bilibili.com": "https://www.bilibili.com/",
}


def _media_referer(media_url: str, page_url: str) -> str:
    """Referer to send when downloading *media_url*.

    Most CDNs accept the page URL, and that is what the caller passes.  A
    few pin the referer to their own root and reject anything else — Bilibili
    among them — so those hosts get their fixed root instead.  Without this,
    every Bilibili clip is fetched with a ``b23.tv`` referer and refused.

    Args:
        media_url: The media address being downloaded.
        page_url: The page the address was found on.

    Returns:
        The referer to send, possibly empty when *page_url* was.
    """
    try:
        host = (urllib.parse.urlsplit(media_url).hostname or "").lower()
    except ValueError:
        return page_url
    for suffix, root in _ROOT_REFERER_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return root
    return page_url


#: ``Duration: 00:00:20.06`` in ffmpeg's banner.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _project_root() -> Path:
    """Repository root, whether running from source or from the frozen EXE."""
    return Path(__file__).resolve().parent.parent.parent


def _candidate_binaries() -> list[Path]:
    """Places ``ffmpeg.exe`` has been known to live, most specific first."""
    candidates: list[Path] = []
    override = os.environ.get("FFMPEG_PATH", "").strip()
    if override:
        candidates.append(Path(override))

    import sys as _sys

    if getattr(_sys, "frozen", False):
        bundled = getattr(_sys, "_MEIPASS", "")
        if bundled:
            candidates.append(Path(bundled) / "native" / "windows" / "ffmpeg.exe")
        # Shipped beside the EXE (see build.spec) — the intended location for
        # the packaged build, since the 121 MB binary is not embedded.
        exe_dir = Path(_sys.executable).resolve().parent
        candidates.append(exe_dir / "native" / "windows" / "ffmpeg.exe")
        candidates.append(exe_dir / "ffmpeg.exe")

    candidates.append(_project_root() / "native" / "windows" / "ffmpeg.exe")

    found = shutil.which("ffmpeg")
    if found:
        candidates.append(Path(found))
    return candidates


_ffmpeg_cache: Optional[str] = None
_ffmpeg_checked = False


def ffmpeg_path() -> Optional[str]:
    """Absolute path to a runnable ffmpeg, or None when there is none.

    Cached after the first call: locating it walks the filesystem and reads an
    environment variable, neither of which changes mid-run.  Checked once
    rather than per video so a missing binary costs one wasted ``stat``, not
    one per message.
    """
    global _ffmpeg_cache, _ffmpeg_checked
    if _ffmpeg_checked:
        return _ffmpeg_cache
    _ffmpeg_checked = True
    for candidate in _candidate_binaries():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                _ffmpeg_cache = str(candidate)
                break
        except OSError:  # unreadable path, odd drive, …
            continue
    if _ffmpeg_cache is None:
        logger.info(
            "Video frames unavailable: no ffmpeg found (looked in %s)",
            ", ".join(str(c) for c in _candidate_binaries()),
        )
    else:
        logger.debug("Video frames: using %s", _ffmpeg_cache)
    return _ffmpeg_cache


def reset_ffmpeg_cache() -> None:
    """Forget the located binary.  Only for tests that change the search path."""
    global _ffmpeg_cache, _ffmpeg_checked
    _ffmpeg_cache = None
    _ffmpeg_checked = False


def available() -> bool:
    """True when frames can be extracted at all."""
    return ffmpeg_path() is not None


# ── Finding the media address ────────────────────────────────────────


def _unescape(raw: str) -> str:
    """Undo the escaping a JSON string literal carries.

    Sites write ``http:\\u002Fsns-video…`` so the address is not a usable URL
    until ``\\u002F`` (and the shorter ``\\/``) become ``/``.  ``&amp;`` appears
    when the address sat in HTML rather than JSON.
    """
    return (
        raw.replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )


def video_urls_from_page(page: str, *, limit: int = 4) -> list[str]:
    """Return the video file addresses a page mentions, best first.

    Several copies of the same clip are usually listed (a low- and a
    high-bitrate rendition on different CDNs), so duplicates and non-HTTP
    entries are dropped and the order of the patterns decides priority:
    ``masterUrl`` is the one the site itself plays.

    Within that order, standard-port copies come before odd-port ones.  The
    reasoning is in :func:`_port_rank`: a CDN that serves the clip on port
    4483 is unreachable to this module by design, so listing it first would
    spend the one retry the caller can afford on an address that is certain
    to be refused.

    Args:
        page: Whole HTML document.
        limit: Most addresses to return.

    Returns:
        Candidate URLs, possibly empty.  Each still needs
        :func:`is_public_url` before it is fetched.
    """
    if not page or limit <= 0:
        return []
    seen: set[str] = set()
    found: list[str] = []
    for pattern in _URL_PATTERNS:
        for match in pattern.finditer(page):
            url = _unescape(match.group(1)).strip()
            if not url or url in seen:
                continue
            scheme = urllib.parse.urlsplit(url).scheme.lower()
            if scheme not in ("http", "https"):
                continue
            seen.add(url)
            found.append(url)
    # Stable sort: pattern order still decides among equals, which is what
    # keeps ``masterUrl`` ahead of a generic ``.mp4`` hit.
    found.sort(key=_port_rank)
    return found[:limit]


# ── Downloading ──────────────────────────────────────────────────────


def download_video(
    url: str,
    dest_dir: Path,
    *,
    referer: str = "",
    max_bytes: int = _MAX_VIDEO_BYTES,
) -> Optional[Path]:
    """Fetch *url* into *dest_dir* and return the file, or None.

    Refuses non-public addresses (:func:`is_public_url`) and stops once
    *max_bytes* have arrived, so a hostile or mislabelled URL cannot exhaust
    memory or disk.  A partial file is removed before returning None.

    Args:
        url: Media address taken from a page.
        dest_dir: Directory to write ``clip.mp4`` into.
        referer: Page the address came from.  Several CDNs reject a request
            with no referer; the caller passes the article URL.  Hosts that
            pin the referer to their own root (see :func:`_media_referer`)
            get that root instead.
        max_bytes: Transfer cap.

    Returns:
        The written file, or None on refusal, failure, or an empty body.
    """
    if not is_public_url(url):
        logger.warning("Video download refused: not a public URL (%s)", url[:80])
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
            "Mobile/15E148 Safari/604.1"
        ),
        "Accept": "*/*",
    }
    actual_referer = _media_referer(url, referer)
    if actual_referer:
        headers["Referer"] = actual_referer
    request = urllib.request.Request(url, headers=headers)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "clip.mp4"
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SEC) as response:
            total = 0
            with open(dest, "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        logger.warning(
                            "Video download exceeded %d bytes, aborting (%s)",
                            max_bytes, url[:80],
                        )
                        handle.close()
                        dest.unlink(missing_ok=True)
                        return None
                    handle.write(chunk)
    except Exception as exc:
        logger.warning(
            "Video download failed: %s (%s: %s)", url[:80],
            type(exc).__name__, exc,
        )
        dest.unlink(missing_ok=True)
        return None

    size = dest.stat().st_size if dest.exists() else 0
    if size < 1024:
        logger.warning("Video download produced %d bytes, discarding", size)
        dest.unlink(missing_ok=True)
        return None
    logger.info("Video downloaded: %s (%.1f MB)", url[:80], size / 1024 / 1024)
    return dest


# ── Frames ───────────────────────────────────────────────────────────


def probe_duration(video: Path, *, ffmpeg: Optional[str] = None) -> float:
    """Return the clip length in seconds, or 0.0 when it cannot be read.

    A zero answer is not an error: the caller falls back to sampling the
    opening seconds, which is where a short clip's content is anyway.
    """
    exe = ffmpeg or ffmpeg_path()
    if not exe:
        return 0.0
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", str(video)],
            capture_output=True, timeout=_PROBE_TIMEOUT_SEC,
            # The packaged bot has no console of its own, so Windows would
            # give this child a fresh black window on screen.
            **hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("ffmpeg probe failed for %s", video, exc_info=True)
        return 0.0
    # ffmpeg writes its banner to stderr and exits non-zero here because no
    # output file was named — that is expected, so stderr is all we read.
    banner = proc.stderr.decode(errors="replace")
    match = _DURATION_RE.search(banner)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def frame_timestamps(duration: float, count: int) -> list[float]:
    """Pick *count* moments spread across a clip.

    The middle of each equal slice is used rather than the very start: the
    first frame of a phone clip is often a black fade-in.  A clip whose
    length could not be read is sampled in the opening seconds.
    """
    count = max(1, count)
    if duration <= 0:
        return [round(0.5 + i, 2) for i in range(count)]
    return [round(duration * (i + 0.5) / count, 2) for i in range(count)]


def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    max_width: int = _MAX_FRAME_WIDTH,
) -> list[Path]:
    """Write *count* JPEG frames of *video* into *out_dir*.

    Args:
        video: Downloaded clip.
        out_dir: Where the JPEGs go.  Created if missing.
        count: How many frames to sample.
        max_width: Downscale bound; the taller side follows the aspect ratio.

    Returns:
        The frames that were actually written, in time order.  Empty when
        ffmpeg is missing or refused the file.
    """
    exe = ffmpeg_path()
    if not exe:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video, ffmpeg=exe)
    frames: list[Path] = []
    for index, moment in enumerate(frame_timestamps(duration, count)):
        dest = out_dir / f"frame_{index}.jpg"
        scale = f"scale='min({max_width},iw)':-2"
        try:
            proc = subprocess.run(
                [
                    exe, "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", str(moment), "-i", str(video),
                    "-frames:v", "1", "-q:v", "3", "-vf", scale,
                    str(dest),
                ],
                capture_output=True, timeout=_FFMPEG_TIMEOUT_SEC,
                **hidden_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "Frame extraction failed at %.2fs: %s", moment, type(exc).__name__,
            )
            continue
        if proc.returncode != 0:
            logger.debug(
                "ffmpeg refused frame at %.2fs: %s", moment,
                proc.stderr.decode(errors="replace")[:200],
            )
            continue
        if not dest.exists() or dest.stat().st_size < _MIN_FRAME_BYTES:
            dest.unlink(missing_ok=True)
            continue
        frames.append(dest)

    if frames:
        logger.info(
            "Video frames: %d/%d extracted from %.1fs clip (%s)",
            len(frames), count, duration, video.name,
        )
    else:
        logger.warning("Video frames: none extracted (%s)", video.name)
    return frames


def frames_from_urls(
    urls: list[str],
    page_url: str,
    *,
    count: int = DEFAULT_FRAME_COUNT,
) -> list[Path]:
    """Download one of *urls* and return sample frames of it.

    Split from :func:`frames_from_page` because the media address has to come
    from the *same* response that carried the article text.  Measured on a
    Xiaohongshu video note: asking the site for the page a second time gets a
    degraded 23 KB shell with no ``masterUrl`` at all, while the first
    response (70 KB) has it — so re-fetching to hunt for the video silently
    drops every clip.

    **Downloading the whole clip is deliberate.**  Streaming it — letting
    ffmpeg read the URL and seek — looks like the obvious saving, and it was
    tried and measured on a 2.5 MB / 60 s clip sampling 3 frames:

    ================================  ==================
    full download (what this does)   100% of the file
    ffmpeg direct, 3 separate calls  ~49% (not 18%)
    ffmpeg direct, one call / 3 frames  ~50%
    local vetting relay in between   223-270% (worse)
    ================================  ==================

    The 18% figure that motivates the idea is a *single*-frame number; every
    extra frame pays for reading the header again, so three frames halve the
    transfer at best.  Worse, ffmpeg follows redirects on its own — a 302 to
    a loopback address was walked without complaint — so a stranger's URL
    could aim this machine at an intranet address.  Vetting that with an
    in-process relay costs more bandwidth than it saves (measured above),
    because a relay cannot hang up the way a CDN can.  Half the bytes is not
    worth an SSRF hole, and the clips people actually share are 2-7 MB.

    Args:
        urls: Candidate media addresses, best first.
        page_url: Page they came from; used as referer.
        count: How many frames to sample.

    Returns:
        Frame files in a per-call temporary directory, or an empty list.
    """
    if not available() or not urls:
        return []

    workdir = Path(tempfile.mkdtemp(prefix="webot_video_"))
    for url in urls:
        video = download_video(url, workdir, referer=page_url)
        if not video:
            continue
        frames = extract_frames(video, workdir, count=count)
        # The clip is large and no longer needed once the frames exist.
        video.unlink(missing_ok=True)
        if frames:
            return frames

    shutil.rmtree(workdir, ignore_errors=True)
    return []


def frames_from_page(
    page: str,
    page_url: str,
    *,
    count: int = DEFAULT_FRAME_COUNT,
) -> list[Path]:
    """Find the video a page embeds and return sample frames of it.

    The whole path — address → download → sample — behind one call, because
    the caller only ever wants the frames and every step degrades to "no
    frames" rather than raising.

    Callers that already fetched the page for its text should pass the
    addresses to :func:`frames_from_urls` instead: re-fetching the page is
    known to lose the video (see that function's note).

    Args:
        page: The page's HTML, already fetched.
        page_url: Where it came from; used for SSRF checks and as referer.
        count: How many frames to sample.

    Returns:
        Frame files in a per-call temporary directory, or an empty list.
        The caller owns the directory and should remove it after describing
        the frames.
    """
    if not available():
        return []
    urls = video_urls_from_page(page)
    if not urls:
        logger.info("Video frames: no media address in %s", page_url[:80])
        return []
    return frames_from_urls(urls, page_url, count=count)
