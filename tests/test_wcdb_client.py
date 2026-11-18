"""Tests for src.wechat.wcdb_client — key-source handling and error classes.

The interesting behaviour here is which failure the user is told about:
a missing key ("re-extract it") and a key that does not decrypt the
database ("this .env belongs to another machine") need different actions,
and they used to share one misleading message.
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

# src/ on the path so `import src.wechat...` works when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_client(tmpdir: Path, open_ret: int, sessions):
    """Build a WcdbNativeClient without touching the DLL or the real WeChat.

    ``open()`` only needs a config, a DLL whose wcdb_open_account returns a
    chosen code, and the two helpers that talk to native code.
    """
    from src.wechat.wcdb_client import WcdbNativeClient

    wxid = "wxid_testaccount_abcd"
    account = tmpdir / f"{wxid}_1234"
    (account / "db_storage" / "session").mkdir(parents=True)
    (account / "db_storage" / "session" / "session.db").write_bytes(b"")

    client = object.__new__(WcdbNativeClient)
    client._config = {"myWxid": wxid, "dbPath": str(tmpdir)}
    client._dll = MagicMock()
    client._dll.wcdb_open_account.return_value = open_ret
    client._handle = 0
    client._nicknames = {}
    client._load_nickname_cache = lambda: None
    client._close_handle = lambda: None
    client.get_sessions = lambda *a, **kw: sessions
    return client


class KeyFailureMessageTests(unittest.TestCase):
    """The message must name the real cause, keeping the UI's prefix."""

    def test_missing_key_message(self):
        from src.wechat.wcdb_client import _key_failure_message

        msg = _key_failure_message(tried_real_key=False)

        self.assertTrue(msg.startswith("KEY_MISSING:"))
        self.assertIn("密钥未配置", msg)

    def test_invalid_key_message(self):
        from src.wechat.wcdb_client import _key_failure_message

        msg = _key_failure_message(tried_real_key=True)

        self.assertTrue(msg.startswith("KEY_INVALID:"))
        # Must not claim the key is absent — the user has one, it is wrong.
        self.assertNotIn("密钥未配置", msg)
        self.assertIn("不匹配", msg)

    def test_prefixes_are_distinct(self):
        """The UI branches on these prefixes, so they must not collide."""
        from src.wechat.wcdb_client import _key_failure_message

        missing = _key_failure_message(tried_real_key=False)
        invalid = _key_failure_message(tried_real_key=True)

        self.assertNotEqual(missing, invalid)
        self.assertNotIn("KEY_INVALID", missing)
        self.assertNotIn("KEY_MISSING", invalid)


class OpenFailureClassificationTests(unittest.TestCase):
    """WcdbNativeClient.open() must raise the right error class."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rejected_key_raises_key_invalid(self):
        """A configured key that the DLL refuses → KEY_INVALID, not MISSING.

        This is the VMware/shared-folder case: .env holds the host's key,
        the VM's WeChat database needs a different one.
        """
        client = _make_client(self.tmpdir, open_ret=-3, sessions=[])

        with patch.dict("os.environ", {"WCDB_KEY": "a" * 64}):
            with self.assertRaises(RuntimeError) as ctx:
                client.open()

        self.assertTrue(str(ctx.exception).startswith("KEY_INVALID:"))

    def test_opened_but_no_sessions_raises_key_invalid(self):
        """Key accepted by the DLL but decrypts nothing → also KEY_INVALID."""
        client = _make_client(self.tmpdir, open_ret=0, sessions=[])

        with patch.dict("os.environ", {"WCDB_KEY": "b" * 64}):
            with self.assertRaises(RuntimeError) as ctx:
                client.open()

        self.assertTrue(str(ctx.exception).startswith("KEY_INVALID:"))

    def test_no_configured_key_raises_key_missing(self):
        """With no WCDB_KEY at all, the message must stay KEY_MISSING."""
        client = _make_client(self.tmpdir, open_ret=-2, sessions=[])

        env = {k: v for k, v in __import__("os").environ.items() if k != "WCDB_KEY"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                client.open()

        self.assertTrue(str(ctx.exception).startswith("KEY_MISSING:"))

    def test_short_key_is_not_treated_as_a_real_key(self):
        """_key_candidates only yields 64-char keys; a truncated one is ignored.

        A 63-char value must not make the failure look like "wrong key" —
        it was never a candidate in the first place.
        """
        from src.wechat.wcdb_client import WcdbNativeClient

        client = object.__new__(WcdbNativeClient)
        with patch.dict("os.environ", {"WCDB_KEY": "a" * 63}):
            candidates = list(client._key_candidates())

        self.assertEqual([label for _, label in candidates], ["builtin"])

    def test_successful_open_persists_key(self):
        """A working key is written back to .env for the next cold start."""
        client = _make_client(self.tmpdir, open_ret=0, sessions=[{"username": "x"}])
        key = "c" * 64
        env_file = self.tmpdir / ".env"

        with (
            patch.dict("os.environ", {"WCDB_KEY": key, "WEBOT_ENV_FILE": str(env_file)}),
            patch("src.wechat.wcdb_client.WcdbNativeClient._save_key_to_env") as save,
        ):
            self.assertTrue(client.open())

        save.assert_called_once_with(key)


if __name__ == "__main__":
    unittest.main()
