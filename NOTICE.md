# 외부 오픈소스 검토 결과 및 출처 표기

HOME RADAR는 아래 오픈소스를 참고·일부 재사용했다.
검토일: 2026-10-30

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
- `cheong/eligibility.py` 의 판정 설계(추천/확인/제외, 대상계층 역필터, 청약통장 소요 구분)를 `app/eligibility.py` 로 각색
- `cheong/models.py` 의 `Notice` 공통 레코드 및 `is_closed` 마감 추정 로직을 `app/models.py` 로 각색
- `cheong/profile.py` 의 프로필 스키마를 `app/profile.py` 로 각색

**재사용하지 않은 것**
- Discord 전송(`discord.py`), GitHub Actions 워크플로, macOS launchd 스크립트
- LH/SH/서울시 수집기 — 현 단계에서는 청약홈 단일 소스로 범위를 좁혔다 (아래 "다음 단계" 참고)

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
