"""테스트가 **운영 데이터를 건드리지 못하는지** 검사한다.

이 파일이 있는 이유
-------------------
2026-11-01, 즐겨찾기를 원격 저장소와 연동한 직후 `test_core.py` 의 북마크
테스트가 개발자 PC 의 `.env` 토큰을 집어 **실제 운영 저장소에 커밋을 남겼다.**
그때는 즐겨찾기가 0건이라 잃을 것이 없었지만, 있었다면 테스트 한 번에 날아갔다.

`tests/__init__.py` 가 설정을 차단하도록 고쳤는데, **차단만 해 두고 확인하지
않으면 언젠가 조용히 풀린다.** 그래서 차단 자체를 검사로 고정한다.
"""
import os
import unittest
from unittest import mock

from app import crypto, remote_store, secrets, store

import tests as tests_pkg


class TestRemoteIsBlocked(unittest.TestCase):
    """기본 상태에서 원격 저장소가 꺼져 있어야 한다."""

    def test_env_vars_are_cleared(self):
        for k in tests_pkg.BLOCKED_KEYS:
            self.assertIsNone(os.environ.get(k),
                              f"{k} 가 환경변수에 남아 있다 — 테스트가 "
                              "운영 저장소를 건드릴 수 있다")

    def test_dotenv_is_unreachable(self):
        """`.env` 로 내려가 실제 토큰을 집어 오지 못해야 한다."""
        self.assertFalse(secrets.ENV_FILE.exists(),
                         f"테스트가 {secrets.ENV_FILE} 를 읽고 있다")
        for k in tests_pkg.BLOCKED_KEYS:
            self.assertEqual(secrets.get(k), "",
                             f"{k} 를 .env 에서 읽어 왔다")

    def test_remote_store_disabled(self):
        self.assertFalse(remote_store.enabled(),
                         "원격 저장소가 켜져 있다 — 테스트가 네트워크로 나간다")
        self.assertFalse(crypto.key_configured())

    def test_bookmark_toggle_stays_local(self):
        """북마크 토글이 네트워크로 나가지 않는지 — 사고가 났던 바로 그 경로.

        httpx 를 통째로 막아 두고 돌린다. 원격으로 나가려 하면 터진다.
        """
        import httpx

        def boom(*a, **kw):
            raise AssertionError("테스트가 실제 네트워크로 나갔다 !!")

        with mock.patch.object(httpx, "request", boom), \
                mock.patch.object(httpx, "get", boom), \
                mock.patch.object(httpx, "post", boom), \
                mock.patch.object(httpx, "put", boom), \
                mock.patch.object(httpx, "delete", boom):
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(store, "BOOKMARKS_FILE",
                                       Path(d) / "bm.json"):
                    r1 = store.toggle_bookmark("테스트:1")
                    self.assertTrue(r1["bookmarked"])
                    self.assertEqual(r1["persisted"], "로컬 파일")
                    r2 = store.toggle_bookmark("테스트:1")
                    self.assertFalse(r2["bookmarked"])
                    self.assertEqual(store.load_bookmarks(), [])

    def test_profile_save_stays_local(self):
        """내 조건 저장도 마찬가지로 네트워크를 타지 않아야 한다."""
        import tempfile
        from pathlib import Path

        import httpx

        from app import profile

        def boom(*a, **kw):
            raise AssertionError("테스트가 실제 네트워크로 나갔다 !!")

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ,
                                 {"PROFILE_FILE": str(Path(d) / "p.json")}), \
                    mock.patch.object(httpx, "request", boom), \
                    mock.patch.object(httpx, "get", boom), \
                    mock.patch.object(httpx, "post", boom):
                res = profile.save({"birth": "1900-01-01"})
                self.assertEqual(res["persisted"], "로컬 파일")


if __name__ == "__main__":
    unittest.main()
