# 외부 오픈소스 검토 결과 및 출처 표기

HOME RADAR는 아래 오픈소스를 참고·일부 재사용했다.
검토일: 2026-10-30

---

## 공개 화면 수집과 robots.txt 확인 결과

공개 화면을 읽는 수집원은 각 사이트의 `robots.txt` 를 직접 받아 확인했다 (2026-10-30).

| 사이트 | robots.txt | 판단 |
|---|---|---|
| `www.applyhome.co.kr` (청약홈) | **없음** (404) | 제한 고지 없음 → 수집 |
| `housing.seoul.go.kr` (SH 서울주거포털) | `User-agent: * / Allow: /` | **전체 허용** → 수집 |
| `soco.seoul.go.kr` (청년안심주택) | **403** — 조회 불가 | RFC 9309 는 robots.txt 가 4xx 면 제한 없음으로 보도록 정한다 → 수집 |
| `www.i-sh.co.kr` (SH 홈페이지 게시판) | **`ClaudeBot`·`Claude-SearchBot`·`GPTBot`·`CCBot` 등 AI 에이전트를 `Disallow: /` 로 명시 차단** | **수집하지 않음. 기본 비활성** |

### i-sh.co.kr 을 뺀 이유

`User-agent: *` 블록은 우리가 읽으려던 경로(`/main/lay2/.../brd/m_241/`)를 막지 않는다.
하지만 같은 파일이 AI 에이전트를 User-agent 별로 하나씩 열거해 전면 차단하고 있다.
사이트 운영자가 AI 에이전트의 접근을 명시적으로 거부한 것이므로 **수집기를 구현하지
않고 기본값을 끔으로 뒀다.** 참고한 오픈소스(cheongyak-auto)에는 이 게시판 수집기가
있지만 그 동작을 그대로 가져오지 않았다.

이 저장소는 사용자 본인이 자기 컴퓨터에서 돌리는 개인 도구이므로 최종 판단은
사용자에게 맡긴다 (`config.yaml` 의 `sources.ish.enabled`). 기본값은 끔이다.

### 수집 예절

- 공개 화면을 읽는 수집원은 페이지 요청 사이에 0.7초 간격을 둔다.
- 상세 조회는 **접수가 끝나지 않은 공고만**, 건수 상한(기본 40건)을 두고 받는다.
- 받은 상세는 캐시해 같은 공고를 반복해서 긁지 않는다.
- 브라우저와 같은 User-Agent 를 쓰고, 세션 쿠키를 정상 흐름대로 먼저 받는다.

---

## API 키가 필요한 수집원

2026-10-30 확인 결과 — 키 없이 호출하면 둘 다 거부된다.

| 서비스 | 응답 | 처리 |
|---|---|---|
| 청약홈 오픈API (`api.odcloud.kr`, 15098547) | `401` | 설정만 준비, 수집기 미구현 |
| LH 분양임대공고문 (`apis.data.go.kr`, 15058530) | `401 SERVICE_KEY_IS_NULL` | **수집기 구현 완료**, 키 대기 |

키가 없으면 그 수집원은 건너뛰고 화면에 '키 없음'으로 표시된다 — 조용히 실패하지 않는다.

---

## 1. jisangfolio/cheongyak-auto — 재사용 (핵심 기반)

| 항목 | 내용 |
|---|---|
| URL | https://github.com/jisangfolio/cheongyak-auto |
| 라이선스 | **MIT** (LICENSE 파일 존재, `Copyright (c) 2026 jisangfolio`) |
| 최종 커밋 | 2026-10-29 (활발히 유지됨) |
| 스택 | Python + requests / beautifulsoup4 / pyyaml |
| 현재 실행 가능성 | 코드는 정상. 단 **청약홈·LH 수집에 공공데이터포털 API 키가 필수** — 키 없이는 해당 수집처가 건너뛰어짐 |

**재사용 범위**
- `cheong/eligibility.py` 의 판정 설계(추천/확인/제외, 대상계층 역필터, 청약통장 소요 구분)를 `app/eligibility.py` 로 각색.
  이후 조건을 하나씩 따로 판정하는 구조(`app/checks.py`)로 바꿨으나 원본의 원칙
  "구조적으로 확실히 안 되는 것만 제외하고, 애매하면 확인으로 살린다"는 그대로 따른다
- `cheong/models.py` 의 `Notice` 공통 레코드 및 `is_closed` 마감 추정 로직을 `app/models.py` 로 각색
- `cheong/profile.py` 의 프로필 스키마를 `app/profile.py` 로 각색 후 확장
  (근로/사업소득 분리, 자산·부채, 거주기간, 청약통장 상세, 희망 분양가)
- `cheong/sources/lh.py` 를 `app/sources/lh.py` 로 각색 (httpx 로 교체)
- `cheong/sources/sh.py` 의 서울주거포털 파싱을 `app/sources/sh.py` 로 각색 (bs4 유지)
- `cheong/sources/youth.py` 의 청년안심주택 JSON 수집·접수기간 파싱을
  `app/sources/youth.py` 로 각색

**재사용하지 않은 것**
- Discord 전송(`discord.py`) — 자동 알림은 승인 전까지 붙이지 않는다
- GitHub Actions 워크플로, macOS launchd 스크립트 (Windows 작업 스케줄러용으로 새로 씀)
- `cheong/sources/sh.py` 의 **i-sh.co.kr 게시판 수집 부분** — 위 robots.txt 확인 결과 참고

MIT 라이선스 원문은 `_reference/cheongyak-auto/LICENSE` 에 보존되어 있다.

---

## 2. analyst-sooin/apply-home — 기법만 참고

| 항목 | 내용 |
|---|---|
| URL | https://github.com/analyst-sooin/apply-home |
| 라이선스 | ⚠️ **불명확** — README 에 "## 라이선스 / MIT" 한 줄만 있고 `LICENSE` 파일도, `package.json` 의 `license` 필드도 **없음** |
| 최종 커밋 | 2026-03-18 (약 7개월 정지) |
| 스택 | React 19 + Vite + Tailwind + Netlify Functions |
| 현재 실행 가능성 | **검증 완료 — 동작함.** `node scripts/fetch-data.js` 실행 시 9개월분 777건 수집 성공 (2026-10-30 확인) |

**라이선스 판단**
README 의 "MIT" 표기는 저작권자·전문이 빠져 있어 완전한 허락 고지로 보기 어렵다.
따라서 **코드를 복사하지 않고, 공개된 HTTP 호출 방식(엔드포인트·헤더·세션 쿠키 흐름)만 참고해 독자 구현**했다.
HTTP 엔드포인트 자체는 저작물이 아니므로 이 범위는 안전하다.

**참고한 내용**
- 청약홈 캘린더 AJAX 엔드포인트와 필요한 헤더 조합 (`gvPgmId`, `ajaxAt`, 세션 쿠키 선취득)
- 공고 상세 HTML 의 섹션 구조 (접수기간 / 공급대상 / 공급금액 / 모집공고문)
- `RCEPT_SE` 일정 구분 코드의 의미

**이 저장소에서 복사한 코드는 없다.**

---

## 3. verisworks-ai/naejipgak-mcp — 데이터만 참고

| 항목 | 내용 |
|---|---|
| URL | https://github.com/verisworks-ai/naejipgak-mcp |
| 라이선스 | **MIT** (LICENSE 파일 존재, `Copyright (c) 2026 Veris`) |
| 최종 커밋 | 2026-07-07 |
| 스택 | Node.js MCP (stdio JSON-RPC) 서버 |
| 현재 실행 가능성 | 번들된 JSON 룰로 동작하므로 키 없이 실행 가능. 단 **MCP 서버라 웹앱에 직접 붙이기엔 맞지 않음** |

**재사용 범위**
- `src/data/rules/income-limits.json` 의 **도시근로자 가구원수별 월평균소득 기준표**
- `src/data/rules/special-supply.json` 의 특별공급 유형별 `incomeLimitPct` (신혼 140% / 생애최초 160%)

위 수치를 `app/rules.py` 에 출처 주석과 함께 옮겼다.

**주의** — 원 저장소도 스스로 밝히듯 이 룰은 "OSS 샘플 룰 엔진 검증용 기본값"이다.
공고별 실제 기준이 항상 우선하므로, HOME RADAR 는 이 수치를 **제외 근거로 쓰지 않고 "확인 필요" 판정과 안내 문구에만** 사용한다.

---

## 종합 판단

- 재사용의 **법적 근거가 확실한 것은 cheongyak-auto 와 naejipgak-mcp (둘 다 MIT)** 이다.
- apply-home 은 라이선스가 불완전해 코드 복사를 피하고 기법만 참고했다.
- **가장 큰 실행 리스크는 공공데이터포털 API 키 의존성**이었고, 키가 필요 없는 청약홈 캘린더 경로가 실제로 동작함을 확인해 이를 1차 수집원으로 채택했다. 공식 API 키는 발급 후 선택적 보강 경로로 붙인다.
