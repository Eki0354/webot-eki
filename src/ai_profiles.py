"""Named AI service profiles — multiple saved "provider + model + key" sets.

Why this exists: ``.env`` can only describe *one* AI configuration
(``AI_BACKEND`` plus the DeepSeek / OpenAI / Anthropic key, base URL and
model).  Users routinely want several: the cheap model for routine group
summaries, a stronger one for deep dives, and maybe a self-hosted
OpenAI-compatible endpoint.  This module stores those as named profiles and
activates one by writing it back to ``.env``.

Storage:

    data/ai_profiles.json   all saved profiles + which one is active
    .env                    still exactly one live configuration

Activation is therefore "write profile → .env, then reload it into
``os.environ``".  Everything downstream (``config.load_config``,
``summarize``, ``media.pipeline``) keeps reading ``.env`` exactly as before,
so there is never a second source of truth to reconcile.

Keys are stored in plain text, the same way ``.env`` already stores them —
the file lives next to ``.env`` under the same permissions.  The web API
masks them on the way out and treats a masked value coming back as
"unchanged", so a round trip through the UI cannot destroy a real secret.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path

from src.config import PROJECT_ROOT, write_env_file

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_PATH = PROJECT_ROOT / "data" / "ai_profiles.json"

#: ``provider`` values are the only ones ``config.load_config()`` accepts.
BACKENDS = ("deepseek", "openai", "claude")

#: Built-in service presets.  These drive the UI's "服务商" picker and the
#: offline model dropdown; ``backend`` is what gets written to ``AI_BACKEND``
#: (everything OpenAI-compatible shares the ``openai`` backend).
SERVICE_PRESETS = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "backend": "deepseek",
        "base_url": "https://api.deepseek.com",
        "key_hint": "在 platform.deepseek.com/api_keys 获取",
        "models": [
            {"id": "deepseek-v4-flash", "desc": "V4 Flash", "hint": "极速 · 极低费用"},
            {"id": "deepseek-v4-pro", "desc": "V4 Pro", "hint": "百万上下文 · 旗舰"},
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "backend": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_hint": "platform.openai.com 获取",
        "models": [
            {"id": "gpt-4o-mini", "desc": "GPT-4o mini", "hint": "便宜 · 日常够用"},
            {"id": "gpt-4o", "desc": "GPT-4o", "hint": "效果更好 · 更贵"},
            {"id": "gpt-4.1-mini", "desc": "GPT-4.1 mini", "hint": "新一代轻量模型"},
            {"id": "o4-mini", "desc": "o4-mini", "hint": "推理模型 · 较慢"},
        ],
    },
    {
        "id": "glm",
        "label": "智谱 GLM",
        "backend": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "key_hint": "bigmodel.cn 获取",
        "models": [
            {"id": "glm-4-flash", "desc": "GLM-4-Flash", "hint": "免费额度 · 快"},
            {"id": "glm-4-plus", "desc": "GLM-4-Plus", "hint": "效果更好"},
            {"id": "glm-4-air", "desc": "GLM-4-Air", "hint": "性价比"},
        ],
    },
    {
        "id": "moonshot",
        "label": "Moonshot 月之暗面",
        "backend": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "key_hint": "platform.moonshot.cn 获取",
        "models": [
            {"id": "moonshot-v1-8k", "desc": "Moonshot v1 8K", "hint": "短上下文"},
            {"id": "moonshot-v1-32k", "desc": "Moonshot v1 32K", "hint": "中等上下文"},
            {"id": "moonshot-v1-128k", "desc": "Moonshot v1 128K", "hint": "长上下文"},
            {"id": "kimi-k2-0711-preview", "desc": "Kimi K2", "hint": "新旗舰"},
        ],
    },
    {
        "id": "qwen",
        "label": "通义千问 Qwen",
        "backend": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_hint": "dashscope.console.aliyun.com 获取",
        "models": [
            {"id": "qwen-plus", "desc": "Qwen-Plus", "hint": "均衡"},
            {"id": "qwen-turbo", "desc": "Qwen-Turbo", "hint": "快 · 便宜"},
            {"id": "qwen-max", "desc": "Qwen-Max", "hint": "效果最好"},
        ],
    },
    {
        "id": "ollama",
        "label": "本地 Ollama / vLLM",
        "backend": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "key_hint": "本地服务通常随便填（如 sk-local）",
        "models": [
            {"id": "qwen2.5:7b", "desc": "Qwen2.5 7B", "hint": "常见本地模型"},
            {"id": "llama3.1:8b", "desc": "Llama 3.1 8B", "hint": "通用"},
        ],
    },
    {
        "id": "claude",
        "label": "Claude (Anthropic)",
        "backend": "claude",
        "base_url": "https://api.anthropic.com",
        "key_hint": "console.anthropic.com 获取",
        "models": [
            {"id": "claude-haiku-4-5-20251001", "desc": "Haiku 4.5", "hint": "快速 · 低成本"},
            {"id": "claude-sonnet-4-5-20250929", "desc": "Sonnet 4.5", "hint": "高质量"},
            {"id": "claude-sonnet-4-6", "desc": "Sonnet 4.6", "hint": "高质量 · 推荐"},
        ],
    },
    {
        "id": "custom",
        "label": "自定义（OpenAI 兼容）",
        "backend": "openai",
        "base_url": "",
        "key_hint": "任何 OpenAI 兼容服务或中转站",
        "models": [],
    },
]

#: provider → the .env keys that carry its credentials and model.
PROVIDER_ENV_KEYS = {
    "deepseek": {
        "api_key": "DEEPSEEK_API_KEY",
        "base_url": "DEEPSEEK_BASE_URL",
        "model": "DEEPSEEK_MODEL",
        "default_base_url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-flash",
    },
    "openai": {
        "api_key": "OPENAI_API_KEY",
        "base_url": "OPENAI_BASE_URL",
        "model": "OPENAI_MODEL",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "claude": {
        "api_key": "ANTHROPIC_API_KEY",
        "base_url": "ANTHROPIC_BASE_URL",
        "model": "SUMMARIZE_MODEL",
        "default_base_url": "https://api.anthropic.com",
        "default_model": "claude-haiku-4-5-20251001",
    },
}

MASK_MARK = "***"


def preset_for(service_id: str) -> dict | None:
    """Return the built-in preset with *service_id*, or None."""
    for preset in SERVICE_PRESETS:
        if preset["id"] == service_id:
            return preset
    return None


def preset_for_base_url(base_url: str) -> dict | None:
    """Best-effort match of a base URL back to a built-in preset.

    Used when a profile has no ``service`` recorded (profiles migrated from
    ``.env``) so the UI can still show the right model list.
    """
    target = (base_url or "").strip().rstrip("/")
    if not target:
        return None
    for preset in SERVICE_PRESETS:
        base = (preset.get("base_url") or "").strip().rstrip("/")
        if base and base == target:
            return preset
    return None


def mask_key(value: str) -> str:
    """Mask a secret for display: first 4 + last 4, or ``***`` when short."""
    if not value:
        return ""
    if len(value) <= 8:
        return MASK_MARK
    return value[:4] + MASK_MARK + value[-4:]


def is_masked(value) -> bool:
    """True when *value* looks like a masked key rather than a real one."""
    return isinstance(value, str) and MASK_MARK in value


def _normalize_profile(raw: dict) -> dict:
    """Coerce one on-disk profile into the canonical shape."""
    backend = str(raw.get("backend") or raw.get("provider") or "openai").lower()
    if backend not in BACKENDS:
        backend = "openai"
    service = str(raw.get("service") or "").strip()
    if not service:
        matched = preset_for_base_url(raw.get("base_url", ""))
        service = matched["id"] if matched else ("custom" if backend == "openai" else backend)
    preset = preset_for(service)
    env_keys = PROVIDER_ENV_KEYS[backend]
    base_url = str(raw.get("base_url") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not base_url:
        base_url = (preset or {}).get("base_url") or env_keys["default_base_url"]
    if not model:
        models = (preset or {}).get("models") or []
        model = models[0]["id"] if models else env_keys["default_model"]
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "name": str(raw.get("name") or "未命名配置").strip() or "未命名配置",
        "service": service,
        "backend": backend,
        "base_url": base_url,
        "model": model,
        "api_key": str(raw.get("api_key") or ""),
    }


def profile_from_env(env_raw: dict[str, str]) -> dict:
    """Build a profile out of the currently effective ``.env`` values."""
    backend = str(env_raw.get("AI_BACKEND") or "deepseek").strip().lower()
    if backend not in BACKENDS:
        backend = "deepseek"
    env_keys = PROVIDER_ENV_KEYS[backend]
    base_url = env_raw.get(env_keys["base_url"]) or env_keys["default_base_url"]
    model = env_raw.get(env_keys["model"]) or env_keys["default_model"]
    preset = preset_for_base_url(base_url)
    service = preset["id"] if preset else ("custom" if backend == "openai" else backend)
    return _normalize_profile({
        "id": "default",
        "name": f"{preset['label'] if preset else backend} 配置",
        "service": service,
        "backend": backend,
        "base_url": base_url,
        "model": model,
        "api_key": env_raw.get(env_keys["api_key"], ""),
    })


def _public(profile: dict) -> dict:
    """Profile shape safe to hand to the browser (secret masked)."""
    out = dict(profile)
    out["api_key"] = mask_key(profile.get("api_key", ""))
    out["has_api_key"] = bool(profile.get("api_key"))
    return out


class AiProfileStore:
    """Thread-safe store for named AI service profiles.

    The file is small and read on every config load, so no caching: a stale
    in-memory copy would show up as "I edited it but the UI still says the
    old value".
    """

    def __init__(self, path: Path | str = DEFAULT_PROFILES_PATH) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    # ── Load / save ───────────────────────────────────────────────

    def _load_raw(self) -> dict:
        if not self._path.exists():
            return {"version": 1, "active": "", "profiles": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s — starting empty", self._path)
            return {"version": 1, "active": "", "profiles": []}
        if not isinstance(data, dict):
            return {"version": 1, "active": "", "profiles": []}
        profiles = [
            _normalize_profile(p) for p in data.get("profiles", []) if isinstance(p, dict)
        ]
        return {
            "version": 1,
            "active": str(data.get("active") or ""),
            "profiles": profiles,
        }

    def _save_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # ── Queries ───────────────────────────────────────────────────

    def _seed_if_empty(self, data: dict, env_raw: dict[str, str] | None) -> bool:
        """Seed one profile from *env_raw* when the store is still empty.

        Every entry point that reasons about "the active profile" has to do
        this first, or a request that arrives before the first list would
        see an empty store and silently allow deleting the profile that is
        actually running.
        """
        if data["profiles"]:
            return False
        if env_raw is None:
            from src.config import find_env_file
            env_path = find_env_file()
            env_raw = _read_env_map(env_path) if env_path else {}
        seeded = profile_from_env(env_raw)
        data["profiles"] = [seeded]
        data["active"] = seeded["id"]
        return True

    def snapshot(self, env_raw: dict[str, str] | None = None) -> dict:
        """Return profiles (masked) + which one is active.

        When nothing has been saved yet, one profile is seeded from *env_raw*
        so the UI opens on the configuration that is actually running
        instead of an empty list.
        """
        with self._lock:
            data = self._load_raw()
            if self._seed_if_empty(data, env_raw):
                self._save_raw(data)
            active = data["active"]
            ids = {p["id"] for p in data["profiles"]}
            if active not in ids:
                active = data["profiles"][0]["id"]
                data["active"] = active
                self._save_raw(data)
            return {
                "active": active,
                "profiles": [_public(p) for p in data["profiles"]],
                "presets": SERVICE_PRESETS,
            }

    def get(self, profile_id: str) -> dict | None:
        """Return the raw (unmasked) profile with *profile_id*."""
        with self._lock:
            for profile in self._load_raw()["profiles"]:
                if profile["id"] == profile_id:
                    return profile
        return None

    def active_profile(self) -> dict | None:
        with self._lock:
            data = self._load_raw()
            for profile in data["profiles"]:
                if profile["id"] == data["active"]:
                    return profile
        return None

    # ── Mutations ─────────────────────────────────────────────────

    def upsert(self, payload: dict) -> dict:
        """Create or update a profile.  Returns the masked saved copy.

        A masked ``api_key`` (the UI echoing back what it was shown) means
        "leave the stored key alone" — otherwise every save from the settings
        page would overwrite a real secret with ``sk-a***wxyz``.
        """
        with self._lock:
            data = self._load_raw()
            profile_id = str(payload.get("id") or "").strip()
            existing = next(
                (p for p in data["profiles"] if p["id"] == profile_id), None
            )
            cleaned = {
                "id": profile_id or uuid.uuid4().hex[:12],
                "name": payload.get("name") or (existing or {}).get("name") or "未命名配置",
                "service": payload.get("service") or (existing or {}).get("service") or "",
                "backend": payload.get("backend") or (existing or {}).get("backend") or "openai",
                "base_url": payload.get("base_url"),
                "model": payload.get("model"),
            }
            incoming_key = payload.get("api_key")
            if incoming_key is None or is_masked(incoming_key):
                cleaned["api_key"] = (existing or {}).get("api_key", "")
            else:
                cleaned["api_key"] = str(incoming_key)
            normalized = _normalize_profile(cleaned)

            replaced = False
            for idx, profile in enumerate(data["profiles"]):
                if profile["id"] == normalized["id"]:
                    data["profiles"][idx] = normalized
                    replaced = True
                    break
            if not replaced:
                data["profiles"].append(normalized)
            if not data["active"]:
                data["active"] = normalized["id"]
            self._save_raw(data)
            return _public(normalized)

    def remove(self, profile_id: str) -> bool:
        """Delete a profile.  The active one cannot be removed."""
        with self._lock:
            data = self._load_raw()
            # A delete can arrive before the first GET (two clicks in a
            # fresh install).  Seed in memory only: nothing is being
            # added, so there is nothing to persist unless the delete
            # itself succeeds.
            self._seed_if_empty(data, None)
            if profile_id == data["active"]:
                raise ValueError("不能删除当前正在使用的配置，请先切换到其他配置")
            before = len(data["profiles"])
            data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
            if len(data["profiles"]) == before:
                return False
            self._save_raw(data)
            return True

    # ── Activation ────────────────────────────────────────────────

    def activate(self, profile_id: str, env_path: Path) -> dict:
        """Write the profile to ``.env`` and reload it into the process.

        Raises ValueError when the profile does not exist or has no API key —
        activating a keyless profile would make the bot refuse to start on
        the next launch, which is far worse than an immediate error here.
        """
        with self._lock:
            data = self._load_raw()
            profile = next(
                (p for p in data["profiles"] if p["id"] == profile_id), None
            )
            if profile is None:
                raise ValueError(f"配置不存在: {profile_id}")
            if not profile.get("api_key"):
                raise ValueError("该配置还没有填写 API Key，无法启用")

            backend = profile["backend"]
            env_keys = PROVIDER_ENV_KEYS[backend]
            updates = {
                "AI_BACKEND": backend,
                env_keys["api_key"]: profile["api_key"],
                env_keys["base_url"]: profile["base_url"],
                env_keys["model"]: profile["model"],
            }
            _write_env_updates(env_path, updates)
            data["active"] = profile_id
            self._save_raw(data)

        from src.config import apply_env_file
        apply_env_file(env_path)
        logger.info(
            "Activated AI profile %r (backend=%s, model=%s)",
            profile["name"], backend, profile["model"],
        )
        return _public(profile)

    # ── Drift control ─────────────────────────────────────────────

    def sync_active_from_env(self, env_raw: dict[str, str]) -> None:
        """Make the active profile match ``.env`` after an outside edit.

        ``/api/config`` still writes ``.env`` directly, so the active profile
        has to follow it — otherwise switching away and back would silently
        revert the user's edit.
        """
        with self._lock:
            data = self._load_raw()
            if not data["profiles"]:
                seeded = profile_from_env(env_raw)
                data["profiles"] = [seeded]
                data["active"] = seeded["id"]
                self._save_raw(data)
                return
            active = next(
                (p for p in data["profiles"] if p["id"] == data["active"]), None
            )
            if active is None:
                return
            env_keys = PROVIDER_ENV_KEYS[active["backend"]]
            incoming_backend = (env_raw.get("AI_BACKEND") or "").strip().lower()
            if incoming_backend in BACKENDS and incoming_backend != active["backend"]:
                # Backend changed from outside: reseed from .env wholesale.
                data["active"] = ""
                seeded = profile_from_env(env_raw)
                seeded["id"] = active["id"]
                seeded["name"] = active["name"]
                for idx, profile in enumerate(data["profiles"]):
                    if profile["id"] == active["id"]:
                        data["profiles"][idx] = seeded
                        break
                data["active"] = seeded["id"]
                self._save_raw(data)
                return
            for field, env_key in (
                ("base_url", env_keys["base_url"]),
                ("model", env_keys["model"]),
                ("api_key", env_keys["api_key"]),
            ):
                value = (env_raw.get(env_key) or "").strip()
                if value:
                    active[field] = value
            self._save_raw(data)


def _read_env_map(env_path) -> dict[str, str]:
    """Parse a .env file into a plain key→value dict (comments ignored)."""
    raw: dict[str, str] = {}
    if not env_path or not Path(env_path).exists():
        return raw
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            raw[key.strip()] = value.strip()
    return raw


def _write_env_updates(env_path: Path, updates: dict[str, str]) -> None:
    """Set several keys in ``.env`` at once, preserving unrelated lines."""
    env_path = Path(env_path)
    lines = (
        env_path.read_text(encoding="utf-8").splitlines()
        if env_path.exists()
        else []
    )
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    write_env_file(env_path, "\n".join(new_lines) + "\n")


_store: AiProfileStore | None = None
_store_lock = threading.Lock()


def get_ai_profile_store() -> AiProfileStore:
    """Return the process-wide profile store."""
    global _store
    with _store_lock:
        if _store is None:
            _store = AiProfileStore()
        return _store
