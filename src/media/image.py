"""Locate, decrypt and unwrap a WeChat image message (``localType=3``).

Images are the one media type that needs no network: the ``.dat`` file is
already on disk under ``msg/attach/{md5(session)}/{yyyy-MM}/Img/``.  Getting
from a message to a viewable picture takes three steps, each of which can
fail independently:

1. **Locate** — the XML ``md5`` is *not* the on-disk filename, so it cannot
   be pasted into a path.  Two keys are used instead: the base name after
   stripping the size-suffix variants, and the container size, which the XML
   records as ``length`` and which pairs exactly as ``file_size == length + 31``
   (verified on real data — see :func:`expected_dat_size`).
2. **Decrypt** — the four-segment ``.dat`` container, via
   :mod:`src.media.dat_decrypt`.  Needs the per-machine keys.
3. **Unwrap** — a payload starting with ``wxgf`` is an HEVC stream, via
   :mod:`src.media.wxgf`.

Anything that fails yields ``None`` and the caller keeps the ``[图片]``
placeholder, which is the honest answer: a half-decrypted image described by
a vision model would be worse than saying nothing.
"""

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .dat_decrypt import (
    DERIVE_TAIL_BYTES,
    HEADER_LEN,
    dat_version,
    decrypt_dat,
    derive_xor_key,
    expected_dat_size,
    sniff_format,
)
from .wxgf import unwrap_wxgf

logger = logging.getLogger(__name__)

#: Filename suffixes WeChat appends to the base md5, best picture first.
#: ``_t`` is a thumbnail and is only used when nothing better exists.
_VARIANT_RANK = {
    "_h": 600,      # full size
    "_hd": 550,
    "_b": 520,
    "_w": 510,
    "": 500,        # no suffix at all
    "_c": 400,
    "_t": 100,      # thumbnail
    ".t": 100,
    ".h": 600,
    "_thumb": 100,
}

#: Longest first, so ``_hd`` is stripped before ``_h`` would match it.
_SUFFIXES = sorted((s for s in _VARIANT_RANK if s), key=len, reverse=True)

#: Skip absurdly large files: decrypting them costs memory for no benefit.
_MAX_DAT_BYTES = 64 * 1024 * 1024

#: How many file tails to sample when deriving the XOR key.  Ten already
#: settle it on real data, but a larger cap costs little and covers stores
#: whose first files are all thumbnails.
_DERIVE_SAMPLE_LIMIT = 400


def strip_variant(stem: str) -> str:
    """Return the base md5 of a ``.dat`` stem, dropping its size suffix.

    ``"1e5f9167..._h"`` → ``"1e5f9167..."``; an unsuffixed stem is unchanged.
    Suffixes are tried longest first, otherwise ``_hd`` would be stripped as
    ``_h`` and leave a trailing ``d``.
    """
    for suffix in _SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def variant_rank(stem: str) -> int:
    """Preference score for a ``.dat`` stem — higher is a better picture."""
    for suffix in _SUFFIXES:
        if stem.endswith(suffix):
            return _VARIANT_RANK[suffix]
    return _VARIANT_RANK[""]


class ImageStore:
    """Resolve an image message to a decrypted, viewable file."""

    def __init__(
        self,
        wechat_data_dir: str,
        cache_dir: Path,
        xor_key: Optional[int] = None,
        aes_key_text: str = "",
        allow_derive_xor: bool = True,
    ) -> None:
        """
        Args:
            wechat_data_dir: Parent of the ``wxid_*`` folders, or an account
                folder (may be empty).
            cache_dir:       Where decrypted images are kept between runs.
            xor_key:         Single-byte XOR key, or None when unavailable.
            aes_key_text:    Configured AES key string.
            allow_derive_xor: Recover the XOR key from the files themselves
                when it is not configured.  The key is not a secret that has
                to be extracted — see :func:`dat_decrypt.derive_xor_key`.
        """
        self._data_dir = Path(wechat_data_dir) if wechat_data_dir else None
        self._cache_dir = Path(cache_dir)
        self._xor_key = xor_key
        self._aes_key_text = aes_key_text or ""
        self._allow_derive_xor = allow_derive_xor
        # Deriving scans the attachment tree, so it runs at most once and the
        # answer (including "could not tell") is kept.
        self._xor_derive_done = False
        self._derived_xor: Optional[int] = None

    @property
    def can_decrypt(self) -> bool:
        """True when decryption is worth attempting."""
        return (
            self._xor_key is not None
            or bool(self._aes_key_text)
            or self._allow_derive_xor
        )

    def fetch(self, msg: dict) -> Optional[Path]:
        """Return a decrypted image file for this message, or None."""
        if not self.can_decrypt:
            logger.debug("Image keys not configured — keeping [图片]")
            return None

        xml = _body_xml(msg)
        if not xml:
            return None

        dat = self._locate(msg, xml)
        if dat is None:
            return None

        cached = self._cache_path(dat)
        if cached.is_file() and cached.stat().st_size > 0:
            return cached

        image = self._decrypt(dat)
        if not image:
            return None

        return self._cache(dat, image)

    # -- Locating ----------------------------------------------------------

    def _locate(self, msg: dict, xml: str) -> Optional[Path]:
        """Find the ``.dat`` file belonging to this message."""
        chat_id = _first_attr(msg, ("chat_id", "talker", "username"))
        if not chat_id:
            return None

        session_dir = self._session_dir(chat_id)
        if session_dir is None:
            logger.debug("No attach directory for %s", chat_id)
            return None

        md5 = _xml_attr(xml, "md5")
        length = _as_int(_xml_attr(xml, "length"))

        # The message timestamp names the month directory; scanning only that
        # one keeps a busy group (thousands of files) cheap.  A wrong guess
        # falls through to the full scan below.
        month = _month_dir(_first_attr(msg, ("create_time", "createTime", "timestamp")))
        if month:
            hit = self._search(session_dir / month / "Img", md5, length)
            if hit is not None:
                return hit
        return self._search_all(session_dir, md5, length)

    def _session_dir(self, chat_id: str) -> Optional[Path]:
        """Return ``msg/attach/{md5(chat_id)}`` for the account holding it."""
        digest = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
        for attach in self._attach_dirs():
            candidate = attach / digest
            if candidate.is_dir():
                return candidate
        return None

    def _attach_dirs(self) -> list[Path]:
        """Every ``msg/attach`` directory we can see."""
        if self._data_dir is None or not self._data_dir.is_dir():
            return []
        direct = self._data_dir / "msg" / "attach"
        if direct.is_dir():
            return [direct]
        try:
            accounts = sorted(self._data_dir.glob("wxid_*"))
        except OSError:
            return []
        return [p / "msg" / "attach" for p in accounts
                if (p / "msg" / "attach").is_dir()]

    def _search_all(self, session_dir: Path, md5, length) -> Optional[Path]:
        """Search every month directory under one session."""
        try:
            img_dirs = sorted(session_dir.glob("*/Img"))
        except OSError:
            return None
        for img_dir in img_dirs:
            hit = self._search(img_dir, md5, length)
            if hit is not None:
                return hit
        return None

    def _search(self, img_dir: Path, md5, length) -> Optional[Path]:
        """Pick the best ``.dat`` in *img_dir* for this message.

        Two independent keys, because neither is reliable alone: the base
        name matches only sometimes, and the size rule only when the file is
        a full-size original (a ``_h`` variant has extra plaintext, so its
        size is *not* ``length + 31``).
        """
        try:
            files = [f for f in img_dir.glob("*.dat") if f.is_file()]
        except OSError:
            return None
        if not files:
            return None

        if md5:
            named = [f for f in files if strip_variant(f.stem) == md5]
            if named:
                return max(named, key=lambda f: variant_rank(f.stem))

        if length:
            target = expected_dat_size(length)
            sized = [f for f in files if _size(f) == target]
            if sized:
                return max(sized, key=lambda f: variant_rank(f.stem))

        return None

    # -- Decrypting --------------------------------------------------------

    def _decrypt(self, dat: Path) -> Optional[bytes]:
        """Decrypt *dat* and unwrap a ``wxgf`` container if present."""
        size = _size(dat)
        if not size or size > _MAX_DAT_BYTES:
            logger.debug("Skipping %s (%s bytes)", dat, size)
            return None

        try:
            data = dat.read_bytes()
        except OSError:
            logger.debug("Cannot read %s", dat, exc_info=True)
            return None

        plain = decrypt_dat(data, self._xor_key, self._aes_key_text)
        if not plain:
            # The XOR key is recoverable from the files, so a missing one is
            # not fatal — try once, then reuse the answer for every message.
            derived = self._resolve_xor_key()
            if derived is not None and derived != self._xor_key:
                plain = decrypt_dat(data, derived, self._aes_key_text)
        if not plain:
            return None

        if sniff_format(plain) == "wxgf":
            plain = unwrap_wxgf(plain)
            if not plain:
                logger.debug("wxgf container could not be decoded: %s", dat)
                return None

        if not sniff_format(plain):
            # A wrong key yields random bytes that still pass the header
            # checks; refusing them here keeps garbage out of the cache.
            logger.debug("Decrypted %s is not an image — key wrong?", dat)
            return None
        return plain

    # -- Deriving the XOR key ----------------------------------------------

    def _resolve_xor_key(self) -> Optional[int]:
        """Return the configured XOR key, or one derived from the files.

        Runs the derivation at most once per store: it walks the attachment
        tree, and the answer cannot change while WeChat keeps the same
        installation.  A failed derivation is remembered too, so a machine
        with no usable files does not rescan on every message.
        """
        if self._xor_key is not None:
            return self._xor_key
        if not self._allow_derive_xor or self._xor_derive_done:
            return self._derived_xor

        self._xor_derive_done = True
        self._derived_xor = derive_xor_key(self._sample_tails())
        if self._derived_xor is None:
            logger.info(
                "Image XOR key could not be derived from local files — "
                "set IMAGE_XOR_KEY if images stay [图片]"
            )
        else:
            logger.info(
                "Image XOR key derived from local files: 0x%02X",
                self._derived_xor,
            )
        return self._derived_xor

    def _sample_tails(self):
        """Yield the masked tail of every V2 container we can find.

        Only the last few bytes of each file are read, so sampling thousands
        of images costs a directory walk rather than megabytes of I/O.
        """
        taken = 0
        for path in self._iter_dat_files():
            if taken >= _DERIVE_SAMPLE_LIMIT:
                return
            try:
                with path.open("rb") as handle:
                    header = handle.read(HEADER_LEN)
                    if dat_version(header) != 2:
                        continue
                    xor_size = int.from_bytes(header[10:14], "little", signed=True)
                    if xor_size < 2:
                        continue
                    handle.seek(-min(xor_size, DERIVE_TAIL_BYTES), os.SEEK_END)
                    tail = handle.read()
            except OSError:
                continue
            if tail:
                taken += 1
                yield tail

    def _iter_dat_files(self):
        """Walk the attachment tree."""
        for attach in self._attach_dirs():
            try:
                for path in attach.rglob("*.dat"):
                    yield path
            except OSError:
                continue

    def _cache_path(self, dat: Path) -> Path:
        return self._cache_dir / f"{dat.stem}.jpg"

    def _cache(self, dat: Path, image: bytes) -> Optional[Path]:
        """Write *image* to the cache and return its path."""
        target = self._cache_path(dat)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image)
        except OSError:
            logger.debug("Cannot cache %s", target, exc_info=True)
            return None
        return target


def _body_xml(msg: dict) -> Optional[str]:
    """Decode the message body to its XML text."""
    from .blob import decode_blob, is_binary

    raw = msg.get("message_content") or msg.get("content") or ""
    if not raw:
        return None
    if not is_binary(msg):
        return str(raw)
    return decode_blob(str(raw))


def _xml_attr(xml: str, name: str) -> Optional[str]:
    """Return attribute *name* from a message XML, or None."""
    import re

    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', xml or "")
    return match.group(1) if match else None


def _first_attr(msg: dict, names) -> str:
    """Return the first non-empty value among *names* in *msg*."""
    for name in names:
        value = msg.get(name)
        if value:
            return str(value)
    return ""


def _as_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _month_dir(timestamp: str) -> Optional[str]:
    """Return the ``yyyy-MM`` directory for a message timestamp."""
    seconds = _as_int(timestamp)
    if not seconds:
        return None
    try:
        return time.strftime("%Y-%m", time.localtime(seconds))
    except (OSError, ValueError):
        return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
