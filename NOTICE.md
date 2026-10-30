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

### SH 공식 개방 데이터 조사 결과 (2026-10-30)

"허용되지 않은 크롤링은 하지 않는다"는 요구에 따라, SH 공고를 **공식 개방 데이터로
받을 수 있는지 먼저 조사**했다. 결론: **공고 목록용 공식 오픈API가 없다.**

| 조사 대상 | 결과 |
|---|---|
| `data.go.kr` 의 SH 데이터셋 | 자치구별 임대주택 현황, 행복주택 공급계획, 주택관리현황, 주거안심종합센터 목록 — 모두 **파일데이터(현황·계획)** 이고 **공고 목록이 아니다** |
| `i-sh.co.kr` 공공데이터 개방 안내 | 자체 카탈로그 없이 `data.go.kr` 목록으로 안내 |
| 서울 열린데이터광장 `data.seoul.go.kr` | `서울주택도시공사_국민임대주택 공급계획` 이 있었으나 **서비스 중단**. 공고 목록 API 없음 |
| 국토교통부 마이홈포털 공공임대주택 **단지정보** 조회 (15110581) | 단지 정보 API 이고 **공고 목록이 아니다** |

따라서 SH 공고는 **서울주거포털(`housing.seoul.go.kr`)** 이 유일한 경로다.
이 사이트의 `robots.txt` 는 `User-agent: * / Allow: /` 로 **전체 허용**이므로
허용 범위 안의 수집이다. AI 에이전트를 명시 차단한 `i-sh.co.kr` 은 계속 비활성으로 둔다.

공식 API 가 생기면 그쪽으로 옮기는 것이 맞다 (`app/sources/registry.py` 에
수집원을 추가하고 `dedup.SOURCE_RANK` 에서 공식 API 를 우선하게 하면 된다).

### 수집 예절

- 공개 화면을 읽는 수집원은 페이지 요청 사이에 0.7초 간격을 둔다.
- 상세 조회는 **접수가 끝나지 않은 공고만**, 건수 상한(기본 40건)을 두고 받는다.
- 받은 상세는 캐시해 같은 공고를 반복해서 긁지 않는다.
- 브라우저와 같은 User-Agent 를 쓰고, 세션 쿠키를 정상 흐름대로 먼저 받는다.

---

## API 키가 필요한 수집원

2026-10-30 — LH 키를 받아 **실제 호출로 서비스별 승인 상태를 확인**했다.
LH 는 서비스마다 활용신청을 따로 해야 하고, 실제로 일부만 승인된 상태가 존재한다.

| 서비스 | 승인 | 확인 방법 |
|---|---|---|
| LH 공고 목록 15058530 | **승인** | 303건 수신 (ALL_CNT 188/유형) |
| LH 공급정보 15056765 | **승인** | 주택형·면적·세대수·금액 정규화 성공 |
| LH 공고 상세 15057999 | **미승인** | `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` |
| 청약홈 오픈API 15098547 | 키 미설정 | — |

키가 없으면 그 수집원은 건너뛰고 화면에 '키 없음'으로 표시된다 — 조용히 실패하지 않는다.
호출 한도 초과·미등록 키·승인 안 된 서비스는 각각 다른 안내 문구로 구분된다.

### 인증키 파라미터 — 실측으로 확정

공식 문서는 `ServiceKey`(대문자 S)로 적혀 있는데 코드는 `serviceKey` 를 쓴다.
더미 키를 넣고 "게이트웨이가 그 이름을 인식했는가"로 판별했다
(인식하면 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`, 무시하면 `SERVICE_KEY_IS_NULL`).

| 파라미터 이름 | 결과 |
|---|---|
| `serviceKey` / `ServiceKey` / `servicekey` | 403 NOT_REGISTERED → **인식됨** |
| `SERVICEKEY` | 401 IS_NULL → 미인식 |

→ **대소문자 무관**(전부 대문자만 예외). 기존 코드가 맞다.

또한 무효 키의 실제 응답은 **403**이지 401이 아니다. 401만 처리하던 코드는 403을
일반 오류로 흘려 재시도만 3번 하고 조치 안내를 잃었다 — 본문 `errMsg` 를 먼저
보도록 고쳤다.

### LH 공급정보 응답 필드 — 라이브로 확정

포털 문서의 출력결과 표에는 **토지 변형만** 실려 있다. 주택 변형은 문서에 없어서
실제 호출로 유형별 필드를 확인했다 (`app/sources/lh_supply.py` 주석에 기록).

| 유형 | 필드 |
|---|---|
| 임대·신혼희망타운 | `HTY_NNA` · `DDO_AR` · `SPL_AR` · `NOW_HSH_CNT` · `LS_GMY` · `RFE` · `SBD_LGO_NM`(단지명) |
| 분양 | `HTY_NM` · `RSDN_DDO_AR` · `SIL_AMT`(분양가) · `BZDT_NM`(사업지구명) |
| 매입임대 | `ADR`/`DNG_HS_ADR`(호별 주소) · `VLD_VL_NM`(층) · `SGG_NM` |
| 토지 | `LND_US_DS_CD_NM` · `LGDN_DTL_ADR` · `AR` · `SPL_XPC_AMT` |

- 단지명이 **2종**이고 매입임대는 주택형·세대수 대신 호별 주소·층으로 온다.
- 금액 단위는 **원** (분양 `SIL_AMT` 실측 407,471,000 = 4.07억).
  `Notice.price_min/max` 는 만원이라 환산해서 넣는다.
- 임대 유형은 보증금·월임대료가 `"공고문 참조"` **문자열**로 오는 경우가 많다 →
  0 으로 채우지 않고 원문을 보존한다. 금액이 0 으로 보이면 '공짜'로 오독된다.
- 의미를 확정하지 못한 `QUP_CNT` · `LTR_SPL_RMNO` · `HO_NO` 는 **매핑하지 않았다** —
  추측해서 세대수로 쓰면 틀린 숫자를 사실처럼 보여준다.

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
