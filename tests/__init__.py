"""테스트 패키지 — **실제 원격 저장소를 건드리지 못하게 막는다.**

왜 이 파일에 코드가 있나
------------------------
2026-11-01, 즐겨찾기를 원격 저장소와 연동한 뒤 사고가 하나 났다.
`test_core.py` 의 북마크 테스트가 `store.toggle_bookmark()` 를 그냥 호출하는데,
그 함수는 이제 원격 저장소에 쓴다. 개발자 PC 의 `.env` 에 실제 토큰이 들어 있으면
**테스트가 운영 데이터 저장소에 커밋을 남긴다.** 실제로 남겼다.

그때는 즐겨찾기가 0건이라 잃을 것이 없었지만, 즐겨찾기가 있었다면
테스트를 한 번 돌리는 것만으로 날아갔을 것이다.

그래서 테스트 패키지를 불러오는 시점에 **설정을 원천 차단한다.**

  1. 프로세스 환경변수에서 원격 저장소 설정을 지운다
  2. `.env` 파일을 못 찾는 경로로 돌린다 (secrets 는 환경변수 → .env 순서로 찾는다)

원격을 **써야 하는** 테스트(test_storage.py)는 각자
`mock.patch.dict(os.environ, {...}, clear=True)` 로 가짜 설정을 넣고
httpx 를 가로채므로 이 차단과 충돌하지 않는다.

이 차단이 살아 있는지 자체를 `test_isolation.py` 가 검사한다 —
막아 두기만 하고 확인하지 않으면 언젠가 조용히 풀린다.
"""
import os
from pathlib import Path

from app import secrets as _secrets

# 원격 저장소·암호화 설정을 프로세스에서 제거한다
BLOCKED_KEYS = (
    "GITHUB_REPO", "GITHUB_TOKEN", "GITHUB_BRANCH", "ENCRYPTION_KEY",
    # 예전 백엔드 흔적까지 같이 막아 둔다
    "SUPABASE_URL", "SUPABASE_KEY",
)
for _k in BLOCKED_KEYS:
    os.environ.pop(_k, None)

# `.env` 를 읽지 못하게 한다. 환경변수를 지워도 secrets 가 .env 로 내려가면
# 실제 토큰을 다시 집어 오기 때문에 이 줄이 반드시 필요하다.
NO_ENV_FILE = Path(__file__).resolve().parent / "_no_such_env_file"
_secrets.ENV_FILE = NO_ENV_FILE
_secrets.reset_cache()
