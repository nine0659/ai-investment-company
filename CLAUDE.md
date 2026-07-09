# AI 투자 자문 시스템 — 개발·운영 가이드

전속 투자 자문 AI. 한국·미국 시장 데이터를 수집해 텔레그램으로 브리핑을 발송한다.
사용자는 중장기 가치투자자이며, 실보유 종목 기반의 신뢰할 수 있는 조언이 목적이다.

## ⚠️ 절대 원칙 5가지 (2026-07 대규모 장애 복구에서 확립 — 위반 금지)

1. **테스트 통과 없이 push 금지.** `python -m pytest tests/` 전부 통과 후 커밋.
   CI(.github/workflows/ci.yml)가 push마다 검증한다. push 즉시 Render가 운영 배포되므로
   깨진 코드는 곧바로 사용자에게 잘못된 브리핑으로 나간다.
2. **수치 계산은 코드가, LLM은 서술만.** 수익률·알파·성장률·비교 연산을 LLM에게
   시키지 마라. LLM은 주어진 숫자를 무비판적으로 서술한다 — 계산해서 넣어줘라.
3. **LLM에 넣는 데이터는 `services/data_guard.py`를 통과시켜라.** 이상치는 수정이
   아니라 제거(N/A). 프롬프트에는 반드시 "없는 수치는 만들지 마라"를 포함.
4. **텔레그램으로 나가는 모든 신규 발송 경로에는 `claim_report_slot(date, run_type)`
   중복 방지 가드를 넣어라.** Render·GitHub Actions·수동 실행이 같은 날 겹칠 수 있다.
5. **새 스케줄 잡을 추가하면 `services/job_ledger.py`의 `_EXPECTED_BY_WEEKDAY`와
   `tests/test_job_ledger.py`를 함께 갱신하라.** 안 하면 헬스체크가 침묵하거나 오탐한다.

**기능 추가보다 무사고가 우선이다.** 새 에이전트·새 브리핑 요청이 오면, 먼저 기존
스케줄이 4주 무사고인지 확인하고 사용자에게 트레이드오프를 알려라.

## 아키텍처 지도

```
scheduler.py          ← Render 상주 프로세스 (잡 8개 + 텔레그램 봇 스레드). 주 실행자.
.github/workflows/    ← CI(테스트) + 브리핑 백업 크론 (GH cron은 상시 수십 분 지연됨)
graph/investment_graph.py ← 장전/마감 브리핑 LangGraph 파이프라인 (수집→분석→CEO→발송)
agents/               ← 개별 분석 에이전트 (ceo=핵심 브리핑, midterm/us=주간 추천, ...)
services/             ← 계산·저장 로직 (LLM 없음): valuation, nav, data_guard, job_ledger...
clients/              ← 외부 연동: kis(한국투자증권), dart, openai, telegram, yfinance
db/database.py        ← SQLAlchemy 테이블 정의. DATABASE_URL=Neon PostgreSQL(운영),
                        미설정 시 SQLite(개발 전용 — 운영 데이터 아님!)
tests/                ← 전부 과거 실제 사고의 회귀 테스트. 지우지 마라.
```

## 현재 스케줄 (2026-07-06 축소 후 — 정기 메시지 주 5통)

| 시각 | 잡 | 내용 |
|---|---|---|
| 월·수·금 08:20 | pre_market | 장전 브리핑 |
| 금 16:30 | close_market | 주간 마감 브리핑 |
| 일 20:00 | weekly_picks | 주간 추천 1통 (국내 중기 + 미국 통합) |
| 장중 매 15분 | market_monitor | 이상 신호 시에만 발송 |
| 매일 08:05 | daily_health | 어제 잡 누락·실패 감지, 문제 시에만 경보 |
| 매월 첫째 월 19:00 | monthly_thesis | 월간 투자관 (CEO 브리핑 근거 주입용) |
| 평일 16:10 / 16:20 | daily_nav / daily_tracker | 무발송 데이터 수집 |
| 평일 16:45 (GH) | nav-tracker.yml 백업 | Render가 16:10/16:20을 놓친 날만 대신 실행 (job_runs 흔적 확인 후) |

**일시 중단** (추천·거래 데이터 축적 전 공허한 리포트 방지):
귀인분석 · 적중률통계 · 주간전략 · 종목발굴 · 장기분석 · KOSPI추세 · 월간학습.
수동 실행: `python main.py --type attribution|weekly|strategy|longterm|trend|monthly`
**재개 절차**: scheduler.py에 잡 재등록 → job_ledger 기대목록 갱신 → 테스트 갱신 → 사용자 승인.
재개 판단 기준: stock_recommendations에 추천이 8건+ 쌓이고 추적 데이터가 4주+ 존재할 것.

## 학습 루프 (2026-07-06 복원 — 끊어먹지 마라)

```
일요일 추천 발송 → recs_from_weekly_picks()가 파싱·교차검증 → stock_recommendations
→ daily_tracker(16:20)가 목표/손절/만료 추적 → recommendation_tracking
→ (데이터 쌓이면) 적중률·귀인분석 재개 → 프롬프트·기준 개선의 근거
```
파서는 환각 차단 관문이다: 분석에 없던 종목코드 폐기, 진입가는 항상 실데이터,
비현실 목표가 폐기, "(지난 추천 유지)"는 재저장 금지 (tests/test_recommendation_parser.py).

## 알려진 함정 (전부 실제 사고였음)

- **DART 재무**: `get_multi_year_financials` history[0]은 당해 **분기** 보고서일 수 있다.
  분기(3개월)와 연간(12개월) 손익을 섞어 비교하면 안 된다 → 전 종목 성장률 -75% 사고.
- **yfinance dividendYield**: 버전에 따라 0.0291 또는 2.91로 온다. 반드시
  `_normalize_dividend_yield()` 경유 → 배당수익률 291% 사고.
- **알파/수익률 비교는 반드시 같은 시작점·같은 기간끼리** → 알파 -71%p 사고.
- **보유 누적수익률 ≠ 주간 수익률.** LLM 프롬프트에 라벨 명시 → 주간알파 +37% 오기 사고.
- **DATABASE_URL 누락 시 SQLite 폴백** = 데이터 증발. 2026-07-07부터 db/database.py가
  프로젝트 루트 .env를 자체 로드하고, 폴백 시(미설정 포함) 텔레그램 경보를 보낸다.
  로컬 스크립트는 이제 자동으로 Neon에 붙는다 — 수동 반영 스크립트는 그래도
  `db.database.is_postgres()` 확인 후 쓰기. 테스트는 conftest가 DB_FORCE_SQLITE=1로
  격리한다. GH Actions 신규 잡에는 여전히 `DATABASE_URL: ${{ secrets.DATABASE_URL }}`
  주입 필요 (Render/CI엔 .env가 없다) → 2026-07-02 데이터 소실 사고.
- **GH Actions cron은 정시에 안 돈다** (수십 분 지연). 정시성 필요한 잡은 Render에.
- **Render 재시작(플랫폼 이벤트·배포)을 넘긴 APScheduler 실행은 증발한다** — 잡스토어가
  메모리라 지나간 스케줄을 기억 못 한다 → 2026-07-08 daily_tracker 누락. GH 백업
  (nav-tracker.yml 16:45)이 job_runs 흔적을 보고 누락분만 대신 돈다.
- **워크플로 YAML에 `python -c "` 멀티라인 인라인은 금지** — 들여쓰기 없는 연속 줄이
  YAML을 깨뜨려 워크플로가 조용히 죽는다(push마다 failure, 크론 미실행). 스크립트
  파일로 빼라. tests/test_workflows.py가 파싱 유효성을 검증한다.
- **cron-job.org 트리거는 저장소 밖에 산다** — 스케줄 축소 시 함께 정리해야 한다.
  investment-scheduler.yml의 헌장 요일 가드(2026-07-09)가 축소안 밖 자동 트리거를
  발송 전에 스킵하지만, 불필요한 호출 자체는 cron-job.org에서 지워야 한다.
- **Windows 콘솔은 cp949.** 스크립트 실행 시 `PYTHONUTF8=1`, 이모지 print 주의.
- **텔레그램 4096자 제한**은 `send_message`가 자동 분할 처리 — 직접 자르지 마라.
- **KIS 토큰**: 발급 실패 시 서킷 브레이커 있음(2026-07-03). KISClient 생성 실패가
  브리핑 전체를 죽이지 않도록 try/except 유지.
- **드로다운 자동매도 금지 (2026-07-09 사용자 승인 정책).** 드로다운 -44.4% 오판
  → 실계좌 전량 청산 자동 실행된 사고 (2026-07-08, 보유목록이 빈 값으로 와서
  주문 0건에 그침). 드로다운은 경보만 보낸다. tests/test_drawdown_policy.py가
  자동매도 재도입을 막는다. 재도입은 사용자 승인 필수.
- **NAV 이상 판정은 총평가 원값이 아니라 평가배율(value/cost)로.** 위 -44.4%의
  실원인은 시세 왜곡이 아니라 7/7 SK하이닉스 전량매도(매입 2,176만원)로 포트폴리오가
  실제로 준 것이었다 (2026-07-10 진단 정정). 원값 비교는 매매·입출금을 오염으로
  오판한다 — 7/9 데이터가드가 정상 NAV 기록을 막은 오탐도 같은 결함. record_nav의
  `_nav_data_suspicious` 가드와 `check_drawdown_defense` 모두 배율 기준으로 판정한다.

## 장애 대응 런북

- **08:05 헬스체크 경보 수신 시**: ① Render 대시보드에서 서비스 상태 확인(슬립/크래시)
  ② `job_runs`·`report_claims` 테이블에서 해당 날짜 흔적 조회 ③ 필요 시
  `python main.py --type <run_type>`로 수동 재실행 (claim 가드가 중복은 막아준다).
- **"운영 DB 연결 실패" 경보**: Neon 프로젝트 상태 + Render/GH의 DATABASE_URL 확인.
  SQLite 폴백 중 쌓인 데이터는 재시작 시 소실된다 — 빠르게 복구할 것.
- **브리핑에 이상한 수치 발견 시**: 원인은 거의 항상 입력 데이터다. data_guard 로그
  (`[데이터가드]`)부터 확인하고, 해당 사례를 tests/에 회귀 테스트로 추가하라.

## 자주 쓰는 명령

```bash
python -m pytest tests/ -q          # 커밋 전 필수
python main.py --check              # 환경변수 검증
python main.py --type pre           # 장전 브리핑 수동 실행
python main.py --research 005930    # 기업 딥리서치
python main.py --portfolio list     # 보유 종목
git push origin master              # = 운영 배포 (Render 자동배포 + CI)
```
