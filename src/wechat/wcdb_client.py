"""
Native WCDB database client — direct DLL calls, no external HTTP bridge.

Loads wcdb_api.dll via ctypes, applies one-byte DRM patch, and provides
the same data access as a dedicated HTTP bridge but entirely in-process.
"""
import ctypes as ct
from ctypes import wintypes
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PAGE_EXECUTE_READWRITE = 0x40

# ── DRM patch offset configuration ────────────────────────────────────
# Default values target WeChat 4.x.  When WeChat updates and the patch
# offset changes, you can override these via environment variables instead
# of modifying the source:
#
#   WCDB_PATCH_RVA   – hex RVA (relative virtual address) of the patch site
#   WCDB_PATCH_BYTE  – hex byte value expected at patch_site+1 before patching
#
# These are the bytes that the DRM check sets to signal "tampered":
#   mov eax, 2      (B8 02 00 00 00)   → normal / DRM active
#   mov eax, 0      (B8 00 00 00 00)   → patched / DRM bypassed
#
# Example for a future WeChat version:
#   set WCDB_PATCH_RVA=0x6f2a0
#   set WCDB_PATCH_BYTE=0x02

_DEFAULT_PATCH_RVA = 0x6e1f6
_DEFAULT_PATCH_BYTE = 0x02

_env_rva = os.environ.get("WCDB_PATCH_RVA", "").strip()
if _env_rva:
    PATCH_RVA = int(_env_rva, 16)
    logger.info("Using custom WCDB_PATCH_RVA from env: 0x%x", PATCH_RVA)
else:
    PATCH_RVA = _DEFAULT_PATCH_RVA
    logger.debug("Using default WCDB_PATCH_RVA: 0x%x", PATCH_RVA)

_env_byte = os.environ.get("WCDB_PATCH_BYTE", "").strip()
if _env_byte:
    EXPECTED_PATCH_BYTE = int(_env_byte, 16)
    logger.info("Using custom WCDB_PATCH_BYTE from env: 0x%x", EXPECTED_PATCH_BYTE)
else:
    EXPECTED_PATCH_BYTE = _DEFAULT_PATCH_BYTE
    logger.debug("Using default WCDB_PATCH_BYTE: 0x%x", EXPECTED_PATCH_BYTE)

# ── DLL loading ──────────────────────────────────────────────────────

_kernel32 = ct.WinDLL("kernel32", use_last_error=True)


REPLACEMENT_MARKER = b"webot-wcdb-api-replacement"


def _preload_libcrypto(dll_dir):
    """Make libcrypto-3.dll resolvable before loading wcdb_api.dll.

    The replacement DLL links against OpenSSL (SQLCipher has no Windows CNG
    backend).  It does not ship its own copy: the Python process this app runs
    in always carries one already, so we just load it by name first and the
    dependency resolves to that module.

    Searched in order: next to wcdb_api.dll, the PyInstaller bundle root, the
    directory holding the interpreter/EXE, and Python's own DLLs folder.
    """
    candidates = []
    if dll_dir:
        candidates.append(Path(dll_dir) / "libcrypto-3.dll")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "libcrypto-3.dll")
    candidates.append(Path(sys.executable).resolve().parent / "libcrypto-3.dll")
    candidates.append(Path(sys.prefix) / "DLLs" / "libcrypto-3.dll")

    for c in candidates:
        try:
            if c.is_file():
                ct.CDLL(str(c))
                logger.debug("Preloaded %s", c)
                return True
        except OSError as e:
            logger.debug("Preload of %s failed: %s", c, e)
    logger.debug("No libcrypto-3.dll preloaded; relying on default search path")
    return False



def _key_failure_message(tried_real_key: bool) -> str:
    """Build the error text for "no key source could open the database".

    Two very different situations used to share one message that claimed the
    key was simply missing, which sent users to re-extract a key they already
    had.  Keep the machine-readable prefix (the UI keys off ``KEY_MISSING`` /
    ``KEY_INVALID``) and make the advice match the actual cause.

    Args:
        tried_real_key: True when at least one non-empty key was attempted —
            i.e. a key IS configured but it does not decrypt this database.
    """
    if tried_real_key:
        return (
            "KEY_INVALID: 微信数据库密钥不匹配——已配置的密钥无法解密当前数据库。"
            "常见原因：多台机器或多个微信账号共用同一个 .env"
            "（例如虚拟机与宿主机共用共享文件夹里的 .env），"
            "或微信重新登录后密钥已更换。"
            "请点击下方「重新获取密钥」，在本机重新提取一次。"
        )
    return (
        "KEY_MISSING: 密钥未配置。"
        "点击下方「重新获取密钥」按钮，按提示退出并重新登录微信即可。"
    )


def _is_replacement_dll(dll):
    """True when `dll` is the self-built SQLCipher replacement, not the
    original WeFlow binary.

    The replacement exports `webot_wcdb_api_replacement()`; the original does
    not.  This matters because the original needs a one-byte DRM patch at a
    fixed RVA, while the replacement has no such code — writing those bytes
    into it would corrupt live code.
    """
    try:
        fn = dll.webot_wcdb_api_replacement
    except AttributeError:
        return False
    try:
        fn.argtypes = []
        fn.restype = ct.c_char_p
        marker = fn()
        return bool(marker) and REPLACEMENT_MARKER in marker
    except Exception as e:
        logger.debug("Replacement marker probe failed: %s", e)
        return False


def _apply_drm_patch(dll_handle, dll_path):
    """One-byte DRM patch: mov eax,2 -> mov eax,0 at the configured RVA.

    The patch offset and expected byte are controlled by PATCH_RVA and
    EXPECTED_PATCH_BYTE (see module-level config above).  Override them via
    WCDB_PATCH_RVA / WCDB_PATCH_BYTE environment variables when WeChat
    updates change the patch location.

    Only the original WeFlow DLL carries this check.  For any other binary we
    verify the byte first and refuse to write when it does not match, so a
    dropped-in replacement (or a future WeChat build) is never corrupted.
    """
    # Apply the patch
    patch_addr = ct.c_void_p(dll_handle + PATCH_RVA)
    old_protect = wintypes.DWORD()
    _kernel32.VirtualProtect(
        patch_addr, 5, PAGE_EXECUTE_READWRITE, ct.byref(old_protect)
    )

    buf = (ct.c_ubyte * 5).from_address(patch_addr.value)
    if buf[1] == EXPECTED_PATCH_BYTE:
        buf[1] = 0x00
        logger.info("DRM patch applied: RVA 0x%x 02->00", PATCH_RVA)
    elif buf[1] == 0x00:
        logger.info("DRM patch already present")
    else:
        logger.warning(
            "Unexpected byte 0x%02x at patch point — DLL may be tampered",
            buf[1],
        )

    _kernel32.VirtualProtect(
        patch_addr, 5, old_protect, ct.byref(wintypes.DWORD())
    )


# VirtualQuery constants for pointer validation
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100


class _MEMORY_BASIC_INFORMATION(ct.Structure):
    _fields_ = [
        ("BaseAddress", ct.c_void_p),
        ("AllocationBase", ct.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ct.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _read_gbk_string(ptr):
    """Read null-terminated string from a raw pointer.

    The WCDB DLL may return GBK or UTF-8 depending on the data source.
    Since all DLL inputs are UTF-8, try UTF-8 first, then fall back to GBK.
    Validates with JSON parse to confirm the correct encoding was chosen.

    Validates the pointer with VirtualQuery before reading to avoid
    access violations from corrupted/garbage DLL return values.
    """
    if not ptr or ptr.value == 0:
        return ""
    addr = ptr.value

    # Validate pointer with VirtualQuery before attempting to read
    try:
        mbi = _MEMORY_BASIC_INFORMATION()
        if not _kernel32.VirtualQuery(
            ct.c_void_p(addr), ct.byref(mbi), ct.sizeof(mbi)
        ):
            logger.warning(
                "VirtualQuery failed for ptr 0x%x — skipping", addr
            )
            return ""
        if mbi.State != MEM_COMMIT:
            logger.warning(
                "Pointer 0x%x points to uncommitted memory (state=%d)",
                addr, mbi.State,
            )
            return ""
        if mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD):
            logger.warning(
                "Pointer 0x%x points to inaccessible memory (protect=0x%x)",
                addr, mbi.Protect,
            )
            return ""
    except Exception as exc:
        logger.warning("Pointer validation failed for 0x%x: %s", addr, exc)
        return ""

    # Walk the buffer chunk-by-chunk, never reading past the end of the
    # committed region that holds it.  The old fixed 500000-byte ceiling
    # silently truncated large payloads — a 800-message page with base64
    # blobs runs past 500 KB, which used to surface as a JSON parse error.
    # Chunked reads also avoid the byte-at-a-time ctypes overhead.
    raw = bytearray()
    cursor = addr
    while True:
        try:
            mbi = _MEMORY_BASIC_INFORMATION()
            if not _kernel32.VirtualQuery(
                ct.c_void_p(cursor), ct.byref(mbi), ct.sizeof(mbi)
            ):
                logger.warning(
                    "VirtualQuery failed for ptr 0x%x — stopping read", cursor
                )
                break
            if mbi.State != MEM_COMMIT or (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)):
                logger.warning(
                    "Pointer 0x%x left readable memory (state=%d protect=0x%x)",
                    cursor, mbi.State, mbi.Protect,
                )
                break
            region_end = (mbi.BaseAddress or 0) + mbi.RegionSize
        except Exception as exc:
            logger.warning("Pointer walk failed at 0x%x: %s", cursor, exc)
            break

        if cursor >= region_end:
            break
        chunk = min(region_end - cursor, 1 << 20)
        try:
            block = ct.string_at(cursor, chunk)
        except (OSError, ValueError):
            logger.warning(
                "Access violation reading 0x%x (ptr returned by DLL)", cursor
            )
            break
        nul = block.find(b"\x00")
        if nul >= 0:
            raw += block[:nul]
            break
        raw += block
        cursor += chunk
    # Try UTF-8 first (DLL inputs are always UTF-8), fall back to GBK.
    # GBK decode of UTF-8 bytes can produce valid JSON with garbled Chinese
    # (JSON structural chars are ASCII, identical in both encodings), so the
    # old GBK-first heuristic silently returned mojibake.
    import json as _json
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            _json.loads(text)
            return text
        except (UnicodeDecodeError, _json.JSONDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


# ── Filesystem auto-detection ─────────────────────────────────────────


def _find_dll():
    """Find the bundled wcdb_api.dll."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "native" / "windows" / "wcdb_api.dll",
    ]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent / "native" / "windows" / "wcdb_api.dll")
        candidates.insert(0, Path(sys._MEIPASS) / "native" / "windows" / "wcdb_api.dll")

    for c in candidates:
        if c.exists():
            logger.info("Found wcdb_api.dll at: %s", c)
            return str(c.parent), str(c)

    raise FileNotFoundError(
        "wcdb_api.dll not found. Please place it in the native/windows/ folder next to the EXE."
    )


def _account_has_db(wxid_dir: Path) -> bool:
    """True when this account folder holds a readable session.db."""
    return (wxid_dir / "db_storage" / "session" / "session.db").exists()


def _wxid_dirs(base: Path) -> list[Path]:
    """Account folders under *base*, newest first (mtime order)."""
    try:
        return sorted(
            [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")],
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
    except PermissionError:
        logger.warning("Permission denied reading %s — skipping", base)
        return []


def _find_wxid_and_dbpath(custom_base_dir: str = "", pinned_wxid: str = ""):
    """Resolve WeChat wxid and data directory from the filesystem.

    ``pinned_wxid`` (from ``WECHAT_WXID``) wins outright.  That matters
    because the folder layout is not stable: re-logging into WeChat creates a
    new ``wxid_*`` folder, and the old one keeps a snapshot of the last
    session forever.  Picking by mtime therefore used to follow a frozen
    account silently — the key verified, groups resolved, and no new message
    ever arrived.

    A pinned wxid that cannot be found is an error, not a fallback: falling
    back would reproduce exactly the silent wrong-account read this pinning
    exists to prevent.

    If custom_base_dir is provided, scans that directory first.
    Otherwise falls back to Documents\\xwechat_files\\ and Documents\\WeChat Files\\.
    """
    # Collect candidate base directories to scan
    candidates: list[Path] = []

    # 1. Custom directory (highest priority)
    if custom_base_dir:
        custom = Path(custom_base_dir)
        if custom.exists() and custom.is_dir():
            candidates.append(custom)
            logger.info("Scanning custom WECHAT_DATA_DIR: %s", custom)
        else:
            logger.warning(
                "WECHAT_DATA_DIR=%s does not exist or is not a directory — "
                "falling back to auto-detection",
                custom_base_dir,
            )

    # 2. Default auto-detection paths
    documents = Path.home() / "Documents"
    for default_base in (documents / "xwechat_files", documents / "WeChat Files"):
        if default_base not in candidates:
            candidates.append(default_base)

    # ── Pinned account: only this one is acceptable ──────────────────
    if pinned_wxid:
        for base in candidates:
            if not base.exists():
                continue
            dirs = _wxid_dirs(base)
            # Exact folder name first: a prefix match on "wxid_abc" would
            # also accept "wxid_abcd_9999", i.e. another account.
            matches = [d for d in dirs if d.name == pinned_wxid]
            if not matches:
                matches = [d for d in dirs if d.name.startswith(pinned_wxid)]
            for wxid_dir in matches:
                if _account_has_db(wxid_dir):
                    logger.info(
                        "Using pinned WECHAT_WXID: wxid=%s db=%s",
                        wxid_dir.name, str(base),
                    )
                    return wxid_dir.name, str(base)
                raise FileNotFoundError(
                    f"WECHAT_WXID={pinned_wxid} matches {wxid_dir.name}, but it "
                    f"has no db_storage/session/session.db — WeChat may still "
                    f"be starting up. Clear WECHAT_WXID to let webot choose an "
                    f"account automatically."
                )
        raise FileNotFoundError(
            f"WECHAT_WXID={pinned_wxid} was not found under "
            f"{', '.join(str(c) for c in candidates) or '(no candidate dirs)'}. "
            f"WeChat may have been logged into a different account, or "
            f"WECHAT_DATA_DIR points elsewhere. Clear WECHAT_WXID to let webot "
            f"pick an account, or update it to the current wxid_* folder."
        )

    # Scan candidates in order
    for base in candidates:
        if not base.exists():
            continue
        wxid_dirs = _wxid_dirs(base)
        usable = [d for d in wxid_dirs if _account_has_db(d)]
        if not usable:
            continue

        if len(usable) > 1:
            # Do not let mtime silently decide which account to read: a
            # folder left behind by an earlier login keeps its old session.db
            # and would report a healthy key while never seeing new messages.
            logger.warning(
                "Multiple WeChat accounts found under %s (%s) — reading the "
                "newest. Set WECHAT_WXID to pin the right one.",
                base, ", ".join(d.name for d in usable),
            )

        wxid_dir = usable[0]
        source = "custom" if base == candidates[0] and custom_base_dir else "auto"
        logger.info("%s-detected: wxid=%s db=%s", source, wxid_dir.name, str(base))
        return wxid_dir.name, str(base)

    raise FileNotFoundError(
        "Cannot find WeChat data directory. Make sure WeChat is installed "
        "and you have logged in at least once."
    )


# ── Public API ────────────────────────────────────────────────────────

import sys as _sys


class WcdbNativeClient:
    """Direct WCDB database reader via patched wcdb_api.dll.

    Auto-detects WeChat data paths from the filesystem.
    The DLL is bundled with the EXE in the native/windows/ directory.
    """

    def __init__(self, dll_dir=None, config_path=None):
        # Resolve DLL
        if dll_dir is not None:
            self._dll_dir = dll_dir
            self._dll_path = os.path.join(dll_dir, "wcdb_api.dll")
        else:
            self._dll_dir, self._dll_path = _find_dll()

        # Resolve config (wxid + dbPath)
        self._config_path = config_path  # may be None — auto-detected
        self._dll = None
        self._handle = 0
        self._config = None
        self._nicknames = {}  # wxid -> display name cache

        self._load_config()

    # ── Init ──────────────────────────────────────────────────────────

    def _load_config(self):
        # Read wechat_data_dir / wechat_wxid from config (custom path support)
        custom_dir = ""
        pinned_wxid = ""
        try:
            from src.config import load_config
            config = load_config()
            custom_dir = config.wechat_data_dir
            pinned_wxid = getattr(config, "wechat_wxid", "") or ""
        except Exception as e:
            logger.warning(
                "Config load failed — falling back to auto-detection: %s", e
            )

        wxid, db_path = _find_wxid_and_dbpath(custom_dir, pinned_wxid)
        if pinned_wxid:
            logger.info("WECHAT_WXID pinned the account: %s", wxid)
        self._config = {
            "myWxid": wxid,
            "dbPath": db_path,
        }

    def init(self):
        """Load wcdb_api.dll, patch DRM, and initialize the WCDB engine."""
        os.add_dll_directory(self._dll_dir)
        dll_path = os.path.join(self._dll_dir, "wcdb_api.dll")

        # The replacement links OpenSSL; make libcrypto resolvable first.
        _preload_libcrypto(self._dll_dir)
        self._dll = ct.CDLL(dll_path)

        # The self-built replacement has no DRM check, so patching it would
        # only corrupt its code.  Only the original WeFlow binary needs this.
        if _is_replacement_dll(self._dll):
            self._is_replacement = True
            logger.info("Using self-built wcdb_api replacement (no DRM patch needed)")
        else:
            self._is_replacement = False
            _apply_drm_patch(self._dll._handle, dll_path)

        # Set up function signatures
        self._dll.InitProtection.argtypes = [ct.c_char_p]
        self._dll.InitProtection.restype = ct.c_int32

        self._dll.wcdb_init.argtypes = []
        self._dll.wcdb_init.restype = ct.c_int32

        self._dll.wcdb_open_account.argtypes = [
            ct.c_char_p, ct.c_char_p, ct.POINTER(ct.c_int64),
        ]
        self._dll.wcdb_open_account.restype = ct.c_int32

        self._dll.wcdb_get_sessions.argtypes = [
            ct.c_int64, ct.POINTER(ct.c_void_p),
        ]
        self._dll.wcdb_get_sessions.restype = ct.c_int32

        self._dll.wcdb_get_messages.argtypes = [
            ct.c_int64, ct.c_char_p, ct.c_int32, ct.c_int32,
            ct.POINTER(ct.c_void_p),
        ]
        self._dll.wcdb_get_messages.restype = ct.c_int32

        self._dll.wcdb_get_display_names.argtypes = [
            ct.c_int64, ct.c_char_p, ct.POINTER(ct.c_void_p),
        ]
        self._dll.wcdb_get_display_names.restype = ct.c_int32

        try:
            fn = self._dll.wcdb_get_contacts_compact
            fn.argtypes = [ct.c_int64, ct.c_char_p, ct.POINTER(ct.c_void_p)]
            fn.restype = ct.c_int32
        except Exception:
            self._dll.wcdb_get_contacts_compact = None

        try:
            fn = self._dll.wcdb_get_group_members
            fn.argtypes = [ct.c_int64, ct.c_char_p, ct.POINTER(ct.c_void_p)]
            fn.restype = ct.c_int32
        except Exception:
            self._dll.wcdb_get_group_members = None

        self._dll.wcdb_free_string.argtypes = [ct.c_void_p]
        self._dll.wcdb_free_string.restype = None

        # Init protection
        resource_path = os.path.dirname(self._dll_dir)
        self._dll.InitProtection(resource_path.encode("utf-8"))

        # Init engine
        ret = self._dll.wcdb_init()
        if ret != 0:
            raise RuntimeError(f"wcdb_init failed: {ret}")

        logger.info("WCDB engine initialized (DRM patched)")

    def open(self):
        """Open the WeChat session.db for the configured account.

        Tries each key source in turn and returns True on the first one that
        yields sessions.  Raises RuntimeError when none works — the message
        is prefixed KEY_MISSING (no key configured) or KEY_INVALID (a key is
        configured but does not decrypt this database), because the two need
        different user actions.
        """
        my_wxid = self._config.get("myWxid", "")
        db_base = self._config.get("dbPath", "")
        wxid_base = "_".join(my_wxid.split("_")[:3])

        account_dir = None   # wxid directory (e.g. .../xwechat_files/wxid_xxx)
        session_db = None    # full path to session.db
        base = Path(db_base)
        for entry in base.iterdir():
            if entry.name.startswith(wxid_base):
                account_dir = str(entry)
                candidate = entry / "db_storage" / "session" / "session.db"
                if candidate.exists():
                    session_db = str(candidate)
                    break   # only stop when we actually found session.db

        if not account_dir or not session_db:
            raise RuntimeError(f"session.db not found in {db_base}")

        # wcdb_open_account expects the session.db file path.
        # Passing the account directory results in ret=-3.
        db_paths = [session_db]

        # ── Resolve key, try each source until one yields data ──────
        import os as _os

        # Distinguishes "no key configured" from "the key we have does not
        # decrypt this database".  Both end in the same failure, but they
        # need very different user action: the first wants extraction, the
        # second usually means the .env carries another machine's/session's
        # key (e.g. a shared folder reused across a VM and its host).
        tried_real_key = False
        last_ret = 0
        last_source = ""

        for attempt, (key_candidate, source_label) in enumerate(self._key_candidates()):
            logger.info(
                "Trying WCDB key source #%d: %s (len=%d)",
                attempt + 1, source_label,
                len(key_candidate) if key_candidate else 0,
            )
            if key_candidate:
                tried_real_key = True

            # Build key variants to try.  The DLL accepts 64-char hex strings
            # (ret=0) but explicitly rejects raw bytes (ret=-3).  Only try hex.
            key_variants = []
            if key_candidate and len(key_candidate) == 64 and all(
                c in "0123456789abcdefABCDEF" for c in key_candidate
            ):
                key_variants.append((key_candidate.encode("utf-8"), "hex"))
            elif key_candidate:
                key_variants.append((key_candidate.encode("utf-8"), "str"))
            # else: empty key → skip (ret=-2 means DLL requires a key)

            for key_bytes, key_fmt in key_variants:
                for db_path in db_paths:
                    path_label = "dir" if db_path == account_dir else "file"
                    handle = ct.c_int64(0)
                    ret = self._dll.wcdb_open_account(
                        db_path.encode("utf-8"),
                        key_bytes,
                        ct.byref(handle),
                    )
                    if ret != 0:
                        logger.info(
                            "wcdb_open_account FAIL (ret=%d) fmt=%s path=%s source=%s",
                            ret, key_fmt, path_label, source_label,
                        )
                        last_ret, last_source = ret, source_label
                        continue

                    self._handle = handle.value

                    # Verify the key actually decrypts data
                    sessions = self.get_sessions()
                    if sessions:
                        session_count = (
                            len(sessions) if isinstance(sessions, list)
                            else len(sessions.get("sessions", sessions))
                        )
                        logger.info(
                            "Key WORKS (source=%s, fmt=%s, path=%s): %d sessions found",
                            source_label, key_fmt, path_label, session_count,
                        )
                        # Persist key for next cold start
                        _os.environ["WCDB_KEY"] = key_candidate
                        self._save_key_to_env(key_candidate)
                        logger.info("Database opened: %s", db_path)
                        self._load_nickname_cache()
                        return True

                    # Key didn't work — close and try next variant
                    logger.info(
                        "Key from %s (fmt=%s, path=%s) → 0 sessions",
                        source_label, key_fmt, path_label,
                    )
                    # The key opened the database but decrypted nothing —
                    # record it so the final message can tell this apart from
                    # "no key at all".
                    last_ret, last_source = -4, source_label
                    self._close_handle()

            logger.warning(
                "Key from %s failed all formats — trying next source...",
                source_label,
            )

        if tried_real_key:
            # A key IS configured, it just does not belong to this database.
            # Extraction will not help by itself — the stored key must be
            # replaced, which the same button does, so point at it while
            # naming the real cause instead of claiming the key is missing.
            logger.warning(
                "WCDB key rejected (source=%s, last_ret=%s) — the configured "
                "key does not decrypt %s",
                last_source or "env", last_ret, session_db,
            )
            raise RuntimeError(_key_failure_message(tried_real_key=True))

        raise RuntimeError(_key_failure_message(tried_real_key=False))

    @staticmethod
    def _save_key_to_env(key: str):
        """Persist a working WCDB key to .env for next cold start.

        Writes to resolve_env_file() (the canonical .env location), creating
        the file if missing, so the key lands exactly where config reads it.
        Uses config.write_env_file so a filesystem that refuses an atomic
        rename (UNC / VMware shared folder) still persists the key.
        """
        from src.config import resolve_env_file, write_env_file
        env_path = resolve_env_file()
        try:
            lines = (
                env_path.read_text(encoding="utf-8").splitlines()
                if env_path.exists()
                else []
            )
            new_lines = []
            found = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("WCDB_KEY=") or stripped.startswith("WCDB_KEY "):
                    new_lines.append(f"WCDB_KEY={key}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"WCDB_KEY={key}")
            how = write_env_file(env_path, "\n".join(new_lines) + "\n")
            logger.debug("Persisted WCDB_KEY to %s (%s write)", env_path, how)
        except Exception as e:
            logger.warning("Failed to persist WCDB_KEY to %s: %s", env_path, e)

    def _key_candidates(self):
        """Generate (key, label) pairs in priority order.

        The key is captured ONCE during onboarding (WeChat restart flow)
        and persisted to .env / WCDB_KEY.  Live extraction from an
        already-running WeChat is unreliable (the key was loaded at
        startup and the hook may miss it), so we don't try it here.

        As a last resort, tries an empty key — some wcdb_api.dll builds
        can derive the key internally via InitProtection.
        """
        import os as _os

        # Environment variable — persists across runs after onboarding
        env_key = _os.environ.get("WCDB_KEY", "").strip()
        if env_key and len(env_key) == 64:
            yield env_key, "env"

        # Fallback: let the DLL try its own internal key discovery
        yield "", "builtin"

    def _load_nickname_cache(self):
        """Load wxid -> display name mappings from sessions and contacts."""
        sessions = self.get_sessions()
        for s in sessions:
            username = s.get("username", "")
            display = (s.get("displayName") or s.get("nickname") or "").strip()
            if username and display:
                self._nicknames[username] = display

        # Load contacts (best-effort — DLL may not fully support this)
        try:
            contacts = self.get_contacts()
            for c in contacts:
                username = c.get("userName") or c.get("username") or ""
                nick = (c.get("nickName") or c.get("remark") or c.get("displayName") or "").strip()
                if username and nick:
                    self._nicknames[username] = nick
        except Exception as e:
            logger.warning("Failed to load contacts: %s", e)

        # Manual overrides from nicknames.json
        nick_file = Path("data/nicknames.json")
        if nick_file.exists():
            try:
                manual = json.loads(nick_file.read_text(encoding="utf-8"))
                for wxid, name in manual.items():
                    if wxid.startswith("_"):
                        continue
                    if name and name.strip():
                        self._nicknames[wxid] = name.strip()
                logger.info("Loaded %d manual nickname overrides", len(manual))
            except Exception as e:
                logger.warning("Failed to load nicknames.json: %s", e)

    # ── Query methods ─────────────────────────────────────────────────

    def _call_json(self, func, *args):
        """Call a WCDB function that returns a JSON string pointer."""
        out = ct.c_void_p()
        ret = func(*args, ct.byref(out))
        if ret != 0:
            logger.warning("WCDB call %s failed: ret=%d", func.__name__, ret)
            return None
        if not out.value:
            logger.debug("WCDB call %s returned null pointer", func.__name__)
            return {}
        # Free exactly once, and only after the buffer has been copied out.
        # Freeing again in an except branch corrupts the DLL heap and the next
        # allocation dies with an access violation, so keep the free in a
        # finally and do all parsing on the Python-owned copy.
        try:
            data = _read_gbk_string(out)
        finally:
            self._dll.wcdb_free_string(out)
        if not data:
            logger.warning("WCDB call %s returned an unreadable buffer",
                           func.__name__)
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error in %s: %s (%d bytes)",
                           func.__name__, e, len(data))
            return {}

    def get_sessions(self, limit=500):
        """Get all chat sessions with metadata."""
        result = self._call_json(self._dll.wcdb_get_sessions, self._handle)
        if result is None:
            logger.warning("wcdb_get_sessions returned None (DLL call failed)")
            return []
        if isinstance(result, list):
            logger.info("Got %d sessions (list)", len(result))
            return result
        if isinstance(result, dict):
            keys = list(result.keys())
            logger.info("Got sessions dict with keys: %s", keys)
            return result.get("sessions", result.get("data", []))
        logger.warning("wcdb_get_sessions returned unexpected type: %s", type(result))
        return []

    def get_messages(self, talker, limit=200, offset=0):
        """Get messages for a specific chat."""
        result = self._call_json(
            self._dll.wcdb_get_messages,
            self._handle,
            talker.encode("utf-8"),
            limit,
            offset,
        )
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("messages", result.get("data", []))
        return []

    def get_display_names(self, usernames):
        """Resolve wxids to display names."""
        if not self._handle or not usernames:
            return {}
        username_json = json.dumps(usernames, ensure_ascii=False).encode("utf-8")
        try:
            result = self._call_json(
                self._dll.wcdb_get_display_names,
                self._handle,
                username_json,
            )
            if isinstance(result, dict):
                return result.get("names", result)
            return {}
        except Exception as e:
            logger.warning(
                "wcdb_get_display_names failed: %s", e
            )
            return {}

    def get_contacts(self, keyword="", limit=1000):
        """Get contacts list."""
        if not self._dll.wcdb_get_contacts_compact:
            return []
        try:
            result = self._call_json(
                self._dll.wcdb_get_contacts_compact,
                self._handle,
                json.dumps([keyword], ensure_ascii=False).encode("utf-8"),
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("contacts", result.get("data", []))
            return []
        except Exception as e:
            logger.warning(
                "wcdb_get_contacts_compact failed — disabling: %s", e
            )
            self._dll.wcdb_get_contacts_compact = None
            return []

    def get_group_members(self, chat_id):
        """Get member list for a group chat. Returns list of {username, avatarUrl, ...}."""
        if not self._dll.wcdb_get_group_members:
            return []
        try:
            result = self._call_json(
                self._dll.wcdb_get_group_members,
                self._handle,
                chat_id.encode("utf-8"),
            )
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("members", result.get("data", []))
            return []
        except Exception as e:
            logger.warning(
                "wcdb_get_group_members failed: %s", e
            )
            return []

    def resolve_nickname(self, wxid):
        """Get display name for a wxid from cache."""
        if wxid in self._nicknames:
            return self._nicknames[wxid]
        # Try to look up
        names = self.get_display_names([wxid])
        if wxid in names:
            self._nicknames[wxid] = names[wxid]
            return names[wxid]
        self._nicknames[wxid] = wxid
        return wxid

    # ── Cleanup ───────────────────────────────────────────────────────

    def _close_handle(self):
        """Close current DB handle safely (no-op if already closed)."""
        if self._handle:
            try:
                wcdb_close = self._dll.wcdb_close_account
                wcdb_close.argtypes = [ct.c_int64]
                wcdb_close.restype = ct.c_int32
                wcdb_close(self._handle)
            except Exception:
                pass
            self._handle = 0

    def close(self):
        self._close_handle()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
