"""Tests for src.ai_profiles — named AI service profiles.

The behaviour worth pinning down is what happens around the secrets and the
single-source-of-truth rule:

- a masked key echoed back by the UI must never replace the stored key
- activating a profile rewrites .env without touching unrelated lines
- the active profile cannot be deleted, and a keyless one cannot be activated
- /api/config edits must flow into the active profile, or switching back
  would silently revert them
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_profiles import (
    SERVICE_PRESETS,
    AiProfileStore,
    is_masked,
    mask_key,
    preset_for,
    preset_for_base_url,
    profile_from_env,
)


DEEPSEEK_ENV = {
    "AI_BACKEND": "deepseek",
    "DEEPSEEK_API_KEY": "sk-deepseek-secret-123456",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-v4-flash",
}


class MaskingTests(unittest.TestCase):
    def test_mask_keeps_ends_and_hides_middle(self):
        self.assertEqual(mask_key("sk-1234567890abcdef"), "sk-1***cdef")

    def test_short_secret_is_fully_masked(self):
        self.assertEqual(mask_key("abc"), "***")
        self.assertEqual(mask_key(""), "")

    def test_is_masked_detects_round_trip_value(self):
        self.assertTrue(is_masked(mask_key("sk-1234567890abcdef")))
        self.assertFalse(is_masked("sk-1234567890abcdef"))
        self.assertFalse(is_masked(None))


class PresetTests(unittest.TestCase):
    def test_presets_cover_all_supported_backends(self):
        backends = {p["backend"] for p in SERVICE_PRESETS}
        self.assertEqual(backends, {"deepseek", "openai", "claude"})

    def test_custom_preset_uses_openai_backend(self):
        self.assertEqual(preset_for("custom")["backend"], "openai")
        self.assertEqual(preset_for("glm")["backend"], "openai")

    def test_lookup_by_base_url_tolerates_trailing_slash(self):
        self.assertEqual(preset_for_base_url("https://api.deepseek.com/")["id"], "deepseek")
        self.assertIsNone(preset_for_base_url("https://relay.example.com/v1"))
        self.assertIsNone(preset_for_base_url(""))


class ProfileFromEnvTests(unittest.TestCase):
    def test_deepseek_env_becomes_a_deepseek_profile(self):
        profile = profile_from_env(DEEPSEEK_ENV)
        self.assertEqual(profile["backend"], "deepseek")
        self.assertEqual(profile["service"], "deepseek")
        self.assertEqual(profile["model"], "deepseek-v4-flash")
        self.assertEqual(profile["api_key"], "sk-deepseek-secret-123456")

    def test_unknown_base_url_is_recorded_as_custom(self):
        profile = profile_from_env({
            "AI_BACKEND": "openai",
            "OPENAI_API_KEY": "sk-relay",
            "OPENAI_BASE_URL": "https://relay.example.com/v1",
            "OPENAI_MODEL": "my-private-alias",
        })
        self.assertEqual(profile["service"], "custom")
        self.assertEqual(profile["model"], "my-private-alias")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"
        self.env_path.write_text(
            "\n".join(f"{k}={v}" for k, v in DEEPSEEK_ENV.items()) + "\n",
            encoding="utf-8",
        )
        self.store = AiProfileStore(self.tmp / "ai_profiles.json")

    def test_first_snapshot_seeds_from_env_and_masks_key(self):
        snap = self.store.snapshot(DEEPSEEK_ENV)
        self.assertEqual(snap["active"], "default")
        self.assertEqual(len(snap["profiles"]), 1)
        self.assertNotIn("sk-deepseek-secret-123456", snap["profiles"][0]["api_key"])
        self.assertTrue(snap["profiles"][0]["has_api_key"])
        self.assertEqual(len(snap["presets"]), len(SERVICE_PRESETS))

    def test_upsert_creates_then_updates_in_place(self):
        created = self.store.upsert({
            "name": "GLM 便宜款", "service": "glm", "backend": "openai",
            "base_url": "https://open.bigmodel.cn/api/paas/v4/",
            "model": "glm-4-flash", "api_key": "glm-key-12345678",
        })
        again = self.store.upsert({
            "id": created["id"], "name": "GLM 改名",
            "service": "glm", "backend": "openai",
            "base_url": "https://open.bigmodel.cn/api/paas/v4/",
            "model": "glm-4-plus", "api_key": "glm-key-12345678",
        })
        self.assertEqual(again["id"], created["id"])
        self.assertEqual(self.store.snapshot()["profiles"].__len__(), 1)
        self.assertEqual(self.store.get(created["id"])["model"], "glm-4-plus")

    def test_masked_key_on_write_keeps_the_real_secret(self):
        created = self.store.upsert({
            "name": "relay", "service": "custom", "backend": "openai",
            "base_url": "https://relay.example.com/v1",
            "model": "alias", "api_key": "sk-relay-real-key-9876",
        })
        # Exactly what the settings page sends back: the mask it was shown.
        self.store.upsert({**created, "api_key": created["api_key"]})
        self.assertEqual(
            self.store.get(created["id"])["api_key"], "sk-relay-real-key-9876"
        )

    def test_missing_model_falls_back_to_the_preset_list(self):
        created = self.store.upsert({
            "name": "no model", "service": "deepseek", "backend": "deepseek",
            "base_url": "", "model": "", "api_key": "sk-x-12345678",
        })
        self.assertEqual(created["base_url"], "https://api.deepseek.com")
        self.assertEqual(created["model"], "deepseek-v4-flash")

    def test_activate_rewrites_only_its_own_env_lines(self):
        self.env_path.write_text(
            self.env_path.read_text(encoding="utf-8")
            + "BOT_DISPLAY_NAME=小助手\n",
            encoding="utf-8",
        )
        created = self.store.upsert({
            "name": "qwen", "service": "qwen", "backend": "openai",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus", "api_key": "sk-qwen-1234567890",
        })
        with patch("src.config.apply_env_file") as apply:
            self.store.activate(created["id"], self.env_path)
            apply.assert_called_once_with(self.env_path)

        written = self.env_path.read_text(encoding="utf-8")
        self.assertIn("AI_BACKEND=openai", written)
        self.assertIn("OPENAI_MODEL=qwen-plus", written)
        self.assertIn("OPENAI_API_KEY=sk-qwen-1234567890", written)
        # Unrelated configuration must survive the switch.
        self.assertIn("DEEPSEEK_API_KEY=sk-deepseek-secret-123456", written)
        self.assertIn("BOT_DISPLAY_NAME=小助手", written)
        self.assertEqual(self.store.snapshot()["active"], created["id"])

    def test_active_profile_cannot_be_deleted(self):
        snap = self.store.snapshot(DEEPSEEK_ENV)
        with self.assertRaises(ValueError):
            self.store.remove(snap["active"])

    def test_inactive_profile_can_be_deleted(self):
        self.store.snapshot(DEEPSEEK_ENV)
        created = self.store.upsert({
            "name": "tmp", "service": "openai", "backend": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key": "sk-x-1234567890",
        })
        self.assertTrue(self.store.remove(created["id"]))
        self.assertFalse(self.store.remove(created["id"]))

    def test_keyless_profile_cannot_be_activated(self):
        created = self.store.upsert({
            "name": "blank", "service": "openai", "backend": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key": "",
        })
        with self.assertRaises(ValueError):
            self.store.activate(created["id"], self.env_path)

    def test_unknown_profile_id_raises(self):
        self.store.snapshot(DEEPSEEK_ENV)
        with self.assertRaises(ValueError):
            self.store.activate("does-not-exist", self.env_path)

    def test_sync_active_from_env_picks_up_a_settings_page_edit(self):
        self.store.snapshot(DEEPSEEK_ENV)
        self.store.sync_active_from_env({**DEEPSEEK_ENV, "DEEPSEEK_MODEL": "deepseek-v4-pro"})
        active = self.store.active_profile()
        self.assertEqual(active["model"], "deepseek-v4-pro")

    def test_sync_follows_a_backend_change_from_outside(self):
        self.store.snapshot(DEEPSEEK_ENV)
        self.store.sync_active_from_env({
            "AI_BACKEND": "claude",
            "ANTHROPIC_API_KEY": "sk-ant-1234567890",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "SUMMARIZE_MODEL": "claude-haiku-4-5-20251001",
        })
        active = self.store.active_profile()
        self.assertEqual(active["backend"], "claude")
        self.assertEqual(active["service"], "claude")
        self.assertEqual(active["model"], "claude-haiku-4-5-20251001")

    def test_store_is_reloaded_from_disk(self):
        self.store.upsert({
            "name": "persisted", "service": "openai", "backend": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini", "api_key": "sk-persist-123456",
        })
        reopened = AiProfileStore(self.tmp / "ai_profiles.json")
        self.assertEqual(len(reopened.snapshot()["profiles"]), 1)

    def test_corrupt_file_does_not_crash_the_store(self):
        (self.tmp / "ai_profiles.json").write_text("{not json", encoding="utf-8")
        snap = self.store.snapshot(DEEPSEEK_ENV)
        self.assertEqual(len(snap["profiles"]), 1)
        self.assertEqual(snap["profiles"][0]["backend"], "deepseek")


class AllBackendsActivateTests(unittest.TestCase):
    """Each backend must land on its own .env keys, never another's."""

    def test_each_backend_writes_its_own_keys(self):
        cases = {
            "deepseek": ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"),
            "openai": ("OPENAI_MODEL", "OPENAI_API_KEY"),
            "claude": ("SUMMARIZE_MODEL", "ANTHROPIC_API_KEY"),
        }
        for backend, (model_key, key_key) in cases.items():
            with self.subTest(backend=backend):
                tmp = Path(tempfile.mkdtemp())
                env_path = tmp / ".env"
                env_path.write_text("AI_BACKEND=deepseek\n", encoding="utf-8")
                store = AiProfileStore(tmp / "profiles.json")
                created = store.upsert({
                    "name": backend, "service": backend, "backend": backend,
                    "base_url": "https://example.invalid/v1",
                    "model": f"{backend}-model", "api_key": f"key-{backend}-123456",
                })
                with patch("src.config.apply_env_file"):
                    store.activate(created["id"], env_path)
                written = env_path.read_text(encoding="utf-8")
                self.assertIn(f"AI_BACKEND={backend}", written)
                self.assertIn(f"{model_key}={backend}-model", written)
                self.assertIn(f"{key_key}=key-{backend}-123456", written)


if __name__ == "__main__":
    unittest.main()
