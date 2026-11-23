"""Locate or download WeChat emoji (localType=47) image files.

Two sources, in order of cost:

1. **Local cache** — WeChat keeps received emoji under
   ``{wxid}/business/emoticon/Persist/{md5[:2]}/{md5}``.  Cheap, but only
   populated for emoji the user actually opened, and the stored payload is
   *not* always an image: on real data 5 of 5 local files began with random
   bytes and were neither zstd nor zlib, so they are treated as unusable.
2. **CDN** — the message XML carries a signed ``cdnurl``.  Fetching it
   returns the original GIF/PNG bytes.  Measured 9/9 success on real data
   once non-image local files were skipped; unlike image (localType=3)
   thumbnails, emoji URLs need no extra signing.

Images (localType=3) are deliberately *not* handled here: their ``.dat``
container is neither XOR nor AES (payload length is not 16-aligned), and
their CDN thumbnails return HTTP 400.  See ``src/media/pipeline.py``.
"""

import html
import json
import logging
import os
import re
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .vision import detect_mime

logger = logging.getLogger(__name__)

# Emoji files are a few hundred KB up to several MB; refuse anything absurd.
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 15

# WeChat writes attributes both ways: a *received* emoji carries
# ``cdnurl = "…"`` with spaces around the ``=``, while the shortened body
# some builds store uses ``md5="…"`` with none.  Matching only the compact
# form made every full XML look like it had no download address at all —
# the URL was there, the regex just skipped it.  ``\b`` keeps the pattern
# off ``androidmd5`` / ``s60v3md5``, which also end in ``md5``.
_MD5_RE = re.compile(r'\bmd5\s*=\s*"([0-9a-fA-F]{32})"')
_CDNURL_RE = re.compile(r'\bcdnurl\s*=\s*"([^"]+)"')

#: Where the md5 → download-URL table is kept between runs.  A signed emoji
#: ``cdnurl`` stays valid for weeks (verified on a 15-day-old message), so
#: one successful lookup keeps paying off long after the message scrolls out
#: of the readable history window.
_URL_TABLE_NAME = "emoji_urls.json"

#: Cap on remembered addresses.  Beyond this the oldest entries are dropped;
#: a regularly used sticker set stays well under it.
_MAX_URL_ENTRIES = 2000


def parse_emoji_xml(xml: str) -> tuple[Optional[str], Optional[str]]:
    """Extract ``(md5, cdnurl)`` from an emoji message's XML.

    The CDN URL is HTML-escaped inside the attribute (``&amp;``), so it
    must be unescaped before use.
    """
    if not xml:
        return None, None
    md5_match = _MD5_RE.search(xml)
    url_match = _CDNURL_RE.search(xml)
    md5 = md5_match.group(1).lower() if md5_match else None
    url = html.unescape(url_match.group(1)) if url_match else None
    return md5, url


class EmojiStore:
    """Find emoji images locally, falling back to the CDN."""

    def __init__(
        self,
        wechat_data_dir: str,
        cache_dir: Path,
        url_lookup: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> None:
        """
        Args:
            wechat_data_dir: Parent of the ``wxid_*`` folders (may be empty).
            cache_dir:       Where downloads are kept between runs.
            url_lookup:      Called as ``url_lookup(md5, chat_id)`` when this
                message carries no ``cdnurl``.  A chat can receive a *stripped*
                sticker body (``<emoji md5=… len=…/>`` only), while the very
                same sticker arrived elsewhere with a full signed address;
                the callback is how the backend searches for it.
        """
        self._data_dir = Path(wechat_data_dir) if wechat_data_dir else None
        self._cache_dir = Path(cache_dir)
        self._url_lookup = url_lookup
        self._urls_path = self._cache_dir.parent / _URL_TABLE_NAME
        self._urls: dict[str, str] = self._load_url_table()
        # Emoji that a lookup already failed to resolve.  Repeated scans of
        # the chat history cost seconds each, so a miss is not retried until
        # the process restarts.
        self._misses: set[str] = set()
        self._urls_lock = threading.Lock()

    # -- Public ------------------------------------------------------------

    def fetch(self, xml: str, chat_id: str = "") -> Optional[Path]:
        """Return a local image file for this emoji message, or None.

        Args:
            xml: The emoji message body (or the quoted ``<emoji>`` payload).
            chat_id: Session the message belongs to, used to widen the
                download-address search when the body has been stripped.
        """
        md5, cdnurl = parse_emoji_xml(xml)
        if not md5 and not cdnurl:
            return None

        if md5:
            cached = self._cache_dir / md5
            if cached.is_file() and cached.stat().st_size > 0:
                # A download that was interrupted, or an older run's bad
                # fetch, can leave a non-image behind.  Serving it would
                # fail the vision call and keep failing forever, because the
                # file is never looked at again — so verify and drop it.
                if self._is_image(cached):
                    return cached
                logger.info(
                    "Emoji cache entry is not an image, refetching: %s", cached
                )
                try:
                    cached.unlink()
                except OSError:
                    logger.debug("Cannot remove %s", cached, exc_info=True)

            local = self._find_local(md5)
            if local:
                return local

        url = cdnurl
        if not url and md5:
            url = self.known_url(md5) or self._rescue_url(md5, chat_id)

        if url:
            image = self._download(md5, url)
            if image and md5:
                # Remember a working address: the same sticker is quoted
                # again later, and by then its full XML may be long gone.
                self.remember_urls({md5: url})
            return image
        return None

    def known_url(self, md5: str) -> Optional[str]:
        """Return the remembered download address for *md5*, if any."""
        with self._urls_lock:
            return self._urls.get(md5.lower())

    def set_url_lookup(
        self, lookup: Optional[Callable[[str, str], Optional[str]]],
    ) -> None:
        """Install (or clear) the callback used for stripped sticker bodies."""
        self._url_lookup = lookup

    def remember_urls(self, entries: dict[str, str]) -> None:
        """Persist ``md5 → cdnurl`` pairs discovered this run.

        A sticker's signed address stays valid for weeks, so one successful
        lookup keeps paying off after the message itself scrolls out of the
        readable history window — and it spares the next call a full scan.
        """
        if not entries:
            return
        with self._urls_lock:
            changed = False
            for md5, url in entries.items():
                key = str(md5).lower()
                if not url or self._urls.get(key) == url:
                    continue
                self._urls[key] = url
                changed = True
            if not changed:
                return
            if len(self._urls) > _MAX_URL_ENTRIES:
                # Keep the newest entries; the dict preserves insertion order.
                excess = len(self._urls) - _MAX_URL_ENTRIES
                for stale in list(self._urls)[:excess]:
                    del self._urls[stale]
            payload = dict(self._urls)
            self._write_url_table(payload)

    # -- Internals ---------------------------------------------------------

    def _rescue_url(self, md5: str, chat_id: str) -> Optional[str]:
        """Ask the backend to find this md5's address in another chat."""
        if not self._url_lookup or md5 in self._misses:
            return None
        try:
            url = self._url_lookup(md5, chat_id)
        except Exception:
            logger.debug("Emoji URL lookup failed (md5=%s)", md5, exc_info=True)
            url = None
        if url:
            logger.info("Emoji %s: recovered CDN address from history", md5)
            self.remember_urls({md5: url})
            return url
        self._misses.add(md5)
        logger.debug("Emoji %s: no CDN address in any readable chat", md5)
        return None

    def _load_url_table(self) -> dict[str, str]:
        try:
            raw = self._urls_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.info("Emoji URL table is unreadable, starting empty: %s",
                        self._urls_path)
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key).lower(): value
            for key, value in data.items()
            if isinstance(value, str) and value
        }

    def _write_url_table(self, payload: dict[str, str]) -> None:
        """Write the table atomically, tolerating read-only locations."""
        try:
            self._urls_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._urls_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=0, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self._urls_path)
        except OSError:
            logger.debug("Cannot persist emoji URL table", exc_info=True)

    def _persist_dirs(self) -> list[Path]:
        """Every ``business/emoticon/Persist`` directory we can see."""
        if self._data_dir is None or not self._data_dir.is_dir():
            return []

        # The configured dir may be the account folder itself or its parent.
        direct = self._data_dir / "business" / "emoticon" / "Persist"
        if direct.is_dir():
            return [direct]
        return sorted(
            p / "business" / "emoticon" / "Persist"
            for p in self._data_dir.glob("wxid_*")
            if (p / "business" / "emoticon" / "Persist").is_dir()
        )

    def _find_local(self, md5: str) -> Optional[Path]:
        """Return the cached emoji file, but only if it is really an image.

        The ``Persist`` payload is not always an image: some entries are an
        opaque container that is neither zstd nor zlib (measured 5 of 5 such
        files on real data).  Returning one would make the vision call fail
        on a non-image, so it is skipped and the caller falls back to the
        CDN, which does serve the real GIF/PNG.
        """
        for persist in self._persist_dirs():
            candidate = persist / md5[:2] / md5
            if not (candidate.is_file() and candidate.stat().st_size > 0):
                continue
            if self._is_image(candidate):
                return candidate
            logger.debug("Local emoji is not an image, skipping: %s", candidate)
        return None

    @staticmethod
    def _is_image(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                head = handle.read(16)
        except OSError:
            return False
        return detect_mime(head) is not None

    def _download(self, md5: Optional[str], cdnurl: str) -> Optional[Path]:
        try:
            req = urllib.request.Request(
                cdnurl, headers={"User-Agent": "MicroMessenger Client"}
            )
            with urllib.request.urlopen(
                req, timeout=_DOWNLOAD_TIMEOUT_SEC
            ) as resp:
                data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
        except Exception:
            logger.debug("Emoji download failed (md5=%s)", md5, exc_info=True)
            return None

        if not data or len(data) > _MAX_DOWNLOAD_BYTES:
            logger.debug("Emoji download rejected (md5=%s, %d bytes)",
                         md5, len(data))
            return None

        # An error page or a truncated stream is not an emoji.  Caching it
        # would poison the cache for every later run of this same sticker.
        if detect_mime(data[:16]) is None:
            logger.info(
                "Emoji download is not an image (md5=%s, %d bytes)",
                md5, len(data),
            )
            return None

        name = md5 or f"emoji_{abs(hash(cdnurl))}"
        target = self._cache_dir / name
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            logger.debug("Cannot cache emoji %s", name, exc_info=True)
            return None
        return target
