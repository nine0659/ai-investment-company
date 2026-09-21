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
scheduler.py          ← Render 상주 프로세스 (잡 10개 + 텔레그램 봇 스레드). 주 실행자.
.github/workflows/    ← CI(테스트) + 브리핑 백업 크론 (GH cron은 상시 수십 분 지연됨)
graph/investment_graph.py ← 장전/마감 브리핑 LangGraph 파이프라인 (수집→분석→
                        bull/bear 토론→CEO→발송). PRE/CLOSE 순차 꼬리:
                        risk_management_team→review_feedback_team→investment_committee→
                        portfolio_manager_agent→midterm_stock_agent→bull_case→bear_case→
                        ceo_agent (2026-09-03 bull_case/bear_case 추가)
agents/               ← 개별 분석 에이전트 (ceo=핵심 브리핑, midterm/us=주간 추천,
                        bull_bear_debate_team=CEO 직전 강세/약세 토론, ...)
services/             ← 계산·저장 로직 (LLM 없음): valuation, nav, data_guard, job_ledger,
                        risk_gate(CEO 자체 비중 규칙 위반 결정론적 검사, 2026-09-03)...
clients/              ← 외부 연동: kis(한국투자증권), dart, openai, telegram, yfinance,
                        langfuse(LLM 호출·파이프라인 관측성, 선택사항 — 키 미설정 시 no-op)
db/database.py        ← SQLAlchemy 테이블 정의. DATABASE_URL=Neon PostgreSQL(운영),
                        미설정 시 SQLite(개발 전용 — 운영 데이터 아님!)
tests/                ← 전부 과거 실제 사고의 회귀 테스트. 지우지 마라.
```

## 현재 스케줄 (2026-08-18 백테스트 스냅샷 추가 후 — 정기 메시지 주 6통 + 조건부 1통 + 무발송 잡)

| 시각 | 잡 | 내용 |
|---|---|---|
| 월·수·금 08:20 | pre_market | 장전 브리핑 |
| 화 19:00 | discovery_weekly | 종목 발굴 — **조건부 발송**(진짜 후보 있을 때만, 없으면 무발송) |
| 금 15:00 | rebound_screener | 반등 스크리너 자동 발송 (LLM 미사용, 그 외엔 /rebound 온디맨드) |
| 금 16:30 | close_market | 주간 마감 브리핑 |
| 일 20:00 | weekly_picks | 주간 추천 1통 (국내 중기 + 미국 통합) |
| 일 20:30 | backtest_snapshot | 추천/실매매 백테스트 스냅샷 기록 — **무발송**(LLM·텔레그램 없음, StockBench식) |
| 장중 매 15분 | market_monitor | 이상 신호 시에만 발송 |
| 매일 08:05 | daily_health | 어제 잡 누락·실패 감지, 문제 시에만 경보 |
| 매월 첫째 월 19:00 | monthly_thesis | 월간 투자관 (CEO 브리핑 근거 주입용) |
| 평일 16:10 / 16:20 | daily_nav / daily_tracker | 무발송 데이터 수집 |
| 평일 16:45 (GH) | nav-tracker.yml 백업 | Render가 16:10/16:20을 놓친 날만 대신 실행 (job_runs 흔적 확인 후) |

**일시 중단** (추천·거래 데이터 축적 전 공허한 리포트 방지):
귀인분석 · 적중률통계 · 주간전략 · 장기분석 · KOSPI추세 · 월간학습.
수동 실행: `python main.py --type attribution|weekly|strategy|longterm|trend|monthly`
**재개 절차**: scheduler.py에 잡 재등록 → job_ledger 기대목록 갱신 → 테스트 갱신 → 사용자 승인.
재개 판단 기준: stock_recommendations에 추천이 8건+ 쌓이고 추적 데이터가 4주+ 존재할 것.

**예외 — 종목발굴(discovery_agent)은 2026-08-07 조건부 재개됨**: 위 8건+ 기준은
아직 미달(3건)이지만, "완전 자동 발송"이 아니라 `silent_if_empty=True`로
후보가 없으면 조용히 넘어가는 조건부 방식이라 공허한 리포트 반복 문제가
구조적으로 차단된다는 점을 사용자에게 설명하고 명시적 승인을 받아 재개.
다른 중단 기능(귀인분석 등)에 이 예외를 유추 적용하지 말 것 — 매번 별도 승인 필요.

## 학습 루프 (2026-07-06 복원 — 끊어먹지 마라)

```
일요일 추천 발송 → recs_from_weekly_picks()가 파싱·교차검증 → stock_recommendations
→ daily_tracker(16:20)가 목표/손절/만료 추적 → recommendation_tracking
→ (데이터 쌓이면) 적중률·귀인분석 재개 → 프롬프트·기준 개선의 근거

[2026-09-03 추가] 월·수·금 08:20 pre_market → CEO 신규편입 판단(ceo_decisions)
→ recs_from_cio_decisions()가 코드로 목표가/손절가 계산(실데이터 진입가 필수,
조회 실패 시 폐기) → 같은 stock_recommendations → daily_tracker가 동일하게 추적.
PRE 실행에서만 저장(save_recommendations가 날짜 단위 전체 교체라 같은 금요일
pre_market+close_market 이중 저장 시 충돌 방지 — _register_drafts/_trigger_auto_buy와
같은 이유).
```
파서는 환각 차단 관문이다: 분석에 없던 종목코드 폐기, 진입가는 항상 실데이터,
비현실 목표가 폐기, "(지난 추천 유지)"는 재저장 금지 (tests/test_recommendation_parser.py).
`recs_from_cio_decisions`는 브리핑 텍스트 파싱이 아니라 코드 계산(진입가만 실데이터,
손절 -15%는 CEO 헌장의 재검토 의무 기준, 목표가는 risk_reward 비율 적용)으로 같은
원칙을 지킨다 — 2026-06-19 브리핑 포맷 개편 이후 이 함수가 원래 의존하던 텍스트
문구가 사라져 사실상 죽은 코드였던 걸 재설계하며 발견.

**Risk Gate (2026-09-03 추가, `services/risk_gate.py`)**: CEO가 스스로 문서화한
확신도별 비중 밴드(상 5~10%/중 3~5%/하 1~3%)를 CEO 자신이 어겼는지 결정론적으로
검사, 위반 시 별도 경고 메시지만 발송(발송 자체는 막지 않음 — decision_guard의
데이터불일치 차단과는 성격이 다름). node_send_telegram에서 메인 리포트 발송 직후 실행.

**Bull/Bear 토론 (2026-09-03 추가, `agents/bull_bear_debate_team.py`)**: PRE/CLOSE에서
midterm_stock_agent와 ceo_agent 사이에 순차 삽입. Bear가 Bull의 결과를 직접 받아
반박하도록 설계되어 있어 반드시 순차([[project_langgraph_parallel_state_wipe_bug]]
함정과 무관 — 병렬이 아님). CEO의 `=CIO_DECISION_START=` 출력 파싱 스키마는 그대로,
CEO가 읽는 입력 컨텍스트만 풍부해진다.

**추천/목표가/손절가 계산 로직을 바꾸기 전에는 `python scripts/backtest_gate_check.py`를
먼저 돌려라** (2026-08-18 추가). CI 게이트는 아니다 — 표본이 아직 적어(수건대) 자동
pass/fail 임계값을 걸면 오판만 낸다. 대신 과거 추천이 실제로 어떻게 됐는지 사람이
한 번 보고 "이번 변경이 그 사례들을 어느 방향으로 바꾸는지" 가늠하는 체크리스트다.
`backtest_snapshot`(일 20:30, 무발송)이 매주 같은 계산을 job_runs에 남겨 시계열을 쌓는다.

## 알려진 함정 (전부 실제 사고였음)

**분류 규칙 (2026-09-11 추가)**: 새 항목을 추가할 때는 맨 앞에 `[버그]` 또는
`[설계문제]`를 붙인다. `[버그]`는 의도한 설계는 맞는데 구현이 틀린 경우(수정하면
끝), `[설계문제]`는 구현은 의도대로 동작했지만 그 설계/구조 자체가 이런 사고를
구조적으로 유발하는 경우(같은 클래스의 사고가 또 날 수 있어 재발 방지책까지
필요)다. 사후에 "고쳤다"로 끝내지 말고 어느 쪽인지 판단해서 남길 것 — 아래는
기존 항목을 소급 분류한 것.

- **[설계문제] LangGraph 병렬 노드는 절대 `state[k]=v; return state`로 전체 state를 반환하면
  안 된다 — 반드시 바뀐 필드만 담은 델타 dict를 반환할 것(graph/investment_graph.py의
  `_parallel()` 래퍼 참조).** 병렬 브랜치가 전체 state를 반환하면 `_last` 리듀서가
  "가장 나중에 병합된 브랜치"의 (그 브랜치 입장에선 안 바뀐, 즉 옛) 값으로 형제
  브랜치의 변경사항을 덮어쓴다 → 2026-06-12 병렬 L2/L3 도입 이후 **2개월간** 매크로·
  빅피겨·뉴스·글로벌인텔리전스·이벤트리스크·이슈종목 분석이 거의 매번 서로를 지워
  심층 리포트·CEO 브리핑에 도달하지 못했다(계산은 되고 OpenAI 비용도 냈지만 결과가
  반영 직전에 증발). 게다가 `errors`(operator.add 리듀서) 필드는 병렬 브랜치가
  "새로 늘어난 만큼만" 반환해도 병합 과정에서 지수적으로 중복된다(오류 1건→최종
  512건 실측) — 정확한 내부 메커니즘은 못 밝혔고, 병렬 구간에서는 아예 리듀서에
  넘기지 않고 로그로만 남기는 우회로 해결. 새 병렬 노드를 추가할 때 이 함정을 반복하지
  말 것. `tests/test_graph_parallel_state_merge.py`가 회귀 테스트. (2026-09-10 이 설계문제의
  연장선에서 market_intelligence_team/issue_stock_agent를 아예 순차로 옮겼고, 그 과정에서
  잃은 무변화 감지를 2026-09-11 `_track_sequential()`로 다시 메웠다 — 아래 참조.)
- **[버그] DART 재무**: `get_multi_year_financials` history[0]은 당해 **분기** 보고서일 수 있다.
  분기(3개월)와 연간(12개월) 손익을 섞어 비교하면 안 된다 → 전 종목 성장률 -75% 사고.
- **[버그] yfinance dividendYield**: 버전에 따라 0.0291 또는 2.91로 온다. 반드시
  `_normalize_dividend_yield()` 경유 → 배당수익률 291% 사고.
- **[버그] 알파/수익률 비교는 반드시 같은 시작점·같은 기간끼리** → 알파 -71%p 사고.
- **[설계문제] 보유 누적수익률 ≠ 주간 수익률.** LLM 프롬프트에 라벨 명시 → 주간알파 +37% 오기 사고.
  (원인이 계산 실수가 아니라 "어떤 값인지 프롬프트에 명시 안 함"이라는 설계 공백이었음.)
- **[설계문제] DATABASE_URL 누락 시 SQLite 폴백** = 데이터 증발. 2026-07-07부터 db/database.py가
  프로젝트 루트 .env를 자체 로드하고, 폴백 시(미설정 포함) 텔레그램 경보를 보낸다.
  로컬 스크립트는 이제 자동으로 Neon에 붙는다 — 수동 반영 스크립트는 그래도
  `db.database.is_postgres()` 확인 후 쓰기. 테스트는 conftest가 DB_FORCE_SQLITE=1로
  격리한다. GH Actions 신규 잡에는 여전히 `DATABASE_URL: ${{ secrets.DATABASE_URL }}`
  주입 필요 (Render/CI엔 .env가 없다) → 2026-07-02 데이터 소실 사고.
- **[설계문제] GH Actions cron은 정시에 안 돈다** (수십 분 지연). 정시성 필요한 잡은 Render에.
- **[설계문제] Render 재시작(플랫폼 이벤트·배포)을 넘긴 APScheduler 실행은 증발한다** — 잡스토어가
  메모리라 지나간 스케줄을 기억 못 한다 → 2026-07-08 daily_tracker 누락. GH 백업
  (nav-tracker.yml 16:45)이 job_runs 흔적을 보고 누락분만 대신 돈다.
- **[버그] 워크플로 YAML에 `python -c "` 멀티라인 인라인은 금지** — 들여쓰기 없는 연속 줄이
  YAML을 깨뜨려 워크플로가 조용히 죽는다(push마다 failure, 크론 미실행). 스크립트
  파일로 빼라. tests/test_workflows.py가 파싱 유효성을 검증한다.
- **[설계문제] cron-job.org 트리거는 저장소 밖에 산다** — 스케줄 축소 시 함께 정리해야 한다.
  investment-scheduler.yml의 헌장 요일 가드(2026-07-09)가 축소안 밖 자동 트리거를
  발송 전에 스킵하지만, 불필요한 호출 자체는 cron-job.org에서 지워야 한다.
- **Windows 콘솔은 cp949.** 스크립트 실행 시 `PYTHONUTF8=1`, 이모지 print 주의.
- **텔레그램 4096자 제한**은 `send_message`가 자동 분할 처리 — 직접 자르지 마라.
- **KIS 토큰**: 발급 실패 시 서킷 브레이커 있음(2026-07-03). KISClient 생성 실패가
  브리핑 전체를 죽이지 않도록 try/except 유지.
- **[설계문제] 드로다운 자동매도 금지 (2026-07-09 사용자 승인 정책).** 드로다운 -44.4% 오판
  → 실계좌 전량 청산 자동 실행된 사고 (2026-07-08, 보유목록이 빈 값으로 와서
  주문 0건에 그침). 드로다운은 경보만 보낸다. tests/test_drawdown_policy.py가
  자동매도 재도입을 막는다. 재도입은 사용자 승인 필수.
- **[버그] NAV 이상 판정은 총평가 원값이 아니라 평가배율(value/cost)로.** 위 -44.4%의
  실원인은 시세 왜곡이 아니라 7/7 SK하이닉스 전량매도(매입 2,176만원)로 포트폴리오가
  실제로 준 것이었다 (2026-07-10 진단 정정). 원값 비교는 매매·입출금을 오염으로
  오판한다 — 7/9 데이터가드가 정상 NAV 기록을 막은 오탐도 같은 결함. record_nav의
  `_nav_data_suspicious` 가드와 `check_drawdown_defense` 모두 배율 기준으로 판정한다.
- **[설계문제] 순차로 옮긴 노드는 `_parallel()`의 자동 무변화 감지를 안 받는다
  (2026-09-11 발견, 사고로 이어지기 전 예방적 보강).** market_intelligence_team/
  issue_stock_agent를 2026-09-10 병렬→순차로 옮기면서, `_parallel()`이 자동으로
  기록해주던 "이 브랜치가 실제로 state를 바꿨는가"(branch:{name} job_ledger 기록)를
  잃었다 — 예외 없이 그냥 빈 리포트를 반환해도 daily_health가 잡을 방법이 없어진
  사각지대였다. `graph/investment_graph.py`의 `_track_sequential()`로 같은 규약을
  순차 노드에도 다시 적용해 메웠다. `tests/test_track_sequential_wiring.py`가 회귀 테스트.
  앞으로 병렬 노드를 순차로(또는 그 반대로) 옮길 때마다 감지 로직도 같이 옮겨졌는지 확인할 것.
- **[설계문제] 장중 반전 분석(`check_intraday_reversal`)의 반도체 대형주 동향이 KIS
  단일 소스·종목 단위 실패 구분 없이 전부-아니면-전무로 짜여 있었다 (2026-09-21
  09:30 발송분: KIS 조회 실패 → leaders_text가 통째로 "조회 실패" → LLM이 "반도체
  대형주 등락 데이터 부재로 정확한 대형주 영향 분석 어려움"이라고만 서술).
  `services/alert_service._get_reversal_leader_change()`로 분리해 ① KIS 1회
  재시도 ② 실패 시 `clients/market_data_client.fetch_kr_stock_realtime()`(같은
  파일의 `fetch_kr_index_realtime` 검증 패턴 재사용)로 yfinance 폴백 ③ 두 소스
  모두 실패해도 종목별로 "조회 실패"를 명시(한 종목만 실패해도 나머지 종목 데이터가
  묻히던 문제 동시 수정). 같은 전부-아니면-전무 패턴(단일 API·통짜 실패 문구)이
  다른 실시간 조회 지점에도 있을 수 있으니 새로 추가할 때 이 구조를 기본으로
  고려할 것. `tests/test_reversal_leader_data.py`, `tests/test_market_data_client.py`가
  회귀 테스트.
- **[설계문제] KIS 잔고 기준 자동종료가 이 사용자의 실보유를 5주간 지워버렸다
  (2026-09-21 발견).** 2026-08 `services/portfolio_service.sync_from_kis()`가
  "KIS 실계좌를 진실 소스로 삼아 없는 종목은 자동 매도 처리"하도록 추가돼
  `job_daily_nav`(평일 16:10)에서 호출됐는데, 이 사용자의 실거래는 KIS가 아니라
  **미래에셋증권**을 통해 이뤄지고 KIS 잔고는 항상 0원이 정상이라는 사실
  (2026-07-02 확립, `services/portfolio_service.py`에도 원래 "portfolio_positions는
  사람이 직접 갱신해왔다"는 주석이 있었음)과 정면으로 모순됐다. 결과: 2026-08-14
  daily_nav 1회 실행에서 실보유 4종목(현대차·삼성전자·삼성전기·SK하이닉스) 전부가
  "실계좌에서 확인 안 됨"으로 오판·자동종료(status='sold')됐고, portfolio_history엔
  기록되지 않아(실제 매도가 아니므로) 발견이 더 늦어졌다 — 5주 넘게 이 "전속 투자
  자문 AI"가 실보유를 전혀 모르는 채로 브리핑을 내보냄. 드로다운 자동청산
  (2026-07-08)과 같은 교훈: **신뢰할 수 없는 신호로 실보유 데이터를 자동으로
  파괴하지 않는다.** `scheduler.py`의 호출을 제거해 영구 비활성화(사용자 승인),
  함수 자체는 보존. `tests/test_portfolio_kis_sync.py::test_scheduler_never_calls_sync_from_kis`가
  재도입을 막는다. 새 자동화가 "이 사용자는 KIS로 실거래한다"고 가정하지 않도록 주의할 것.

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
python scripts/backtest_gate_check.py  # 추천/목표가/손절가 로직 변경 전 확인 (CI 게이트 아님)
python scripts/judgment_scenario_check.py  # 프롬프트·가드·그래프 배선 변경 전 판단 시나리오 체크리스트 (CI 게이트 아님, 2026-09-11 추가)
git push origin master              # = 운영 배포 (Render 자동배포 + CI)
```
