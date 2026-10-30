"""비밀값(API 키) 처리 회귀 테스트 — 네트워크 없이 돈다.

지키려는 것:
  1. 키 원문이 로그·화면·상태 응답 어디에도 나가지 않는다
  2. 공공데이터포털 Encoding 키를 넣어도 이중 인코딩으로 인증이 깨지지 않는다
  3. .env 를 고치면 서버 재시작 없이 반영된다
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import secrets
from app.sources import registry

RAW_DECODING_KEY = "aBcD1234+/=xyzQWERTY+abc/def=="
ENCODING_KEY = "aBcD1234%2B%2F%3DxyzQWERTY"


class TestDotenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.envf = Path(self.tmp.name) / ".env"
        self.patch = mock.patch.object(secrets, "ENV_FILE", self.envf)
        self.patch.start()
        secrets.reset_cache()

    def tearDown(self):
        self.patch.stop()
        secrets.reset_cache()
        self.tmp.cleanup()

    def write(self, text):
        self.envf.write_text(text, encoding="utf-8")

    def test_parses_plain_quoted_and_export(self):
        self.write('# 주석\n\nA=plain\nB="quoted"\nexport C=exported\nBAD_LINE\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(secrets.get("A"), "plain")
            self.assertEqual(secrets.get("B"), "quoted")
            self.assertEqual(secrets.get("C"), "exported")
            self.assertEqual(secrets.get("BAD_LINE"), "")

    def test_missing_file_is_not_an_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(secrets.get("ANY"), "")
            self.assertIsNone(secrets.source_of("ANY"))

    def test_env_var_wins_over_dotenv(self):
        self.write("K=from-dotenv\n")
        with mock.patch.dict(os.environ, {"K": "from-env"}, clear=True):
            self.assertEqual(secrets.get("K"), "from-env")
            self.assertEqual(secrets.source_of("K"), "환경변수")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(secrets.get("K"), "from-dotenv")
            self.assertEqual(secrets.source_of("K"), ".env")

    def test_dotenv_reloads_when_file_changes(self):
        """키를 받아 .env 에 넣는 흐름에서 재시작을 요구하면 불편하다."""
        self.write("K=before\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(secrets.get("K"), "before")
            time.sleep(0.02)          # mtime 이 달라지도록
            self.write("K=after\n")
            self.assertEqual(secrets.get("K"), "after")


class TestMasking(unittest.TestCase):
    def test_mask_never_contains_original(self):
        m = secrets.mask(RAW_DECODING_KEY)
        self.assertNotIn(RAW_DECODING_KEY, m)
        self.assertTrue(m.startswith("aBcD"))
        self.assertIn(f"{len(RAW_DECODING_KEY)}자", m)

    def test_mask_short_and_empty(self):
        self.assertEqual(secrets.mask(""), "")
        self.assertIn("*", secrets.mask("short"))
        self.assertNotIn("short", secrets.mask("short"))

    def test_status_does_not_carry_raw_key(self):
        with mock.patch.dict(os.environ, {"LH_API_KEY": RAW_DECODING_KEY},
                             clear=True):
            st = secrets.status("LH_API_KEY")
            self.assertTrue(st["set"])
            self.assertNotIn(RAW_DECODING_KEY, repr(st))

    def test_registry_describe_does_not_carry_raw_key(self):
        with mock.patch.dict(os.environ, {"LH_API_KEY": RAW_DECODING_KEY},
                             clear=True):
            d = registry.describe("lh", cfg_enabled=True)
            self.assertNotIn(RAW_DECODING_KEY, repr(d))
            self.assertTrue(d["has_key"])
            self.assertTrue(d["ready"])


class TestDoubleEncoding(unittest.TestCase):
    """공공데이터포털은 같은 키를 Encoding/Decoding 두 가지로 준다.

    httpx 가 쿼리를 알아서 인코딩하므로, 이미 인코딩된 Encoding 키를 그대로
    넘기면 '+' → '%2B' → '%252B' 로 이중 인코딩되어 인증이 깨진다.
    """

    def test_decoding_key_passes_through(self):
        self.assertFalse(secrets.looks_url_encoded(RAW_DECODING_KEY))
        self.assertEqual(secrets.normalize_api_key(RAW_DECODING_KEY),
                         RAW_DECODING_KEY)

    def test_encoding_key_is_decoded_once(self):
        self.assertTrue(secrets.looks_url_encoded(ENCODING_KEY))
        self.assertEqual(secrets.normalize_api_key(ENCODING_KEY),
                         "aBcD1234+/=xyzQWERTY")

    def test_double_encoded_key_is_recovered(self):
        dbl = "aBcD1234%252B%252F%253DxyzQWERTY"
        self.assertEqual(secrets.normalize_api_key(dbl), "aBcD1234+/=xyzQWERTY")

    def test_plain_key_untouched(self):
        self.assertEqual(secrets.normalize_api_key("plain-key"), "plain-key")
        self.assertEqual(secrets.normalize_api_key(""), "")

    def test_registry_api_key_returns_normalized(self):
        with mock.patch.dict(os.environ, {"LH_API_KEY": ENCODING_KEY},
                             clear=True):
            secrets.reset_cache()
            self.assertEqual(registry.api_key("lh"), "aBcD1234+/=xyzQWERTY")
        secrets.reset_cache()


class TestKeyGating(unittest.TestCase):
    def test_source_without_key_is_not_ready(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(secrets, "ENV_FILE",
                                   Path(tempfile.gettempdir()) / "__nope.env"):
                secrets.reset_cache()
                d = registry.describe("lh", cfg_enabled=True)
                self.assertFalse(d["ready"])
                self.assertFalse(d["has_key"])
                self.assertIn("LH_API_KEY", d["blocked_by"])
        secrets.reset_cache()

    def test_keyless_source_is_ready(self):
        d = registry.describe("applyhome", cfg_enabled=True)
        self.assertFalse(d["needs_key"])
        self.assertTrue(d["ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
