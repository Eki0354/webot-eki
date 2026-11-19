"""Extract WCDB and image keys from WeChat using wx_key.dll.

Zero external dependencies — wx_key.dll is bundled with the EXE.

Two independent exports of the same DLL are used:

* ``InitializeHook`` / ``PollKeyData`` — the WCDB key, which requires the
  user to re-login so the key can be captured as WeChat loads it.
* ``GetImageKey`` — the per-account image keys, which need neither a hook
  nor a restart: the client keeps them readable while it runs.

Usage:
    python -m src.wechat.extract_key
"""
import ctypes as ct
from ctypes import wintypes
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

TH32CS_SNAPPROCESS = 0x00000002
kernel32 = ct.WinDLL("kernel32", use_last_error=True)

#: Buffer handed to ``GetImageKey``.  The real answer is ~200 bytes for two
#: accounts; the export rejects anything smaller than 0x100, and a generous
#: cap costs nothing but keeps a large account list from truncating.
IMAGE_KEY_BUFFER = 64 * 1024


class ImageKey(NamedTuple):
    """One account's image keys, as reported by ``GetImageKey``.

    ``aes_key_text`` is kept as the hex *text* WeChat returns, because that is
    what :func:`src.media.dat_decrypt.aes_key_from_text` expects — the first
    16 ASCII characters are the AES key, not the hex-decoded bytes.
    """

    wxid: str
    xor_key: Optional[int]
    aes_key_text: str


def _find_wechat_pid():
    """Find running WeChat process PID."""
    class PE(ct.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ct.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ct.c_wchar * 260),
        ]
    # Get our own PID to exclude webot.exe from results
    import os as _os
    my_pid = _os.getpid()

    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == -1:
        return None
    pe = PE()
    pe.dwSize = ct.sizeof(PE)
    if kernel32.Process32FirstW(h, ct.byref(pe)):
        while True:
            n = pe.szExeFile.lower()
            # Only match actual WeChat (Weixin.exe/WeChat.exe), not our own webot.exe
            if pe.th32ProcessID != my_pid and (n == "weixin.exe" or n == "wechat.exe"):
                kernel32.CloseHandle(h)
                return pe.th32ProcessID
            if not kernel32.Process32NextW(h, ct.byref(pe)):
                break
    kernel32.CloseHandle(h)
    return None


def _find_wx_key_dll():
    """Locate wx_key.dll: bundled native/windows/ first, then fallbacks."""
    import sys as _s
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "native" / "windows" / "wx_key.dll",
    ]
    if getattr(_s, "frozen", False):
        candidates.insert(0, Path(_s._MEIPASS) / "native" / "windows" / "wx_key.dll")
        candidates.insert(1, Path(_s.executable).resolve().parent / "native" / "windows" / "wx_key.dll")
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _verify_dll_loadable(dll_path: str) -> str | None:
    """Try to load wx_key.dll and return None if OK, or an error string."""
    dll_dir = os.path.dirname(dll_path)
    if dll_dir:
        try:
            os.add_dll_directory(dll_dir)
        except AttributeError:
            k32 = ct.WinDLL("kernel32", use_last_error=True)
            k32.SetDllDirectoryW(dll_dir)
    try:
        ct.WinDLL(dll_path)
        return None  # OK
    except OSError as e:
        err_code = ct.get_last_error()
        if err_code == 126:
            return (
                f"wx_key.dll 加载失败：缺少依赖库（错误码 126）。"
                f"请确认 native/windows/ 目录下的 VC++ 运行时 DLL 完整。\n"
                f"详情: {e}"
            )
        elif err_code == 193:
            return (
                f"wx_key.dll 加载失败：不是有效的 Win32/Win64 程序（错误码 193）。"
                f"请确认 DLL 架构与当前 Python 一致。\n详情: {e}"
            )
        else:
            return f"wx_key.dll 加载失败（错误码 {err_code}）: {e}"


def _log_console(msg):
    """Log a user-visible message (survives console=False in EXE).

    Uses the module logger — callers (e.g. web.server) should separately
    propagate status to the Web UI.
    """
    logger.warning(msg)


# ── Primary: wx_key.dll (zero-dependency key extraction) ─────────────

def extract_wcdb_key(require_restart: bool = True,
                     on_progress=None):
    """Extract WCDB key by asking user to re-login to WeChat.

    Args:
        require_restart: If True (default), ask user to restart WeChat
            when direct hooking fails. If False, return None immediately
            on direct hook failure.
        on_progress: Optional callback(phase, message) called at each
            stage change so callers can update their UI in real time.
            Phases: hooking, waiting_exit, waiting_login, hooking_restart.

    Returns 64-char hex key, or None on failure.
    """
    def _progress(phase, msg):
        logger.info("[%s] %s", phase, msg)
        if on_progress:
            try:
                on_progress(phase, msg)
            except Exception:
                pass

    dll_path = _find_wx_key_dll()
    if not dll_path:
        logger.error("wx_key.dll not found")
        return None

    # Pre-flight: verify the DLL actually loads before asking user
    # to restart WeChat.  Missing VC++ runtimes or architecture
    # mismatches are surfaced immediately rather than after a
    # confusing timeout.
    dll_err = _verify_dll_loadable(dll_path)
    if dll_err:
        logger.error(dll_err)
        raise RuntimeError(dll_err)

    pid = _find_wechat_pid()

    # ── Fast path: WeChat is running, try to hook directly ──────
    if pid:
        logger.info("检测到微信正在运行 (PID %d)，直接提取密钥...", pid)
        _progress("hooking", "检测到微信正在运行，尝试直接获取密钥...")
        time.sleep(1)
        key = _hook_and_poll(pid, dll_path, timeout=10)
        if key:
            return key
        if not require_restart:
            logger.warning(
                "直接提取密钥失败 — 微信已运行较久，密钥已在启动时加载完毕。"
                "请通过新用户引导流程重新提取密钥。"
            )
            return None
        logger.warning("直接提取失败，请重启微信...")

    if pid:
        # WeChat is running but direct hook failed — ask user to restart
        _progress("waiting_exit",
                  "直接提取失败，请右键任务栏微信图标选择「退出微信」，然后重新登录")
        logger.info("请退出微信并重新登录（右键托盘→退出微信）")
    else:
        # WeChat is not running at all — ask user to start it
        _progress("waiting_login",
                  "请启动微信并登录（首次启动更容易捕获密钥）")
        logger.info("微信未运行，等待用户启动微信...")

    # Phase 1: wait for WeChat to exit (if it was running)
    while _find_wechat_pid():
        time.sleep(1)

    if pid:
        # We were waiting for exit — now ask for login
        _progress("waiting_login", "微信已退出，请重新启动并登录微信")
        logger.info("微信已退出，等待重新登录...")

    # Phase 2: wait for WeChat to restart
    new_pid = None
    while not new_pid:
        time.sleep(0.5)
        new_pid = _find_wechat_pid()

    _progress("hooking_restart",
              f"检测到微信启动 (PID {new_pid})，正在安装 Hook...")
    logger.info("检测到微信启动 (PID %d)，安装 Hook...", new_pid)

    # Give WeChat a moment to initialize then hook
    time.sleep(2)

    return _hook_and_poll(new_pid, dll_path)


def _hook_and_poll(pid: int, dll_path: str, timeout=180):
    """Install hook on WeChat process and poll for key."""
    try:
        # Add the DLL's directory to the search path so Windows can
        # find its dependencies (VCRUNTIME140.dll etc.) which are
        # bundled alongside it.  Without this, LoadLibrary may fail
        # with "The specified module could not be found" (error 126)
        # even though the DLL file itself exists.
        dll_dir = os.path.dirname(dll_path)
        if dll_dir:
            try:
                os.add_dll_directory(dll_dir)
            except AttributeError:
                # Python < 3.8 — fall back to SetDllDirectoryW
                k32 = ct.WinDLL("kernel32", use_last_error=True)
                k32.SetDllDirectoryW(dll_dir)

        lib = ct.WinDLL(dll_path)
        lib.InitializeHook.argtypes = [wintypes.DWORD]
        lib.InitializeHook.restype = wintypes.BOOL
        lib.PollKeyData.argtypes = [ct.c_char_p, ct.c_int]
        lib.PollKeyData.restype = wintypes.BOOL
        lib.CleanupHook.argtypes = []
        lib.CleanupHook.restype = wintypes.BOOL

        if not lib.InitializeHook(wintypes.DWORD(pid)):
            err = ct.get_last_error()
            logger.error(
                "Hook 安装失败 — InitializeHook 返回 False"
                "（请确认微信版本兼容，错误码: %d）", err,
            )
            return None

        logger.info("Hook 已安装，等待微信加载数据...")

        buf = ct.create_string_buffer(128)
        deadline = time.monotonic() + timeout
        key = None

        while time.monotonic() < deadline:
            if lib.PollKeyData(buf, 128):
                data = bytes(buf)
                n = data.find(0)
                raw = data[:n] if n >= 0 else data
                key = raw.decode("utf-8", errors="replace").strip()
                if len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key):
                    logger.info("密钥捕获成功: %s...", key[:16])
                    break

            # PID check every 1s
            cur = _find_wechat_pid()
            if cur and cur != pid:
                logger.info("PID 变化 (%d→%d)，重新 Hook", pid, cur)
                pid = cur
                lib.InitializeHook(wintypes.DWORD(pid))

            time.sleep(0.1)

        lib.CleanupHook()

        if key:
            return key
        logger.error("未在 %ds 内捕获密钥 — PollKeyData 始终未返回有效数据", timeout)
        return None

    except OSError as e:
        # DLL load failures — missing dependencies, corrupted file, etc.
        logger.error("无法加载 wx_key.dll: %s（错误码: %d）", e, ct.get_last_error())
        return None
    except Exception as e:
        logger.error("Hook 失败: %s", e, exc_info=True)
        try:
            lib.CleanupHook()
        except Exception:
            pass
        return None


# ── Image keys: the GetImageKey export ───────────────────────────────

def extract_image_keys(timeout: float = 5.0) -> dict:
    """Return the per-account image keys reported by ``wx_key.dll``.

    ``GetImageKey`` is a second, independent export of the same DLL that
    already provides the WCDB key.  Unlike ``InitializeHook``/``PollKeyData``
    it needs no injection and no WeChat restart: it reads the running
    client's memory and returns a JSON document::

        {"accounts": [
            {"wxid": "wxid_abc", "keys": [
                {"code": 48897517, "xorKey": 237, "aesKey": "36a9357e83ccf4e9"}]}]}

    The keys are **per account**, so the caller must pick the entry matching
    the account that owns the ``.dat`` file — one account's ``aesKey`` cannot
    decrypt another's images (measured: 3808/3808 for the right pair, 0/1 for
    the wrong one).

    Args:
        timeout: Seconds to keep retrying.  WeChat loads its keys shortly
            after login, so a client that just started may answer with an
            empty account list for a moment.

    Returns:
        ``{wxid: ImageKey}``; empty when no key could be read (WeChat not
        running, or not logged in yet).

    Raises:
        RuntimeError: ``wx_key.dll`` is missing or cannot be loaded — a
            setup problem the caller should show to the user verbatim.
    """
    dll_path = _find_wx_key_dll()
    if not dll_path:
        raise RuntimeError("未找到 wx_key.dll，无法读取图片密钥")

    dll_err = _verify_dll_loadable(dll_path)
    if dll_err:
        raise RuntimeError(dll_err)

    lib = _load_wx_key_lib(dll_path)
    lib.GetImageKey.argtypes = [ct.c_char_p, ct.c_int]
    lib.GetImageKey.restype = wintypes.BOOL

    buf = ct.create_string_buffer(IMAGE_KEY_BUFFER)
    deadline = time.monotonic() + max(timeout, 0)

    while True:
        # Clear the buffer: a shorter answer than last time must not leave
        # the previous JSON tail behind and break parsing.
        ct.memset(buf, 0, IMAGE_KEY_BUFFER)
        ok = lib.GetImageKey(buf, IMAGE_KEY_BUFFER)
        raw = bytes(buf).split(b"\0", 1)[0]
        if ok and raw:
            keys = _parse_image_keys(raw)
            if keys:
                logger.info("读取到 %d 个账号的图片密钥", len(keys))
                return keys

        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)

    logger.warning(
        "未读取到图片密钥 — 请确认微信已登录（需要微信正在运行）"
    )
    return {}


def _load_wx_key_lib(dll_path: str):
    """Load ``wx_key.dll`` with its bundled dependencies on the search path."""
    dll_dir = os.path.dirname(dll_path)
    if dll_dir:
        try:
            os.add_dll_directory(dll_dir)
        except AttributeError:
            k32 = ct.WinDLL("kernel32", use_last_error=True)
            k32.SetDllDirectoryW(dll_dir)
    try:
        return ct.WinDLL(dll_path)
    except OSError as e:
        raise RuntimeError(
            f"无法加载 wx_key.dll: {e}（错误码: {ct.get_last_error()}）"
        ) from e


def _parse_image_keys(raw: bytes) -> dict:
    """Parse ``GetImageKey``'s JSON into ``{wxid: ImageKey}``.

    A malformed document is not an error worth raising: the caller retries
    and eventually reports "no keys", which is what the user needs to see.
    """
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        logger.warning("GetImageKey 返回了无法解析的内容: %r", raw[:120])
        return {}

    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list):
        return {}

    keys: dict = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        wxid = str(account.get("wxid") or "").strip()
        entries = account.get("keys")
        if not wxid or not isinstance(entries, list):
            continue
        # Several key generations can be listed; the first is the current one.
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            aes_key = str(entry.get("aesKey") or "").strip()
            xor_key = entry.get("xorKey")
            if isinstance(xor_key, bool) or not isinstance(xor_key, int):
                xor_key = None
            if aes_key or xor_key is not None:
                keys[wxid] = ImageKey(wxid, xor_key, aes_key)
                break
    return keys


# ── Compatibility API ────────────────────────────────────────────────

def extract_aes_key():
    """Extract WCDB key from running WeChat memory."""
    return extract_wcdb_key()


def decrypt_wcdb_key(aes_hex):
    """Return a plain hex key as-is if valid."""
    if not aes_hex:
        return None
    if len(aes_hex) == 64 and all(c in "0123456789abcdefABCDEF" for c in aes_hex):
        return aes_hex
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    k = extract_wcdb_key()
    if k:
        print(k)
    else:
        print("ERROR: Could not extract key.", file=sys.stderr)
        sys.exit(1)
