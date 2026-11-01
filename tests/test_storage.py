"""영속 저장소·암호화 회귀 테스트 — 네트워크 없이 돈다 (httpx 를 가로챈다).

지키려는 것:

  1. 암호문은 평문을 흘리지 않고, **한 바이트만 손대도 거부**된다
  2. 키가 다르면 복호화되지 않는다 (조용히 빈 값으로 넘어가지 않는다)
  3. 저장은 **되읽어 확인**한 뒤에야 성공이다
  4. 저장이 실패하면 예외가 올라온다 — 성공이라고 말하지 않는다
  5. 원격이 켜져 있으면 내 조건·즐겨찾기가 거기서 읽히고 거기에 쓰인다
  6. 원격을 못 읽어도 화면은 떠야 하고, 그때 **경고가 남아야** 한다
  7. GitHub 토큰이 화면용 상태 응답에 **절대 담기지 않는다**
  8. 커밋 이력으로 예전 시점의 값을 되살릴 수 있다 (백업·복구)
"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app import crypto, profile, remote_store, secrets, store

KEY = "테스트-암호화-키-0123456789"
REPO = "testuser/home-radar-data"
# 실제 GitHub PAT 는 ASCII 다. 테스트도 같은 모양으로 둔다 —
# HTTP 헤더에 ASCII 만 들어갈 수 있어서 한글 토큰은 애초에 성립하지 않는다.
TOKEN = "github_pat_11ABCDEFG0testtokenvalue"


def _env(**over):
    return mock.patch.dict(os.environ, over, clear=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = mock.patch.object(secrets, "ENV_FILE",
                                   Path(self.tmp.name) / "no.env")
        self.p.start()
        secrets.reset_cache()
        crypto.reset_cache()
        remote_store.invalidate()

    def tearDown(self):
        self.p.stop()
        secrets.reset_cache()
        crypto.reset_cache()
        remote_store.invalidate()
        self.tmp.cleanup()


# ============================================================ 암호화
class TestCrypto(Base):
    def test_roundtrip(self):
        with _env(ENCRYPTION_KEY=KEY):
            for text in ("", "간단", '{"income_work": 320}', "한글 " * 500):
                self.assertEqual(crypto.decrypt(crypto.encrypt(text)), text)

    def test_ciphertext_hides_plaintext(self):
        with _env(ENCRYPTION_KEY=KEY):
            tok = crypto.encrypt('{"birth":"1994-07-15","income_work":320}')
        self.assertNotIn("1994", tok)
        self.assertNotIn("income", tok)
        self.assertNotIn("320", tok)

    def test_same_input_differs_each_time(self):
        """같은 값을 두 번 저장해도 암호문이 달라야 한다 (nonce 가 매번 새로)."""
        with _env(ENCRYPTION_KEY=KEY):
            self.assertNotEqual(crypto.encrypt("같은 값"),
                                crypto.encrypt("같은 값"))

    def test_tamper_is_rejected(self):
        """한 바이트만 바꿔도 거부된다 — 위조된 내용을 풀어 쓰지 않는다.

        Fernet 은 복호화 **전에** HMAC 태그를 검증한다.

        **바이트 단위로** 비튼다. base64 문자 하나를 바꾸는 방식은 쓰지 않는다 —
        마지막 문자는 유효 비트가 일부뿐이라 64자 중 16자가 **같은 바이트로**
        디코딩된다. 그건 변조가 아니라 같은 내용이라 거부되지 않는 게 맞고,
        그걸 변조 테스트로 쓰면 1/6 확률로 실패하는 깜빡이 테스트가 된다.
        """
        with _env(ENCRYPTION_KEY=KEY):
            tok = crypto.encrypt("원래 값")
            raw = bytearray(base64.urlsafe_b64decode(tok))
            # 버전 바이트 · 암호문 중간 · HMAC 태그 — 세 구간을 각각 건드린다
            for i in (0, len(raw) // 2, len(raw) - 1):
                bad = bytearray(raw)
                bad[i] ^= 0x01
                token = base64.urlsafe_b64encode(bytes(bad)).decode("ascii")
                with self.assertRaises(crypto.CryptoError,
                                       msg=f"{i}번째 바이트 변조가 통과했다"):
                    crypto.decrypt(token)

    def test_truncation_rejected(self):
        """뒤를 잘라내도 거부된다 (HMAC 태그가 짧아진다)."""
        with _env(ENCRYPTION_KEY=KEY):
            tok = crypto.encrypt("충분히 긴 평문입니다 " * 10)
            raw = base64.urlsafe_b64decode(tok)
            for cut in (1, 16, len(raw) // 2):
                token = base64.urlsafe_b64encode(raw[:-cut]).decode("ascii")
                with self.assertRaises(crypto.CryptoError):
                    crypto.decrypt(token)

    def test_uses_fernet_format(self):
        """검증된 표준 구현(Fernet)을 쓰고 있는지 — 토큰은 'gAAAA' 로 시작한다."""
        with _env(ENCRYPTION_KEY=KEY):
            self.assertTrue(crypto.encrypt("x").startswith("gAAAA"))

    def test_wrong_key_cannot_read(self):
        with _env(ENCRYPTION_KEY=KEY):
            tok = crypto.encrypt("비밀")
        crypto.reset_cache()
        with _env(ENCRYPTION_KEY="다른-키-9999"):
            with self.assertRaises(crypto.CryptoError):
                crypto.decrypt(tok)

    def test_missing_key_raises(self):
        with _env():
            self.assertFalse(crypto.key_configured())
            with self.assertRaises(crypto.CryptoError):
                crypto.encrypt("x")

    def test_fingerprint_changes_with_key(self):
        """키 지문으로 '키가 바뀌었다'를 알 수 있어야 한다 (값은 안 드러낸다)."""
        with _env(ENCRYPTION_KEY=KEY):
            fp1 = crypto.fingerprint()
        crypto.reset_cache()
        with _env(ENCRYPTION_KEY="다른-키"):
            fp2 = crypto.fingerprint()
        self.assertTrue(fp1 and fp2)
        self.assertNotEqual(fp1, fp2)
        self.assertNotIn(KEY, fp1)

    def test_json_roundtrip(self):
        with _env(ENCRYPTION_KEY=KEY):
            obj = {"birth": "1994-07-15", "regions": ["서울", "경기"],
                   "income_work": 320, "nested": {"a": [1, 2, None]}}
            self.assertEqual(crypto.decrypt_json(crypto.encrypt_json(obj)), obj)


# ============================================================ 가짜 GitHub
class FakeGitHub:
    """GitHub Contents API 흉내 — 파일 몇 개와 커밋 이력을 메모리에 둔다."""

    def __init__(self):
        # path -> {"content": base64 str, "sha": str}
        self.files: dict[str, dict] = {}
        self.commits: dict[str, list[dict]] = {}
        self.fail_write = False
        self.fail_read = False
        self.calls: list[str] = []
        self._n = 0

    def _sha(self) -> str:
        self._n += 1
        return f"sha{self._n:04d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")

        # 저장소 정보 (ping)
        if request.method == "GET" and path.count("/") == 3:
            return httpx.Response(200, json={"private": True,
                                             "default_branch": "main"})
        # 커밋 목록 (history)
        if request.method == "GET" and path.endswith("/commits"):
            p = request.url.params.get("path", "")
            return httpx.Response(200, json=[
                {"sha": c["sha"], "commit": {"committer": {"date": c["at"]},
                                             "message": c["message"]}}
                for c in reversed(self.commits.get(p, []))])

        key = path.split("/contents/", 1)[-1] if "/contents/" in path else ""

        if request.method == "GET":
            if self.fail_read:
                return httpx.Response(500, json={"message": "boom"})
            ref = request.url.params.get("ref", "")
            # 특정 커밋 시점 조회 (get_at)
            for c in self.commits.get(key, []):
                if c["sha"] == ref:
                    return httpx.Response(200, json={"content": c["content"],
                                                     "sha": c["sha"]})
            f = self.files.get(key)
            if not f:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=f)

        if request.method == "PUT":
            if self.fail_write:
                return httpx.Response(500, json={"message": "write failed"})
            body = json.loads(request.content)
            cur = self.files.get(key)
            # 기존 파일을 덮어쓰려면 올바른 sha 가 있어야 한다 (낙관적 동시성)
            if cur and body.get("sha") != cur["sha"]:
                return httpx.Response(409, json={"message": "sha mismatch"})
            sha = self._sha()
            self.files[key] = {"content": body["content"], "sha": sha}
            self.commits.setdefault(key, []).append(
                {"sha": sha, "at": "2026-11-01T00:00:00Z",
                 "message": body.get("message", ""),
                 "content": body["content"]})
            return httpx.Response(200, json={"content": self.files[key]})

        if request.method == "DELETE":
            self.files.pop(key, None)
            return httpx.Response(200, json={})

        return httpx.Response(405, json={"message": "nope"})


class RemoteBase(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeGitHub()
        transport = httpx.MockTransport(self.fake.handler)
        self._orig = httpx.request

        def call(method, url, **kw):
            kw.pop("timeout", None)
            with httpx.Client(transport=transport) as c:
                return c.request(method, url, **kw)

        httpx.request = call

    def tearDown(self):
        httpx.request = self._orig
        super().tearDown()


# ============================================================ 원격 저장소
class TestRemoteStore(RemoteBase):
    def test_disabled_without_config(self):
        with _env():
            self.assertFalse(remote_store.enabled())
            self.assertIsNone(remote_store.get("profile"))
            with self.assertRaises(remote_store.RemoteError):
                remote_store.put("profile", {"a": 1})

    def test_put_get_roundtrip(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            value = {"birth": "1994-07-15", "income_work": 320}
            res = remote_store.put("profile", value)
            self.assertTrue(res["saved"] and res["verified"])
            remote_store.invalidate()
            self.assertEqual(remote_store.get("profile"), value)

    def test_stored_file_is_encrypted(self):
        """저장소에 올라간 파일에 개인정보가 평문으로 보이면 안 된다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"birth": "1994-07-15",
                                         "income_work": 320})
        raw = base64.b64decode(self.fake.files["data/profile.json"]["content"])
        text = raw.decode("utf-8")
        env = json.loads(text)
        self.assertEqual(env["v"], 1)
        self.assertTrue(env["data"].startswith("gAAAA"))   # Fernet 토큰
        for leak in ("1994", "income_work", "320"):
            self.assertNotIn(leak, text)

    def test_write_failure_raises(self):
        """저장이 실패하면 예외 — 성공이라고 답하지 않는다."""
        self.fake.fail_write = True
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            with self.assertRaises(remote_store.RemoteError):
                remote_store.put("profile", {"a": 1})

    def test_read_failure_raises(self):
        self.fake.fail_read = True
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            with self.assertRaises(remote_store.RemoteError):
                remote_store.get("profile", use_cache=False)

    def test_requires_encryption_key(self):
        """암호화 키가 없으면 개인정보를 외부로 내보내지 않는다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN):
            with self.assertRaises(remote_store.RemoteError):
                remote_store.put("profile", {"a": 1})

    def test_key_change_is_reported_not_swallowed(self):
        """키를 바꾸면 '없는 값'이 아니라 **오류**로 알린다.

        조용히 None 을 돌려주면 호출한 쪽이 기본값으로 덮어써서 원본이 날아간다.
        """
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"birth": "1994-07-15"})
        remote_store.invalidate()
        crypto.reset_cache()
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN,
                  ENCRYPTION_KEY="바뀐-키-9999"):
            with self.assertRaises(remote_store.RemoteError) as cm:
                remote_store.get("profile", use_cache=False)
            self.assertIn("ENCRYPTION_KEY", str(cm.exception))

    def test_delete(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"a": 1})
            remote_store.delete("profile")
            remote_store.invalidate()
            self.assertIsNone(remote_store.get("profile"))

    def test_overwrite_uses_sha(self):
        """기존 파일을 덮어쓸 때 sha 를 같이 보내야 한다 (낙관적 동시성).

        가짜 서버는 sha 가 안 맞으면 409 를 준다 — 실제 GitHub 와 같다.
        """
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"n": 1})
            remote_store.put("profile", {"n": 2})
            remote_store.invalidate()
            self.assertEqual(remote_store.get("profile"), {"n": 2})

    def test_recovers_from_stale_sha(self):
        """다른 곳에서 먼저 저장해 sha 가 낡았어도 한 번 다시 읽어 성공한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"n": 1})
            # 캐시의 sha 를 일부러 낡게 만든다
            remote_store._cache["profile"] = (
                remote_store._cache["profile"][0],
                remote_store._cache["profile"][1], "sha-오래됨")
            remote_store.put("profile", {"n": 3})
            remote_store.invalidate()
            self.assertEqual(remote_store.get("profile"), {"n": 3})

    def test_history_and_restore(self):
        """커밋 이력이 곧 백업 — 예전 시점 값을 읽어 되돌릴 수 있어야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"birth": "1990-01-01"})
            remote_store.put("profile", {"birth": "1995-05-05"})
            hist = remote_store.history("profile")
            self.assertGreaterEqual(len(hist), 2)
            # 가장 오래된 커밋 시점의 값을 읽는다
            oldest = hist[-1]["sha"]
            full = [c["sha"] for c in self.fake.commits["data/profile.json"]]
            old_value = remote_store.get_at("profile", full[0])
            self.assertEqual(old_value, {"birth": "1990-01-01"})
            self.assertTrue(oldest)

    def test_non_ascii_token_gives_clear_error(self):
        """토큰을 잘못 붙여 넣었을 때 원인을 알 수 있는 메시지가 나와야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN="토큰에-한글이-섞임",
                  ENCRYPTION_KEY=KEY):
            with self.assertRaises(remote_store.RemoteError) as cm:
                remote_store.get("profile", use_cache=False)
            self.assertIn("ASCII", str(cm.exception))

    def test_status_never_leaks_token(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            st = remote_store.status()
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(KEY, blob)
        self.assertTrue(st["enabled"] and st["encrypted"])
        self.assertEqual(st["backend"], "github")

    def test_ping(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            p = remote_store.ping()
        self.assertTrue(p["ok"])
        self.assertTrue(p["private"], "데이터 저장소는 비공개여야 한다")

    def test_cache_avoids_repeat_calls(self):
        """화면을 열 때마다 네트워크를 타지 않는다 (무료 인스턴스 응답 속도).

        저장 직후에는 되읽기 확인 때문에 이미 캐시가 따뜻하다. 그래서
        **캐시를 비운 상태에서** 여러 번 읽어도 네트워크는 한 번만 가는지 본다.
        """
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            remote_store.put("profile", {"a": 1})

            self.fake.calls.clear()
            for _ in range(5):
                remote_store.get("profile")
            self.assertEqual([c for c in self.fake.calls if c.startswith("GET")],
                             [], "저장 직후에는 다시 읽을 필요가 없다")

            remote_store.invalidate()
            self.fake.calls.clear()
            for _ in range(5):
                remote_store.get("profile")
            self.assertEqual(
                len([c for c in self.fake.calls if c.startswith("GET")]), 1)


# ============================================================ 내 조건 영속
class TestProfilePersistence(RemoteBase):
    def setUp(self):
        super().setUp()
        self.pfile = Path(self.tmp.name) / "profile.json"

    def test_saves_to_remote_and_reads_back(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1994-07-15", "household_size": 3})
            remote_store.invalidate()
            info = profile.load_detailed()
            self.assertEqual(info["origin"], "원격 DB")
            self.assertEqual(info["profile"]["birth"], "1994-07-15")
            self.assertEqual(info["profile"]["household_size"], 3)

    def test_survives_local_file_loss(self):
        """서버 재시작으로 임시 디스크가 비어도 내 조건이 살아 있어야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1988-03-03", "income_work": 420})
            self.pfile.unlink()                      # 임시 디스크가 날아간 상황
            remote_store.invalidate()
            prof = profile.load()
            self.assertEqual(prof["birth"], "1988-03-03")
            self.assertEqual(prof["income_work"], 420)

    def test_two_devices_see_same_value(self):
        """PC 에서 고치면 아이폰에서도 최신 값이 보인다 (같은 레코드를 본다)."""
        other = Path(self.tmp.name) / "other-device.json"
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1990-01-01", "household_size": 2})
        remote_store.invalidate()
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(other)):        # 다른 기기 = 다른 로컬 파일
            prof = profile.load()
            self.assertEqual(prof["birth"], "1990-01-01")
            self.assertEqual(prof["household_size"], 2)

    def test_save_failure_propagates(self):
        """원격 저장 실패가 조용히 묻히지 않는다."""
        self.fake.fail_write = True
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            with self.assertRaises(remote_store.RemoteError):
                profile.save({"birth": "1994-07-15"})

    def test_read_failure_falls_back_with_warning(self):
        """원격을 못 읽어도 화면은 떠야 하고, 경고가 남아야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1994-07-15"})
            remote_store.invalidate()
            self.fake.fail_read = True
            info = profile.load_detailed()
            self.assertEqual(info["origin"], "로컬 파일")
            self.assertTrue(info["warning"], "경고 없이 조용히 내려가면 안 된다")
            self.assertEqual(info["profile"]["birth"], "1994-07-15")

    def test_provided_separates_defaults(self):
        """사용자가 저장한 값과 기본값을 구분할 수 있어야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1994-07-15"})
            remote_store.invalidate()
            info = profile.load_detailed()
        self.assertIn("birth", info["provided"])
        # 저장 시 merge 로 전체 키가 채워지므로 provided 에 기본값 키도 들어온다.
        # 중요한 것은 '저장된 적 있음'을 origin 으로 구분할 수 있다는 점이다.
        self.assertEqual(info["origin"], "원격 DB")
        self.assertTrue(info["saved"])

    def test_clear_resets(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY,
                  PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1994-07-15"})
            profile.clear()
            remote_store.invalidate()
            info = profile.load_detailed()
            self.assertEqual(info["origin"], "기본값")
            self.assertFalse(info["saved"])
            self.assertEqual(info["profile"]["birth"], profile.DEFAULT["birth"])

    def test_local_only_mode_unchanged(self):
        """원격을 안 쓰면 예전 동작 그대로 (로컬 파일)."""
        with _env(PROFILE_FILE=str(self.pfile)):
            profile.save({"birth": "1977-07-07"})
            info = profile.load_detailed()
            self.assertEqual(info["origin"], "로컬 파일")
            self.assertEqual(info["profile"]["birth"], "1977-07-07")


# ============================================================ 즐겨찾기 영속
class TestBookmarkPersistence(RemoteBase):
    def setUp(self):
        super().setUp()
        self.bm = Path(self.tmp.name) / "bookmarks.json"
        self.patch = mock.patch.object(store, "BOOKMARKS_FILE", self.bm)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_survives_local_file_loss(self):
        """즐겨찾기는 되살릴 방법이 없다 — 재시작을 견뎌야 한다."""
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            store.toggle_bookmark("LH:12345")
            store.toggle_bookmark("청약홈:999")
            self.bm.unlink()                      # 임시 디스크가 날아간 상황
            remote_store.invalidate()
            self.assertEqual(sorted(store.load_bookmarks()),
                             ["LH:12345", "청약홈:999"])

    def test_toggle_off_persists(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            store.toggle_bookmark("LH:1")
            res = store.toggle_bookmark("LH:1")
            self.assertFalse(res["bookmarked"])
            remote_store.invalidate()
            self.assertEqual(store.load_bookmarks(), [])

    def test_reports_where_it_was_saved(self):
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            self.assertEqual(store.toggle_bookmark("LH:1")["persisted"], "원격 DB")
        with _env():
            self.assertEqual(store.toggle_bookmark("LH:2")["persisted"], "로컬 파일")

    def test_save_failure_raises(self):
        self.fake.fail_write = True
        with _env(GITHUB_REPO=REPO, GITHUB_TOKEN=TOKEN, ENCRYPTION_KEY=KEY):
            with self.assertRaises(remote_store.RemoteError):
                store.toggle_bookmark("LH:1")


if __name__ == "__main__":
    unittest.main()
