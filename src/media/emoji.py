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
import logging
import re
import urllib.request
from pathlib import Path
from typing import Optional

from .vision import detect_mime

logger = logging.getLogger(__name__)

# Emoji files are a few hundred KB up to several MB; refuse anything absurd.
_MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SEC = 15

_MD5_RE = re.compile(r'md5="([0-9a-fA-F]{32})"')
_CDNURL_RE = re.compile(r'cdnurl="([^"]+)"')


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

    def __init__(self, wechat_data_dir: str, cache_dir: Path) -> None:
        """
        Args:
            wechat_data_dir: Parent of the ``wxid_*`` folders (may be empty).
            cache_dir:       Where downloads are kept between runs.
        """
        self._data_dir = Path(wechat_data_dir) if wechat_data_dir else None
        self._cache_dir = Path(cache_dir)

    # -- Public ------------------------------------------------------------

    def fetch(self, xml: str) -> Optional[Path]:
        """Return a local image file for this emoji message, or None."""
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

        if cdnurl:
            return self._download(md5, cdnurl)
        return None

    # -- Internals ---------------------------------------------------------

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
