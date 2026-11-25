"""Configuration loading from .env file."""

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv


def _decode_wechat_groups(raw: str) -> str:
    """Decode URL-encoded group names from .env WECHAT_GROUPS value.

    We store each group name URL-encoded (via encodeURIComponent / urllib.parse.quote)
    so that commas, equals signs, and newlines in real group names don't break the
    .env format or our comma-separated delimiter.  This function reverses that encoding
    with a fallback: if decoding a chunk doesn't change it (or raises), the original
    is kept — for backward compatibility with old unencoded .env files.
    """
    if not raw or raw.strip() == "*":
        return raw.strip() if raw else "*"
    decoded = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            d = unquote(chunk)
            decoded.append(d)
        except Exception:
            decoded.append(chunk)
    return ",".join(decoded) if decoded else "*"


def _sanitize_display_name(name: str) -> str:
    """Remove only truly dangerous characters from a display name.

    This preserves the user's actual name (including quotes, braces, etc.)
    and relies on *usage-point escaping* (``repr()`` in logs, ``_esc()``
    in ``str.format()`` calls) to prevent injection at each call site.

    Only stripped:
    - Control characters (CR/LF → log-line injection)
    - Leading/trailing whitespace + quotes (almost certainly accidental)
    - Excessive length (> 128 chars)
    """
    if not name:
        return "群聊小助手"

    # 1. Strip control chars (except space) — prevents log-line injection
    name = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", name)

    # 2. Collapse whitespace and strip
    name = re.sub(r"\s+", " ", name).strip()

    # 3. Truncate to reasonable length
    if len(name) > 128:
        name = name[:128]

    # 4. Fallback
    if not name:
        return "群聊小助手"

    return name

def _resolve_project_root() -> Path:
    app_home = os.getenv("WEBOT_APP_HOME", "").strip()
    if app_home:
        return Path(app_home).expanduser().resolve()

    # In a PyInstaller EXE, __file__ resolves inside the temp extraction dir.
    # We want writable data (.env, data/*) in the EXE directory, not in the temp dir.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resolve_project_root()


def resolve_env_file() -> Path:
    """Return the canonical .env path — the single source of truth.

    Does NOT require the file to exist.  Every read AND write of .env must
    resolve through this function so config never splits across two files
    (the original persistence bug came from reads checking one directory
    while writes created .env in the current working directory).

    Precedence:
      1. WEBOT_ENV_FILE (explicit override, e.g. the macOS bundle)
      2. PROJECT_ROOT / ".env" (EXE dir when frozen, project root in dev)
    """
    explicit_env = os.getenv("WEBOT_ENV_FILE", "").strip()
    if explicit_env:
        return Path(explicit_env).expanduser().resolve()
    return PROJECT_ROOT / ".env"


def detect_wechat_data_dir() -> str:
    """Auto-detect the default WeChat data directory (parent of ``wxid_*``).

    Returns the base directory path, or empty string when nothing looks like
    a WeChat install.  This lives in ``config`` rather than in the web layer
    because both the UI *and* the media pipeline need the same answer: the
    UI promises "leave WECHAT_DATA_DIR empty and it is detected for you", and
    a pipeline that skipped the detection found no ``.dat`` file at all while
    the settings page showed a detected path.
    """
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files",
        Path(os.environ.get("USERPROFILE", "")) / "Documents" / "WeChat Files",
    ]
    for base in candidates:
        if not base.exists():
            continue
        try:
            wxid_dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("wxid_")]
            for wxid_dir in wxid_dirs:
                session_db = wxid_dir / "db_storage" / "session" / "session.db"
                if session_db.exists():
                    return str(base)
        except PermissionError:
            continue
    return ""


def resolve_wechat_data_dir(config) -> str:
    """Return the WeChat data directory to read attachments from.

    ``WECHAT_DATA_DIR`` wins when set; otherwise the auto-detected default is
    used, which is what the settings page has always promised.  Returns an
    empty string when neither yields anything — callers must then degrade to
    the ``[图片]`` placeholder rather than guess.
    """
    configured = str(getattr(config, "wechat_data_dir", "") or "").strip()
    if configured:
        return configured
    return detect_wechat_data_dir()


def _is_readable_file(path: Path) -> bool:
    """True when *path* is a readable regular file.

    ``Path.exists()`` can answer False for a file that really is there on a
    UNC / shared-folder path (VMware HGFS), so an actual ``open`` is tried
    before giving up.  This matters more than it looks: a false negative
    here silently sends the process to a different ``.env`` entirely.
    """
    try:
        if path.is_file():
            return True
    except OSError:
        pass
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def find_env_file() -> Path | None:
    """Return the .env path that actually exists, or None.

    The canonical location is resolve_env_file().  For backward
    compatibility with older installs whose .env landed in the working
    directory, a read-only fallback to CWD/.env is kept — but writes always
    go to resolve_env_file().
    """
    canonical = resolve_env_file()
    if _is_readable_file(canonical):
        return canonical

    legacy = Path.cwd() / ".env"
    if legacy != canonical and _is_readable_file(legacy):
        return legacy

    return None


def apply_env_file(env_path: Path | None = None) -> Path | None:
    """Reload a .env file into ``os.environ`` and return the path used.

    This is the single entry point for "the .env file changed on disk —
    make the running process see it".  ``load_config()`` reads only
    ``os.environ``, while the file is otherwise loaded exactly once at
    import time, so every writer of .env (onboarding, config save, WCDB
    key persistence) must call this afterwards.  Skipping it is what made
    a freshly completed onboarding still look unconfigured.

    Returns None when there is no file to load.
    """
    path = env_path or find_env_file()
    if path is None or not path.exists():
        return None
    try:
        load_dotenv(path, override=True)
    except OSError as e:
        # The file was written successfully — a read-back failure must not
        # fail the caller (the config is already on disk for the next start).
        import logging
        logging.getLogger(__name__).warning(
            "Failed to apply %s to os.environ: %s", path, e
        )
        return None
    return path


_env_path = find_env_file()

if _env_path:
    # .env is the user's explicit configuration, so its *non-empty* entries
    # must win over anything inherited from the OS environment.  The plain
    # ``load_dotenv(path)`` does not override, which let a stale system
    # variable silently win: ``MEDIA_VISION_ENABLED=true`` in .env was
    # ignored, the media pipeline never started, and every image stayed
    # ``[图片]`` while all other .env keys (API key, WCDB key) worked fine.
    #
    # Empty entries are skipped on purpose: they mean "auto-detect / use
    # the default", so an operator-provided key in the process environment
    # survives instead of being blanked out by ``KEY=`` in the file.
    import os as _os
    from dotenv import dotenv_values as _dotenv_values

    try:
        for _key, _value in _dotenv_values(_env_path).items():
            if _value is None or not str(_value).strip():
                continue
            _os.environ[_key] = str(_value)
    except OSError:
        load_dotenv(_env_path)
else:
    load_dotenv()

# Log which .env was loaded (helpful for debugging EXE packaging issues)
import logging as _logging
import threading as _threading
_log = _logging.getLogger(__name__)
if _env_path:
    _log.info("Loaded .env from: %s", _env_path)
else:
    _log.warning(
        ".env not found (canonical: %s). Using defaults.", resolve_env_file()
    )

#: The .env this process actually applied (None when none was found).
#: Exposed because the startup log runs before logging is configured, so
#: this line is normally lost — callers log it again once handlers exist,
#: which is the only way to tell "wrong file" apart from "wrong value".
ENV_FILE_USED: "Path | None" = _env_path


# Serializes .env writes across HTTP threads and background workers.
_env_write_lock = _threading.Lock()


def write_env_file(env_path: Path, content: str) -> str:
    """Write ``content`` to ``env_path``; return "atomic" or "direct".

    Prefers the atomic write-temp-then-rename pattern, but that pattern is
    not universally available: on network shares and shared folders
    (VMware HGFS ``\\\\vmware-host\\Shared Folders\\...``, SMB, some
    virtualized disks) ``os.replace`` onto an existing file is refused,
    which used to surface as a dropped HTTP connection and a bogus
    "无法连接服务器" in the UI.  When the rename fails we fall back to
    overwriting the file in place — a torn write is only possible if the
    process dies mid-write, which is strictly better than not saving at all.

    The temp file is always named after the *full* file name
    (``.env.tmp.<pid>.<tid>``) rather than via ``Path.with_suffix``, which
    would turn ``config.env`` into ``config.tmp`` and clobber an unrelated
    file.  Unique names keep concurrent writers from stepping on each other
    even before the lock is taken.

    Raises OSError when neither strategy can write the file.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = env_path.with_name(
        f"{env_path.name}.tmp.{os.getpid()}.{_threading.get_ident()}"
    )

    with _env_write_lock:
        replaced = False
        try:
            tmp_path.write_text(content, encoding="utf-8")
            try:
                os.replace(tmp_path, env_path)
                replaced = True
                return "atomic"
            except OSError as e:
                _log.warning(
                    "Atomic replace of %s failed (%s) — falling back to a "
                    "direct write. This is expected on network/shared folders "
                    "such as UNC paths and VMware shared folders.",
                    env_path, e,
                )
        finally:
            if not replaced:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

        # Rename unsupported here — overwrite in place instead.
        env_path.write_text(content, encoding="utf-8")
        return "direct"


@dataclass
class BotConfig:
    """All configuration for the WeChat summarizer bot."""

    # === AI Backend ===
    # "claude" | "deepseek" | "openai"
    ai_backend: str = "claude"

    # === Claude (Anthropic) ===
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    summarize_model: str = "claude-haiku-4-5-20251001"

    # === DeepSeek ===
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"

    # === OpenAI (compatible) ===
    # Any OpenAI-compatible provider: OpenAI / GLM (Zhipu) / Moonshot / Qwen ...
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # === WeChat Backend ===
    wechat_backend: str = "wcdb"
    # Comma-separated group names to monitor. "*" = auto-discover all groups.
    wechat_groups: str = "*"
    # Custom WeChat data directory. Leave empty to auto-detect from Documents.
    # Set to the parent directory containing wxid_* folders (e.g. D:\WeChatData).
    wechat_data_dir: str = ""
    # Which WeChat account directory to read.  Empty = pick the newest wxid_*
    # folder that has a session.db, which silently follows the wrong account
    # after a re-login creates a fresh folder.  Onboarding writes the detected
    # wxid here, and a pinned wxid that no longer exists is an error rather
    # than a quiet fallback to whatever else is on disk.
    wechat_wxid: str = ""

    # === Bot Identity ===
    bot_display_name: str = "群聊小助手"
    # Admin wxid (can manage nicknames and bot settings)
    admin_wxid: str = ""

    # === Trigger Keywords ===
    trigger_keywords: list[str] = field(default_factory=lambda: [
        "总结一下", "之前发了什么", "错过了什么", "summarize",
        "what did i miss", "聊天总结", "帮我总结", "前面说了什么",
        "说了啥", "发生了什么",
    ])

    # === Summarization ===
    summarize_enabled: bool = True
    fallback_window_hours: int = 8

    # === Feishu / Lark Export ===
    feishu_export_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # knowledge, spreadsheet, bitable, or docx
    feishu_export_mode: str = "knowledge"
    feishu_export_window_hours: int = 8
    feishu_auto_sync_enabled: bool = False
    feishu_auto_sync_min_messages: int = 20
    feishu_auto_sync_cooldown_sec: int = 1800
    feishu_knowledge_base_name: str = "webot 群聊沉淀"
    feishu_knowledge_folder_token: str = ""
    feishu_export_trigger_keywords: list[str] = field(default_factory=lambda: [
        "同步到飞书", "导出到飞书", "写到飞书", "沉淀到飞书",
    ])
    feishu_spreadsheet_token: str = ""
    feishu_spreadsheet_range: str = "Sheet1!A:H"
    feishu_bitable_app_token: str = ""
    feishu_bitable_table_id: str = ""
    feishu_doc_folder_token: str = ""

    # === Database ===
    db_path: str = "data/messages.db"

    # === Fun Features ===
    # @抽签 — draw a fortune lot (大吉/中吉/小吉/末吉/凶)
    fun_enabled: bool = True

    # === Proactive Participation ===
    # Master switch — enable autonomous chat participation without @mention
    proactive_enabled: bool = False
    # Rate thresholds for each mode (msgs/min).  When the message rate
    # exceeds a threshold, the bot enters that mode.  Calibrate these
    # by running:  python tools/analyze_chat_rhythm.py
    proactive_rate_window_sec: int = 120  # rate calculation window
    proactive_rate_quiet: float = 1.5     # SLEEP → QUIET  boundary
    proactive_rate_casual: float = 4.0    # QUIET → CASUAL boundary
    proactive_rate_lively: float = 6.5    # CASUAL → LIVELY boundary
    proactive_rate_burst: float = 8.5     # LIVELY → BURST  boundary

    # === Welcome New Member ===
    # Master switch — automatically send a welcome message when a new
    # member joins a group chat.  Welcome templates and per-group settings
    # are stored in data/welcome_templates.json.
    welcome_enabled: bool = False

    # === Sticky Mention ===
    # When a user sends @bot with no message text, enter sticky listening
    # mode.  The user's next message in the same group is treated as if it
    # were @mentioned (one-shot).  Set enabled=false to disable entirely.
    sticky_mention_enabled: bool = True
    sticky_mention_ttl_sec: int = 60  # max wait for follow-up message

    # === Group Todo ===
    # Master switch — @bot mention triggers todo management commands.
    todo_enabled: bool = True
    # Comma-separated group names where todo is active. "*" = all groups.
    todo_groups: list[str] = field(default_factory=lambda: ["*"])
    todo_max_per_group: int = 50           # max active todos per group (1-200)
    todo_completed_retention_days: int = 30  # auto-clean completed (0=forever)
    todo_deleted_retention_days: int = 30    # auto-clean deleted (0=forever)
    todo_add_keywords: list[str] = field(default_factory=lambda: [
        "记一下", "添加待办", "新建待办", "帮我记", "待办",
    ])
    todo_complete_keywords: list[str] = field(default_factory=lambda: [
        "搞定", "做完了", "完成", "完成了", "done",
    ])
    todo_delete_keywords: list[str] = field(default_factory=lambda: [
        "删掉", "删除", "取消", "不要了",
    ])

    # === Tuning ===
    poll_interval_sec: float = 1.0
    dedup_window_sec: int = 60
    max_messages_for_summary: int = 5000
    chunk_size: int = 400

    # === Logging ===
    log_level: str = "INFO"
    log_file: str = "data/bot.log"

    # === Voice Recognition ===
    # Master switch (default off — user must opt in)
    voice_asr_enabled: bool = False
    # "local_whisper" (free, offline) | "openai_whisper" (cloud, $0.006/min)
    voice_asr_backend: str = "local_whisper"
    voice_asr_language: str = "zh"
    # OpenAI Whisper API
    voice_openai_api_key: str = ""
    voice_openai_base_url: str = ""
    # Local Whisper model size: tiny / base / small / medium
    voice_local_model: str = "small"
    # Convert traditional Chinese → simplified (opencc t2s)
    voice_asr_to_simplified: bool = True

    # === Media (image / emoji / appmsg) ===
    # Master switch (default off — user must opt in)
    media_vision_enabled: bool = False
    # Vision model override. Empty = reuse the configured AI backend's model.
    media_vision_model: str = ""
    # Transcribe a shared video's own speech and send it along with the
    # frames.  Off by default: it costs CPU time on the reply path and a
    # several-hundred-megabyte model download on first use — neither should
    # happen without the operator asking.
    media_video_asr_enabled: bool = False
    # Local Whisper size for video speech.  Empty = reuse voice_local_model.
    # A clip only has to be understood roughly, so this may be smaller than
    # the voice-message model without hurting voice accuracy.
    media_video_asr_model: str = ""
    # Seconds of a clip's sound that may be transcribed.  0 = the built-in
    # budget in ``src.media.video``.  A clip longer than this is sampled in
    # evenly spaced slices, so the result is the whole storyline at a fixed
    # cost rather than only its opening.
    media_video_asr_max_sec: float = 0.0

    # === Shared links (appmsg / URLs in a question) ===
    # Fetch the page behind a link and put its text in the prompt.  Default on:
    # without it a chat model can only repeat the URL ("我打不开链接"), which was
    # the reported bug.  Off = the URL still reaches the prompt, but the bot
    # cannot read the article.
    link_fetch_enabled: bool = True
    # Run the page in a headless browser when plain HTTP gives an empty shell.
    # Sites that build the player (and the video address) in script expose
    # nothing to a static fetch; this is the only way to read them.  Default
    # on — it costs nothing unless a page comes back empty, and it degrades to
    # the static result when no Edge/Chrome is installed.  Off = never start a
    # browser; script-built pages then read as "打不开".
    link_render_enabled: bool = True
    # Wall clock the headless renderer may take, in seconds.  Bounded because
    # this runs on the reply path, after the static attempts already failed.
    link_render_timeout: float = 12.0

    # === Image decryption (localType=3 ``.dat`` containers) ===
    # WeChat encrypts received images per machine; these keys are not in the
    # message XML and cannot be derived, so they have to be supplied.  Both
    # default to empty, which means "no key" — images then degrade to the
    # ``[图片]`` placeholder rather than being half-decrypted into garbage.
    #
    # ``image_xor_key`` is a single byte, written as ``0x37`` (hex) or ``55``
    # (decimal).  ``image_aes_key`` is used as the *first 16 ASCII characters*
    # of the string, not hex-decoded — see src/media/dat_decrypt.py.
    image_xor_key: str = ""
    image_aes_key: str = ""
    # Where the keys come from: "env" (this config) or "file" (a JSON file at
    # data/image_keys.json, convenient for pasting a third-party extractor's
    # output without editing .env).  Keys already set here always win.
    image_key_provider: str = "env"


def _safe_float(raw: str, default: float, label: str) -> float:
    """Parse env value to float, raising RuntimeError with a friendly message on failure."""
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise RuntimeError(
            f"{label} 值 '{raw}' 不是有效的数字，请检查 .env 文件"
        ) from None


def _safe_int(raw: str, default: int, label: str) -> int:
    """Parse env value to int, raising RuntimeError with a friendly message on failure."""
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise RuntimeError(
            f"{label} 值 '{raw}' 不是有效的整数，请检查 .env 文件"
        ) from None


def _validate_config(kwargs: dict) -> None:
    """Validate numeric config values.  Prints clear errors and exits on bad values."""
    errors: list[str] = []

    # poll_interval_sec
    poll_interval_sec = kwargs.get("poll_interval_sec", 1.0)
    if poll_interval_sec < 0.1:
        errors.append(
            f"POLL_INTERVAL_SEC must be >= 0.1, got {poll_interval_sec}"
        )

    # chunk_size
    chunk_size = kwargs.get("chunk_size", 400)
    if not (10 <= chunk_size <= 1000):
        errors.append(
            f"CHUNK_SIZE must be between 10 and 1000, got {chunk_size}"
        )

    # max_messages_for_summary
    max_messages_for_summary = kwargs.get("max_messages_for_summary", 5000)
    if max_messages_for_summary < 10:
        errors.append(
            f"MAX_MESSAGES_FOR_SUMMARY must be >= 10, got {max_messages_for_summary}"
        )

    # fallback_window_hours
    fallback_window_hours = kwargs.get("fallback_window_hours", 8)
    if fallback_window_hours < 1:
        errors.append(
            f"FALLBACK_WINDOW_HOURS must be >= 1, got {fallback_window_hours}"
        )

    # dedup_window_sec
    dedup_window_sec = kwargs.get("dedup_window_sec", 60)
    if dedup_window_sec < 10:
        errors.append(
            f"DEDUP_WINDOW_SEC must be >= 10, got {dedup_window_sec}"
        )

    # sticky_mention_ttl_sec
    sticky_mention_ttl_sec = kwargs.get("sticky_mention_ttl_sec", 60)
    if not (10 <= sticky_mention_ttl_sec <= 300):
        errors.append(
            f"STICKY_MENTION_TTL_SEC must be between 10 and 300, "
            f"got {sticky_mention_ttl_sec}"
        )

    # proactive_rate_window_sec
    proactive_rate_window_sec = kwargs.get("proactive_rate_window_sec", 120)
    if proactive_rate_window_sec < 30:
        errors.append(
            f"PROACTIVE_RATE_WINDOW_SEC must be >= 30, got {proactive_rate_window_sec}"
        )

    # proactive_rate thresholds: all > 0 and in strict ascending order
    quiet = kwargs.get("proactive_rate_quiet", 1.5)
    casual = kwargs.get("proactive_rate_casual", 4.0)
    lively = kwargs.get("proactive_rate_lively", 6.5)
    burst = kwargs.get("proactive_rate_burst", 8.5)

    rate_names = ("quiet", "casual", "lively", "burst")
    rate_values = (quiet, casual, lively, burst)

    if any(v <= 0 for v in rate_values):
        errors.append(
            "All PROACTIVE_RATE_* values must be > 0, got: "
            + ", ".join(f"{n}={v}" for n, v in zip(rate_names, rate_values))
        )

    if not (quiet < casual < lively < burst):
        errors.append(
            "PROACTIVE_RATE_* values must be in strict ascending order "
            "(quiet < casual < lively < burst), got: "
            + ", ".join(f"{n}={v}" for n, v in zip(rate_names, rate_values))
        )

    # feishu_export_mode
    feishu_export_mode = kwargs.get("feishu_export_mode", "knowledge")
    if feishu_export_mode not in ("knowledge", "spreadsheet", "bitable", "docx"):
        errors.append(
            "FEISHU_EXPORT_MODE must be one of knowledge, spreadsheet, bitable, docx, "
            f"got {feishu_export_mode}"
        )

    # feishu_export_window_hours
    feishu_export_window_hours = kwargs.get("feishu_export_window_hours", 8)
    if not (1 <= feishu_export_window_hours <= 168):
        errors.append(
            "FEISHU_EXPORT_WINDOW_HOURS must be between 1 and 168, "
            f"got {feishu_export_window_hours}"
        )

    feishu_auto_sync_min_messages = kwargs.get("feishu_auto_sync_min_messages", 20)
    if not (1 <= feishu_auto_sync_min_messages <= 500):
        errors.append(
            "FEISHU_AUTO_SYNC_MIN_MESSAGES must be between 1 and 500, "
            f"got {feishu_auto_sync_min_messages}"
        )

    feishu_auto_sync_cooldown_sec = kwargs.get("feishu_auto_sync_cooldown_sec", 1800)
    if not (60 <= feishu_auto_sync_cooldown_sec <= 86400):
        errors.append(
            "FEISHU_AUTO_SYNC_COOLDOWN_SEC must be between 60 and 86400, "
            f"got {feishu_auto_sync_cooldown_sec}"
        )

    if errors:
        msg = "配置值无效:\n" + "\n".join(f"  - {err}" for err in errors)
        raise RuntimeError(msg)


def load_config() -> BotConfig:
    """Load configuration from environment variables.

    Returns a validated BotConfig instance.
    Raises RuntimeError if required configuration is missing.
    """
    ai_backend = os.getenv("AI_BACKEND", "claude").strip().lower()

    # An empty AI_BACKEND is never a legitimate choice: it means the .env was
    # never applied to this process (or was blanked).  Say so instead of
    # silently validating the "claude" fallback and blaming an Anthropic key
    # the user never configured.
    if not ai_backend:
        msg = (
            "AI_BACKEND 未设置（当前进程没有读到 AI 配置）。"
            "请到「系统配置 → AI 后端配置」保存一次，或重新走一遍引导页"
        )
        raise RuntimeError(msg)
    if ai_backend not in ("claude", "deepseek", "openai"):
        msg = (
            f"AI_BACKEND 值 '{ai_backend}' 无效，"
            "仅支持 claude / deepseek / openai"
        )
        raise RuntimeError(msg)

    # Validate required API keys based on selected backend
    if ai_backend == "deepseek":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            msg = "DEEPSEEK_API_KEY 未设置，请在 .env 文件中配置或通过引导页完成设置"
            raise RuntimeError(msg)
    elif ai_backend == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            msg = "OPENAI_API_KEY 未设置，请在 .env 文件中配置或通过引导页完成设置"
            raise RuntimeError(msg)
    else:
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            msg = "ANTHROPIC_API_KEY 未设置，请在 .env 文件中配置或通过引导页完成设置"
            raise RuntimeError(msg)

    # Parse trigger keywords from comma-separated string
    keywords_str = os.getenv("TRIGGER_KEYWORDS", "").strip()
    trigger_keywords = (
        [kw.strip() for kw in keywords_str.split(",") if kw.strip()]
        if keywords_str
        else None  # let the dataclass default apply
    )
    feishu_keywords_str = os.getenv("FEISHU_EXPORT_TRIGGER_KEYWORDS", "").strip()
    feishu_export_trigger_keywords = (
        [kw.strip() for kw in feishu_keywords_str.split(",") if kw.strip()]
        if feishu_keywords_str
        else None
    )
    todo_add_kw_str = os.getenv("TODO_ADD_KEYWORDS", "").strip()
    todo_add_keywords = (
        [kw.strip() for kw in todo_add_kw_str.split(",") if kw.strip()]
        if todo_add_kw_str else None
    )
    todo_complete_kw_str = os.getenv("TODO_COMPLETE_KEYWORDS", "").strip()
    todo_complete_keywords = (
        [kw.strip() for kw in todo_complete_kw_str.split(",") if kw.strip()]
        if todo_complete_kw_str else None
    )
    todo_delete_kw_str = os.getenv("TODO_DELETE_KEYWORDS", "").strip()
    todo_delete_keywords = (
        [kw.strip() for kw in todo_delete_kw_str.split(",") if kw.strip()]
        if todo_delete_kw_str else None
    )

    kwargs: dict = {
        "ai_backend": ai_backend,
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "").strip(),
        "anthropic_base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip(),
        "summarize_model": os.getenv("SUMMARIZE_MODEL", "claude-haiku-4-5-20251001").strip(),
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", "").strip(),
        "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        "openai_api_key": os.getenv("OPENAI_API_KEY", "").strip(),
        "openai_base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        # deepseek_model handled conditionally below (dataclass default)
        "wechat_backend": os.getenv("WECHAT_BACKEND", "wcdb").strip(),
        "wechat_groups": _decode_wechat_groups(os.getenv("WECHAT_GROUPS", "*")),
        "wechat_data_dir": os.getenv("WECHAT_DATA_DIR", "").strip(),
        "wechat_wxid": os.getenv("WECHAT_WXID", "").strip(),
        "bot_display_name": _sanitize_display_name(os.getenv("BOT_DISPLAY_NAME", "群聊小助手")),
        "admin_wxid": os.getenv("ADMIN_WXID", "").strip(),
        "db_path": os.getenv("DB_PATH", "data/messages.db").strip(),
        "poll_interval_sec": _safe_float(os.getenv("POLL_INTERVAL_SEC", "1.0"), 1.0, "POLL_INTERVAL_SEC"),
        "dedup_window_sec": _safe_int(os.getenv("DEDUP_WINDOW_SEC", "60"), 60, "DEDUP_WINDOW_SEC"),
        "max_messages_for_summary": _safe_int(os.getenv("MAX_MESSAGES_FOR_SUMMARY", "5000"), 5000, "MAX_MESSAGES_FOR_SUMMARY"),
        "chunk_size": _safe_int(os.getenv("CHUNK_SIZE", "400"), 400, "CHUNK_SIZE"),
        "fallback_window_hours": _safe_int(os.getenv("FALLBACK_WINDOW_HOURS", "8"), 8, "FALLBACK_WINDOW_HOURS"),
        "feishu_export_enabled": os.getenv("FEISHU_EXPORT_ENABLED", "false").strip().lower() == "true",
        "feishu_app_id": os.getenv("FEISHU_APP_ID", "").strip(),
        "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", "").strip(),
        "feishu_export_mode": os.getenv("FEISHU_EXPORT_MODE", "knowledge").strip().lower(),
        "feishu_export_window_hours": _safe_int(os.getenv("FEISHU_EXPORT_WINDOW_HOURS", "8"), 8, "FEISHU_EXPORT_WINDOW_HOURS"),
        "feishu_auto_sync_enabled": os.getenv("FEISHU_AUTO_SYNC_ENABLED", "false").strip().lower() == "true",
        "feishu_auto_sync_min_messages": _safe_int(os.getenv("FEISHU_AUTO_SYNC_MIN_MESSAGES", "20"), 20, "FEISHU_AUTO_SYNC_MIN_MESSAGES"),
        "feishu_auto_sync_cooldown_sec": _safe_int(os.getenv("FEISHU_AUTO_SYNC_COOLDOWN_SEC", "1800"), 1800, "FEISHU_AUTO_SYNC_COOLDOWN_SEC"),
        "feishu_knowledge_base_name": os.getenv("FEISHU_KNOWLEDGE_BASE_NAME", "webot 群聊沉淀").strip(),
        "feishu_knowledge_folder_token": os.getenv("FEISHU_KNOWLEDGE_FOLDER_TOKEN", "").strip(),
        "feishu_spreadsheet_token": os.getenv("FEISHU_SPREADSHEET_TOKEN", "").strip(),
        "feishu_spreadsheet_range": os.getenv("FEISHU_SPREADSHEET_RANGE", "Sheet1!A:H").strip(),
        "feishu_bitable_app_token": os.getenv("FEISHU_BITABLE_APP_TOKEN", "").strip(),
        "feishu_bitable_table_id": os.getenv("FEISHU_BITABLE_TABLE_ID", "").strip(),
        "feishu_doc_folder_token": os.getenv("FEISHU_DOC_FOLDER_TOKEN", "").strip(),
        "fun_enabled": os.getenv("FUN_ENABLED", "true").strip().lower() == "true",
        "summarize_enabled": os.getenv("SUMMARIZE_ENABLED", "true").strip().lower() == "true",
        "proactive_enabled": os.getenv("PROACTIVE_ENABLED", "false").strip().lower() == "true",
        # proactive_rate_window_sec handled conditionally below (dataclass default)
        "proactive_rate_quiet": _safe_float(os.getenv("PROACTIVE_RATE_QUIET", "1.5"), 1.5, "PROACTIVE_RATE_QUIET"),
        "proactive_rate_casual": _safe_float(os.getenv("PROACTIVE_RATE_CASUAL", "4.0"), 4.0, "PROACTIVE_RATE_CASUAL"),
        "proactive_rate_lively": _safe_float(os.getenv("PROACTIVE_RATE_LIVELY", "6.5"), 6.5, "PROACTIVE_RATE_LIVELY"),
        "proactive_rate_burst": _safe_float(os.getenv("PROACTIVE_RATE_BURST", "8.5"), 8.5, "PROACTIVE_RATE_BURST"),
        "welcome_enabled": os.getenv("WELCOME_ENABLED", "false").strip().lower() == "true",
        "sticky_mention_enabled": os.getenv("STICKY_MENTION_ENABLED", "true").strip().lower() == "true",
        "sticky_mention_ttl_sec": _safe_int(os.getenv("STICKY_MENTION_TTL_SEC", "60"), 60, "STICKY_MENTION_TTL_SEC"),
        # Group Todo
        "todo_enabled": os.getenv("TODO_ENABLED", "true").strip().lower() == "true",
        "todo_groups": [g.strip() for g in os.getenv("TODO_GROUPS", "*").split(",") if g.strip()],
        "todo_max_per_group": _safe_int(os.getenv("TODO_MAX_PER_GROUP", "50"), 50, "TODO_MAX_PER_GROUP"),
        "todo_completed_retention_days": _safe_int(os.getenv("TODO_COMPLETED_RETENTION_DAYS", "30"), 30, "TODO_COMPLETED_RETENTION_DAYS"),
        "todo_deleted_retention_days": _safe_int(os.getenv("TODO_DELETED_RETENTION_DAYS", "30"), 30, "TODO_DELETED_RETENTION_DAYS"),
        "log_level": os.getenv("LOG_LEVEL", "INFO").strip(),
        "log_file": os.getenv("LOG_FILE", "data/bot.log").strip(),
        # Voice recognition
        "voice_asr_enabled": os.getenv("VOICE_ASR_ENABLED", "false").strip().lower() == "true",
        "voice_asr_backend": os.getenv("VOICE_ASR_BACKEND", "local_whisper").strip(),
        "voice_asr_language": os.getenv("VOICE_ASR_LANGUAGE", "zh").strip(),
        "voice_openai_api_key": os.getenv("VOICE_OPENAI_API_KEY", "").strip(),
        "voice_openai_base_url": os.getenv("VOICE_OPENAI_BASE_URL", "").strip(),
        "voice_local_model": os.getenv("VOICE_LOCAL_MODEL", "small").strip(),
        "voice_asr_to_simplified": os.getenv("VOICE_ASR_TO_SIMPLIFIED", "true").strip().lower() == "true",
        # Media (image / emoji / appmsg)
        "media_vision_enabled": os.getenv("MEDIA_VISION_ENABLED", "false").strip().lower() == "true",
        "media_vision_model": os.getenv("MEDIA_VISION_MODEL", "").strip(),
        "media_video_asr_enabled": os.getenv("MEDIA_VIDEO_ASR_ENABLED", "false").strip().lower() == "true",
        "media_video_asr_model": os.getenv("MEDIA_VIDEO_ASR_MODEL", "").strip(),
        "media_video_asr_max_sec": _safe_float(
            os.getenv("MEDIA_VIDEO_ASR_MAX_SEC", "0"), 0.0, "MEDIA_VIDEO_ASR_MAX_SEC",
        ),
        # Shared links (fetch the page behind a URL in an @bot question)
        "link_fetch_enabled": os.getenv("LINK_FETCH_ENABLED", "true").strip().lower() == "true",
        # Headless render fallback for pages whose body/player is script-built
        "link_render_enabled": os.getenv("LINK_RENDER_ENABLED", "true").strip().lower() == "true",
        "link_render_timeout": _safe_float(
            os.getenv("LINK_RENDER_TIMEOUT", "12.0"), 12.0, "LINK_RENDER_TIMEOUT",
        ),
        # Image decryption keys (per machine, not derivable — see dat_decrypt.py)
        "image_xor_key": os.getenv("IMAGE_XOR_KEY", "").strip(),
        "image_aes_key": os.getenv("IMAGE_AES_KEY", "").strip(),
        "image_key_provider": os.getenv("IMAGE_KEY_PROVIDER", "env").strip().lower() or "env",
    }

    deepseek_model = os.getenv("DEEPSEEK_MODEL")
    if deepseek_model is not None and deepseek_model.strip():
        kwargs["deepseek_model"] = deepseek_model.strip()

    proactive_rate_window_sec = os.getenv("PROACTIVE_RATE_WINDOW_SEC")
    if proactive_rate_window_sec is not None and proactive_rate_window_sec.strip():
        kwargs["proactive_rate_window_sec"] = int(proactive_rate_window_sec)

    if trigger_keywords is not None:
        kwargs["trigger_keywords"] = trigger_keywords
    if feishu_export_trigger_keywords is not None:
        kwargs["feishu_export_trigger_keywords"] = feishu_export_trigger_keywords
    if todo_add_keywords is not None:
        kwargs["todo_add_keywords"] = todo_add_keywords
    if todo_complete_keywords is not None:
        kwargs["todo_complete_keywords"] = todo_complete_keywords
    if todo_delete_keywords is not None:
        kwargs["todo_delete_keywords"] = todo_delete_keywords

    _validate_config(kwargs)

    return BotConfig(**kwargs)


def is_onboarding_done() -> bool:
    """Check if onboarding has been completed without loading full config.

    Uses find_env_file() for consistent .env resolution.
    """
    env_path = find_env_file()
    if env_path and env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ONBOARDING_DONE="):
                return line.split("=", 1)[1].strip().strip("\"'").lower() == "true"
        return False  # .env exists but no ONBOARDING_DONE key
    return False  # No .env found
