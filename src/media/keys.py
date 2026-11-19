"""Resolve the per-machine keys needed to decrypt WeChat ``.dat`` images.

The keys are **not** in the message XML and cannot be derived from anything
the bot already reads: they are generated per WeChat installation.  So they
have to be supplied by the user, and this module is the single place that
decides where they come from.

Two providers, selected by ``IMAGE_KEY_PROVIDER``:

``env`` (default)
    Read ``IMAGE_XOR_KEY`` / ``IMAGE_AES_KEY`` from the config, i.e. ``.env``.

``file``
    Read ``data/image_keys.json``, so a third-party extractor's output can be
    dropped in without editing ``.env``::

        {"xor_key": "0x37", "aes_key": "491e17ca4a5f665d..."}

``auto``
    Ask ``wx_key.dll``'s ``GetImageKey`` export, falling back to
    ``data/image_keys.json`` when WeChat is not running.  This is the only
    provider that yields a *per-account* table, and multi-account machines
    need one: an account's AES key cannot decrypt another account's images
    (measured 3808/3808 for the right pairing, 0/1 for the wrong one).

Keys already present in the config always win, whatever the provider — that
way an explicit ``.env`` entry is never silently overridden by a stale file.

A missing or malformed key is not an error: it resolves to ``None`` and the
caller degrades to the ``[图片]`` placeholder.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, NamedTuple, Optional

from .dat_decrypt import aes_key_from_text, parse_xor_key

logger = logging.getLogger(__name__)

#: Filename of the ``file`` provider's payload, relative to the project root.
KEY_FILE_NAME = "image_keys.json"

#: Accepted spellings in the JSON payload — extractors differ.
_XOR_KEYS = ("xor_key", "xorKey", "image_xor_key")
_AES_KEYS = ("aes_key", "aesKey", "image_aes_key")


class ImageKeys(NamedTuple):
    """A resolved pair of image keys; either side may be None."""

    xor_key: Optional[int]
    aes_key_text: str

    @property
    def usable(self) -> bool:
        """True when at least one key resolved, i.e. decrypting is worth trying."""
        return self.xor_key is not None or bool(self.aes_key_text)


def resolve_image_keys(config, data_dir: Optional[Path] = None) -> ImageKeys:
    """Return the image keys configured on *config*.

    Args:
        config: BotConfig instance.
        data_dir: Directory holding ``image_keys.json`` for the ``file``
            provider.  Defaults to ``PROJECT_ROOT / "data"``.

    Returns:
        An :class:`ImageKeys`; both fields None/empty when nothing is set.
    """
    xor_text = str(getattr(config, "image_xor_key", "") or "").strip()
    aes_text = str(getattr(config, "image_aes_key", "") or "").strip()

    provider = (getattr(config, "image_key_provider", "env") or "env").strip().lower()
    if provider == "file":
        file_xor, file_aes = _read_key_file(data_dir)
        # Config values win, so an explicit .env entry stays authoritative.
        xor_text = xor_text or file_xor
        aes_text = aes_text or file_aes
    elif provider == "auto":
        auto_keys = _read_auto_keys()
        if auto_keys is not None:
            xor_text = xor_text or ("" if auto_keys.xor_key is None else str(auto_keys.xor_key))
            aes_text = aes_text or auto_keys.aes_key_text
        else:
            # WeChat is not running: a previously saved file still helps.
            file_xor, file_aes = _read_key_file(data_dir)
            xor_text = xor_text or file_xor
            aes_text = aes_text or file_aes
    elif provider != "env":
        logger.warning(
            "Unknown IMAGE_KEY_PROVIDER=%r — using env only", provider
        )

    return _normalise(xor_text, aes_text)


def resolve_image_key_table(
    config, data_dir: Optional[Path] = None
) -> "ImageKeyTable":
    """Return image keys grouped by the account that owns them.

    Only the ``auto`` provider can produce a genuinely per-account table;
    ``env`` and ``file`` describe one pair that is applied to every account,
    which is all a single-account machine needs.

    Args:
        config: BotConfig instance.
        data_dir: Directory holding ``image_keys.json``.  Defaults to
            ``PROJECT_ROOT / "data"``.

    Returns:
        An :class:`ImageKeyTable`.  Empty when no key could be resolved.
    """
    provider = (getattr(config, "image_key_provider", "env") or "env").strip().lower()

    if provider != "auto":
        # One pair, applied to whichever account owns the file.
        return ImageKeyTable(shared=resolve_image_keys(config, data_dir))

    # Ask the extractor exactly once: each call is a DLL round trip that
    # retries for seconds when WeChat is not running.
    extracted = _extract_auto_table()
    if not extracted:
        # WeChat is not running — use the keys a previous run saved, then
        # the older flat file, which at least keeps a single-account setup
        # alive.
        saved = read_key_file_table(data_dir)
        if saved:
            logger.info(
                "WeChat not running — using %d saved account key(s)", len(saved)
            )
            shared = _normalise(
                str(getattr(config, "image_xor_key", "") or "").strip(),
                str(getattr(config, "image_aes_key", "") or "").strip(),
            )
            return ImageKeyTable(shared=shared, by_wxid=saved)
        return ImageKeyTable(shared=resolve_image_keys(
            _with_provider(config, "file"), data_dir
        ))

    by_wxid: Dict[str, ImageKeys] = {}
    for wxid, key in extracted.items():
        keys = _normalise(
            "" if key.xor_key is None else str(key.xor_key), key.aes_key_text
        )
        if keys.usable:
            by_wxid[wxid] = keys

    # ``shared`` stays empty unless .env pins one explicitly, so an unknown
    # account cannot silently decrypt with the wrong account's key.
    shared = _normalise(
        str(getattr(config, "image_xor_key", "") or "").strip(),
        str(getattr(config, "image_aes_key", "") or "").strip(),
    )
    return ImageKeyTable(shared=shared, by_wxid=by_wxid)


def _with_provider(config, provider: str):
    """Return *config* with ``image_key_provider`` overridden.

    Only used to reuse :func:`resolve_image_keys` for the fallback path
    without mutating the caller's config object.
    """
    import copy

    try:
        clone = copy.copy(config)
    except Exception:  # pragma: no cover - non-copyable config
        return config
    try:
        clone.image_key_provider = provider
    except Exception:  # pragma: no cover - frozen config
        return config
    return clone


class ImageKeyTable:
    """Image keys for one or more WeChat accounts.

    ``by_wxid`` holds what ``GetImageKey`` reported, keyed by the account's
    ``wxid``.  ``shared`` is the single configured pair, used when the account
    is unknown — a single-account machine never looks at ``by_wxid`` at all.
    """

    def __init__(
        self,
        shared: Optional[ImageKeys] = None,
        by_wxid: Optional[Dict[str, ImageKeys]] = None,
    ) -> None:
        self.shared = shared if shared is not None else ImageKeys(None, "")
        self.by_wxid = dict(by_wxid or {})

    @property
    def usable(self) -> bool:
        """True when any account's keys are usable."""
        return self.shared.usable or any(k.usable for k in self.by_wxid.values())

    def for_wxid(self, wxid: Optional[str]) -> ImageKeys:
        """Return the keys for *wxid*, falling back to the shared pair.

        An unknown account gets ``shared`` rather than nothing: on a
        single-account machine the caller cannot always name the owner, and
        refusing would turn every image into ``[图片]``.

        With exactly one account extracted, that account is used even when
        the owner is unknown — a custom ``WECHAT_DATA_DIR`` need not contain
        a ``wxid_*`` folder to name it, and guessing is safe because there is
        nothing to confuse it with.  With several accounts an unknown owner
        must **not** borrow one, or images would be decrypted with the wrong
        account's key and silently fail.
        """
        if wxid:
            key = self.by_wxid.get(wxid)
            if key is not None and key.usable:
                return key
        if len(self.by_wxid) == 1:
            only = next(iter(self.by_wxid.values()))
            if only.usable:
                return only
        return self.shared

    def __len__(self) -> int:
        return len(self.by_wxid)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ImageKeyTable(shared={self.shared!r}, "
            f"accounts={sorted(self.by_wxid)!r})"
        )


def _normalise(xor_text: str, aes_text: str) -> ImageKeys:
    """Validate raw key text into an :class:`ImageKeys`."""
    xor_key = parse_xor_key(xor_text)
    if xor_text and xor_key is None:
        logger.warning("IMAGE_XOR_KEY %r is not a byte value — ignoring", xor_text)

    aes_key = aes_text if aes_key_from_text(aes_text) else ""
    if aes_text and not aes_key:
        logger.warning(
            "IMAGE_AES_KEY must be at least 16 characters — ignoring"
        )

    return ImageKeys(xor_key, aes_key)


def _extract_auto_table() -> dict:
    """Ask ``wx_key.dll`` for every account's keys; empty on any failure.

    Kept separate from :func:`_read_auto_keys` because the table is needed
    once per store while the single-pair answer is asked per config read.
    """
    if sys.platform != "win32":
        logger.debug("auto image keys need Windows — no wx_key.dll elsewhere")
        return {}
    try:
        from src.wechat.extract_key import extract_image_keys
    except (ImportError, OSError) as exc:
        logger.warning("Cannot load the image key extractor: %s", exc)
        return {}
    try:
        return extract_image_keys()
    except RuntimeError as exc:
        # Missing/unloadable DLL is a setup problem, not a per-message one;
        # say it once and let the caller degrade to [图片].
        logger.warning("图片密钥自动获取失败：%s", exc)
        return {}


def _read_auto_keys() -> Optional[ImageKeys]:
    """Return the primary account's keys from ``GetImageKey``, or None.

    Used by :func:`resolve_image_keys`, which has room for one pair only.
    """
    table = _extract_auto_table()
    if not table:
        return None
    # A single-account machine is the common case; with several accounts the
    # table from resolve_image_key_table is the accurate answer.
    key = next(iter(table.values()))
    return _normalise(
        "" if key.xor_key is None else str(key.xor_key), key.aes_key_text
    )


def _read_key_file(data_dir: Optional[Path]):
    """Return ``(xor_text, aes_text)`` from ``image_keys.json``, or empty pair."""
    path = Path(data_dir) if data_dir else _default_data_dir()
    path = path / KEY_FILE_NAME
    if not path.exists():
        return "", ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Cannot read %s — image keys ignored", path, exc_info=True)
        return "", ""
    if not isinstance(payload, dict):
        logger.warning("%s must contain a JSON object — image keys ignored", path)
        return "", ""
    return _first(payload, _XOR_KEYS), _first(payload, _AES_KEYS)


def read_key_file_table(data_dir: Optional[Path] = None) -> Dict[str, ImageKeys]:
    """Return the per-account keys saved in ``image_keys.json``.

    Accepts the shape :func:`save_image_keys` writes::

        {"accounts": {"wxid_abc": {"xor_key": 237, "aes_key": "36a9..."}}}

    A flat file (the older single-account form) yields an empty table; it is
    still read by :func:`_read_key_file` for the ``file`` provider.
    """
    path = (Path(data_dir) if data_dir else _default_data_dir()) / KEY_FILE_NAME
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Cannot read %s — image keys ignored", path, exc_info=True)
        return {}
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, dict):
        return {}

    table: Dict[str, ImageKeys] = {}
    for wxid, entry in accounts.items():
        if not isinstance(entry, dict):
            continue
        keys = _normalise(_first(entry, _XOR_KEYS), _first(entry, _AES_KEYS))
        if keys.usable:
            table[str(wxid)] = keys
    return table


def save_image_keys(keys: dict, data_dir: Optional[Path] = None) -> Path:
    """Persist *keys* to ``image_keys.json`` in the per-account shape.

    Saving matters because ``GetImageKey`` only answers while WeChat runs: a
    bot that starts before the client would otherwise have no keys at all.
    The file is git-ignored (it holds machine-specific secrets).

    Args:
        keys: ``{wxid: ImageKey}`` as returned by
            :func:`src.wechat.extract_key.extract_image_keys`.
        data_dir: Directory to write into.  Defaults to ``PROJECT_ROOT/data``.

    Returns:
        The path written.
    """
    target_dir = Path(data_dir) if data_dir else _default_data_dir()
    path = target_dir / KEY_FILE_NAME
    accounts = {}
    for wxid, key in keys.items():
        accounts[str(wxid)] = {
            "xor_key": key.xor_key,
            "aes_key": key.aes_key_text,
        }
    payload = {"accounts": accounts}
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Cannot write %s", path, exc_info=True)
        raise
    return path


def _first(payload: dict, names) -> str:
    """Return the first present, non-empty value among *names*."""
    for name in names:
        value = payload.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _default_data_dir() -> Path:
    from src.config import PROJECT_ROOT

    return PROJECT_ROOT / "data"
