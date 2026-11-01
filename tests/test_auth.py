"""로그인·세션·배포 저장소 회귀 테스트 — 네트워크 없이 돈다.

외부 HTTPS 주소에 올리면 개인정보(생년·소득·자산)가 인터넷에 놓인다.
여기서 지키려는 것:

  1. 세션 쿠키를 **위조할 수 없다** — 서명이 틀리면 무조건 거부
  2. 비밀번호를 바꾸면 기존 세션이 전부 무효가 된다
  3. 만료된 세션은 통과하지 못한다
  4. 비밀번호 추측을 무한히 시도할 수 없다 (시도 제한)
  5. 비밀번호 없이 외부에 뜨는 사고를 기동 단계에서 막는다
  6. 영구 디스크를 붙이면(DATA_DIR) 캐시·내 조건이 그쪽에 쌓인다
  7. 폰에서 고친 '내 조건'이 환경변수 씨앗값에 덮이지 않는다
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import auth, profile, secrets, store

PW = "올바른-비밀번호-1234"


def _clean_env(**over):
    """환경변수를 비우고 필요한 것만 넣는다 (.env 영향도 끊는다)."""
    return mock.patch.dict(os.environ, over, clear=True)


class AuthBase(unittest.TestCase):
    def setUp(self):
        # .env 파일이 테스트에 끼어들지 않게 없는 경로로 돌린다
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(secrets, "ENV_FILE",
                                   Path(self.tmp.name) / "no.env")
        self.p.start()
        secrets.reset_cache()
        auth.reset_state()

    def tearDown(self):
        self.p.stop()
        secrets.reset_cache()
        auth.reset_state()
        self.tmp.cleanup()


# ============================================================ 세션 토큰
class TestSessionToken(AuthBase):
    def test_roundtrip(self):
        with _clean_env(APP_PASSWORD=PW):
            self.assertTrue(auth.verify_token(auth.make_token()))

    def test_rejects_garbage(self):
        with _clean_env(APP_PASSWORD=PW):
            for bad in (None, "", "그냥문자열", "1.2", "abc.def",
                        "9999999999", "9999999999."):
                self.assertFalse(auth.verify_token(bad), bad)

    def test_tampered_signature_rejected(self):
        """서명 한 글자만 바꿔도 통과하지 못한다."""
        with _clean_env(APP_PASSWORD=PW):
            tok = auth.make_token()
            payload, _, sig = tok.rpartition(".")
            flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
            self.assertFalse(auth.verify_token(f"{payload}.{flipped}"))

    def test_extended_expiry_rejected(self):
        """만료시각만 늘려 붙이는 수법 — 서명이 안 맞아 거부된다."""
        with _clean_env(APP_PASSWORD=PW):
            tok = auth.make_token()
            _, _, sig = tok.rpartition(".")
            forged = f"{int(time.time()) + 10_000_000}.{sig}"
            self.assertFalse(auth.verify_token(forged))

    def test_expired_token_rejected(self):
        with _clean_env(APP_PASSWORD=PW):
            self.assertFalse(auth.verify_token(auth.make_token(ttl_days=-1)))

    def test_password_change_invalidates_sessions(self):
        """비밀번호를 바꾸면 이미 나간 쿠키가 전부 죽는다."""
        with _clean_env(APP_PASSWORD=PW):
            tok = auth.make_token()
        with _clean_env(APP_PASSWORD="새-비밀번호-9999"):
            self.assertFalse(auth.verify_token(tok))

    def test_explicit_secret_is_used(self):
        """APP_SECRET 을 주면 비밀번호를 바꿔도 세션이 유지된다."""
        with _clean_env(APP_PASSWORD=PW, APP_SECRET="고정-서명키"):
            tok = auth.make_token()
        with _clean_env(APP_PASSWORD="다른-비밀번호", APP_SECRET="고정-서명키"):
            self.assertTrue(auth.verify_token(tok))


# ============================================================ 비밀번호
class TestPassword(AuthBase):
    def test_correct_and_incorrect(self):
        with _clean_env(APP_PASSWORD=PW):
            self.assertTrue(auth.check_password(PW))
            self.assertFalse(auth.check_password(PW + " "))
            self.assertFalse(auth.check_password(PW[:-1]))
            self.assertFalse(auth.check_password(""))
            self.assertFalse(auth.check_password(None))

    def test_no_password_means_nothing_passes(self):
        """비밀번호가 설정되지 않았으면 아무 값도 통과하지 못한다
        (로그인 자체가 꺼진 상태라 `enabled()` 가 False 다)."""
        with _clean_env():
            self.assertFalse(auth.enabled())
            self.assertFalse(auth.check_password(""))
            self.assertFalse(auth.check_password("아무거나"))

    def test_enabled_follows_password(self):
        with _clean_env(APP_PASSWORD=PW):
            self.assertTrue(auth.enabled())


# ============================================================ 시도 제한
class TestLockout(AuthBase):
    def test_locks_after_repeated_failures(self):
        ip = "203.0.113.9"
        self.assertEqual(auth.locked_out(ip), 0)
        for _ in range(auth.MAX_FAILURES - 1):
            auth.record_failure(ip)
        self.assertEqual(auth.locked_out(ip), 0, "한계 직전까지는 열려 있다")
        auth.record_failure(ip)
        self.assertGreater(auth.locked_out(ip), 0, "한계를 넘으면 잠긴다")

    def test_success_clears(self):
        ip = "203.0.113.10"
        for _ in range(auth.MAX_FAILURES):
            auth.record_failure(ip)
        auth.clear_failures(ip)
        self.assertEqual(auth.locked_out(ip), 0)

    def test_lock_is_per_ip(self):
        for _ in range(auth.MAX_FAILURES):
            auth.record_failure("198.51.100.1")
        self.assertGreater(auth.locked_out("198.51.100.1"), 0)
        self.assertEqual(auth.locked_out("198.51.100.2"), 0)

    def test_old_failures_expire(self):
        ip = "198.51.100.7"
        old = time.time() - auth.LOCKOUT_SECONDS - 5
        auth._failures[ip] = [old] * auth.MAX_FAILURES
        self.assertEqual(auth.locked_out(ip), 0)

    def test_client_ip_prefers_forwarded(self):
        self.assertEqual(
            auth.client_ip({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}), "1.2.3.4")
        self.assertEqual(auth.client_ip({"x-real-ip": "5.6.7.8"}), "5.6.7.8")
        self.assertEqual(auth.client_ip({}, fallback="9.9.9.9"), "9.9.9.9")


# ============================================================ 기동 점검
class TestStartupGuard(AuthBase):
    def test_refuses_to_start_without_password_when_required(self):
        """외부 배포(HR_REQUIRE_AUTH=1)인데 비밀번호가 없으면 아예 안 뜬다.

        개인정보가 열린 채 HTTPS 주소에 떠 있는 것보다 안 뜨는 게 낫다.
        """
        with _clean_env(HR_REQUIRE_AUTH="1"):
            with self.assertRaises(RuntimeError) as cm:
                auth.check_config()
            self.assertIn("APP_PASSWORD", str(cm.exception))

    def test_starts_when_password_present(self):
        with _clean_env(HR_REQUIRE_AUTH="1", APP_PASSWORD=PW):
            auth.check_config()        # 예외가 없어야 한다

    def test_local_mode_allows_no_password(self):
        with _clean_env():
            auth.check_config()        # 로컬은 그대로 쓸 수 있다


# ============================================================ 크론 토큰
class TestCronToken(AuthBase):
    def test_requires_configured_token(self):
        with _clean_env():
            self.assertFalse(auth.check_cron_token(""))
            self.assertFalse(auth.check_cron_token("아무거나"))

    def test_matches_only_exact(self):
        with _clean_env(CRON_TOKEN="크론-토큰-abc"):
            self.assertTrue(auth.check_cron_token("크론-토큰-abc"))
            self.assertFalse(auth.check_cron_token("크론-토큰-ab"))
            self.assertFalse(auth.check_cron_token(""))


# ============================================================ 쿠키 · 프록시
class TestCookieAndProxy(AuthBase):
    def test_secure_detected_behind_proxy(self):
        """Render 같은 호스팅은 TLS 를 앞단에서 끝내고 앱엔 http 로 넘긴다.
        그대로 믿으면 Secure 쿠키를 못 붙인다."""
        self.assertTrue(auth.is_secure_request("http", "https"))
        self.assertTrue(auth.is_secure_request("http", "https,http"))
        self.assertTrue(auth.is_secure_request("https", None))
        self.assertFalse(auth.is_secure_request("http", None))
        self.assertFalse(auth.is_secure_request("http", "http"))

    def test_cookie_flags(self):
        c = auth.cookie_kwargs(secure=True)
        self.assertTrue(c["httponly"])
        self.assertTrue(c["secure"])
        self.assertEqual(c["samesite"], "lax")
        self.assertEqual(c["path"], "/")
        self.assertFalse(auth.cookie_kwargs(secure=False)["secure"])


# ============================================================ 저장 위치
class TestDataDir(AuthBase):
    def test_data_dir_follows_env(self):
        """영구 디스크를 붙이면 DATA_DIR 하나로 캐시가 그쪽에 쌓인다."""
        with tempfile.TemporaryDirectory() as d:
            with _clean_env(DATA_DIR=d):
                self.assertEqual(store.data_dir(), Path(d))
                self.assertEqual(profile.profile_file(), Path(d) / "profile.json")

    def test_defaults_to_project_data(self):
        with _clean_env():
            self.assertEqual(store.data_dir(), store.ROOT / "data")
            self.assertEqual(profile.profile_file(),
                             profile.ROOT / "profile.json")

    def test_explicit_profile_file_wins(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "어디든" / "p.json"
            with _clean_env(DATA_DIR=d, PROFILE_FILE=str(target)):
                self.assertEqual(profile.profile_file(), target)


# ============================================================ 내 조건 영속성
class TestProfilePersistence(AuthBase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.file = Path(self.dir.name) / "profile.json"

    def tearDown(self):
        self.dir.cleanup()
        super().tearDown()

    def test_env_seed_used_when_no_file(self):
        """배포 직후·재시작 직후 — 환경변수 씨앗값으로 시작한다."""
        seed = json.dumps({"birth": "1990-01-01", "household_size": 3})
        with _clean_env(PROFILE_FILE=str(self.file), PROFILE_JSON=seed):
            prof = profile.load()
            self.assertEqual(prof["birth"], "1990-01-01")
            self.assertEqual(prof["household_size"], 3)
            self.assertTrue(profile.exists())

    def test_saved_file_beats_env_seed(self):
        """폰에서 고친 값이 환경변수 씨앗값에 덮이면 안 된다.

        예전 순서(환경변수 우선)였다면 저장해도 화면이 안 바뀌어서
        '저장이 안 된다'로 보였다.
        """
        seed = json.dumps({"birth": "1990-01-01", "household_size": 3})
        with _clean_env(PROFILE_FILE=str(self.file), PROFILE_JSON=seed):
            profile.save({"birth": "1995-12-25", "household_size": 1})
            prof = profile.load()
            self.assertEqual(prof["birth"], "1995-12-25")
            self.assertEqual(prof["household_size"], 1)

    def test_broken_env_json_does_not_crash(self):
        """환경변수가 깨져 있어도 서버는 떠야 한다 — 기본값으로 간다."""
        with _clean_env(PROFILE_FILE=str(self.file), PROFILE_JSON="{깨진 JSON"):
            self.assertEqual(profile.load()["household_size"],
                             profile.DEFAULT["household_size"])

    def test_export_roundtrips_into_env(self):
        """내보낸 값을 PROFILE_JSON 에 넣으면 같은 조건으로 복원된다 —
        무료 플랜에서 설정을 영구 보존하는 통로다."""
        with _clean_env(PROFILE_FILE=str(self.file)):
            profile.save({"birth": "1988-03-03", "household_size": 4,
                          "income_work": 320})
            exported = profile.export_env_value()
        self.assertNotIn("\n", exported, "환경변수에 넣으려면 한 줄이어야 한다")

        fresh = Path(self.dir.name) / "없는파일.json"
        with _clean_env(PROFILE_FILE=str(fresh), PROFILE_JSON=exported):
            prof = profile.load()
            self.assertEqual(prof["birth"], "1988-03-03")
            self.assertEqual(prof["household_size"], 4)
            self.assertEqual(prof["income_work"], 320)

    def test_save_is_atomic(self):
        """쓰다가 죽어도 반쪽짜리 파일이 남지 않는다 (tmp → replace)."""
        with _clean_env(PROFILE_FILE=str(self.file)):
            profile.save({"birth": "1991-06-06"})
            self.assertTrue(self.file.exists())
            self.assertFalse(self.file.with_suffix(".json.tmp").exists())
            json.loads(self.file.read_text(encoding="utf-8"))   # 온전한 JSON


if __name__ == "__main__":
    unittest.main()
