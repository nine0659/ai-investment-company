# AI 투자 자문 시스템 — 개발·운영 가이드

전속 투자 자문 AI. 한국·미국 시장 데이터를 수집해 텔레그램으로 브리핑을 발송한다.
사용자는 중장기 가치투자자이며, 실보유 종목 기반의 신뢰할 수 있는 조언이 목적이다.

## ⚠️ 절대 원칙 5가지 (2026-07 대규모 장애 복구에서 확립 — 위반 금지)

1. **테스트 통과 없이 push 금지.** `python -m pytest tests/` 전부 통과 후 커밋.
   CI(.github/workflows/ci.yml)가 push마다 검증한다. push 즉시 Render가 운영 배포되므로
   깨진 코드는 곧바로 사용자에게 잘못된 브리핑으로 나간다.
   **단, CI 통과·push 성공 ≠ 실제 배포 성공이다.** Render 배포 자체가 실패(예: 필수
   환경변수 누락으로 `start.sh`가 `exit 1`)하면 컨테이너는 예전 버전으로 계속
   돌아가고, 이 실패에 대한 텔레그램 경보는 없다 — Render 대시보드에서 사람이
   확인해야만 안다(2026-09-21 사고, 아래 알려진 함정 참조). **중요한 수정을
   push한 뒤에는 `curl {BASE_URL}/api/status` 응답 필드(`market`/`trading_day`/
   `holiday` 포함 여부)로 실제 배포가 갱신됐는지 확인하는 습관을 들일 것** —
   필드가 없거나 응답 모양이 다르면 옛 컨테이너가 계속 돌고 있다는 신호다.
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
  **연쇄 사각지대(같은 날 발견, [버그]):** 위 사고로 보유가 0개였던 5주 동안
  `services/nav_service.record_nav()`가 "보유 종목 없음"이면 경보 없이 조용히
  스킵하도록 짜여 있어 `portfolio_nav`도 같은 기간 완전히 비었다. `job_runs`엔
  매일 daily_nav "success"만 찍혀 daily_health도 못 잡는 사각지대였다(같은 함수의
  "오염 의심" 분기엔 이미 `send_error_alert`가 있었는데 "보유 0개" 분기만
  빠져 있던 비대칭). 경보 추가로 수정. `tests/test_nav_service_record_nav.py::test_record_nav_alerts_when_portfolio_empty`가
  회귀 테스트. 교훈: 자동화의 한쪽 실패 분기에 경보를 달았다고 안심하지 말고
  "성공도 실패도 아닌 조용한 스킵" 분기가 더 없는지 항상 같이 점검할 것.
- **[설계문제] Render 배포 실패가 경보 없이 며칠간 옛 코드를 계속 운영했다
  (2026-09-21 발견).** `WEB_PASSWORD` 환경변수가 Render에서 빠져 있어 `start.sh`가
  매 배포마다 `[start.sh] WEB_PASSWORD 미설정 — 웹 대시보드 보호를 위해 시작 중단`
  로 즉시 `exit 1`, `Port scan timeout` → 배포 실패. Render는 배포 실패 시
  **직전에 성공했던 컨테이너를 계속 서비스**한다 — 그 자체는 합리적인 안전장치지만,
  이 프로젝트엔 "배포가 실패했다"는 사실을 알리는 경로가 전혀 없어서(텔레그램
  경보도, daily_health 체크도 없음) 옛 컨테이너가 살아서 잡을 계속 "success"로
  실행하는 게 정상 운영처럼 보였다. 그 결과: 바로 위 sync_from_kis 비활성화
  커밋(a6892a3)이 push·CI 통과했는데도 실제로는 배포되지 않아, 옛 코드가 그대로
  살아 16:10 daily_nav에서 실보유 4종목을 **다시** 자동종료(복구 당일 재발).
  **교훈: push 성공·CI green은 "코드가 저장소에 있다"는 뜻일 뿐 "운영에 반영됐다"는
  뜻이 아니다.** 위 절대원칙 1번의 배포 확인 습관 참조. 근본 수정은 코드가 아니라
  Render 환경변수 설정(WEB_PASSWORD 등록)이었다 — 이런 종류의 사고는 테스트로도
  못 잡는다(로컬 CI엔 Render 환경변수가 없어 이 실패 자체가 재현되지 않음).
  Render 대시보드의 배포 실패 알림(이메일 등)을 켜두는 걸 권장.
- **[버그] DATABASE_URL이 psycopg(v3, "+psycopg") 드라이버 스킴이면 즉시 SQLite
  폴백 (2026-09-29 발견).** Render의 DATABASE_URL이 `postgresql+psycopg://` 형식
  이었는데 requirements.txt엔 psycopg2-binary(v2)만 있어 접속 시도 즉시
  `No module named 'psycopg'` → SQLite 폴백 → 그 세션 보유종목 0건 오판 → NAV
  기록 스킵 경보. 실제 Neon DB는 훼손되지 않았음(로컬에서 직접 접속해 실보유
  4종목 `status='holding'` 온전함 확인) — 해당 실행 1회의 연결 실패였다.
  `db/database.py`의 `_make_engine()`에서 create_engine에 넘기기 전 URL 스킴을
  항상 순수 `postgresql://`로 정규화(드라이버 힌트 제거)해 기본값인 psycopg2를
  강제하도록 수정. `tests/test_database_env.py::test_driver_scheme_normalized_to_plain_postgresql`가
  회귀 테스트.
- **[설계문제] daily_tracker(학습 루프의 핵심 잡)도 nav_service와 같은 "조용한
  스킵" 사각지대를 갖고 있었다 (2026-09-29 코드 전수감사에서 발견).**
  `services/recommendation_tracker_service.py`가 ① 추천 종목 DB 조회 실패
  ② 추적 대상 0건, 두 분기 모두 경보 없이 조용히 빈 결과를 반환하도록 짜여
  있었다 — `job_runs`엔 daily_tracker "success"만 찍혀 학습 루프(추천→추적→
  recommendation_tracking→귀인분석 재개 기준)가 몇 주째 멈춰도 daily_health가
  못 잡는 구조. `nav_service.record_nav()`의 "보유 0개" 사각지대(2f3738b, 같은 날
  오전 발견)와 동형 구조 — 두 분기 모두 `send_error_alert` 추가.
  `tests/test_recommendation_tracker_alerts.py`가 회귀 테스트.
- **[메타 패턴] 위 두 건은 같은 유형의 사고가 하루에 두 번 발견된 사례다 — "한
  분기에 경보를 달면 안심하고 넘어간다"는 습관 자체가 반복 사고의 원인이다.**
  fix: 커밋이 전체 커밋의 46%(2026-09 기준)에 달하는 근본원인을 데이터로 보면
  크게 세 클러스터로 나뉜다: ① 프롬프트 지시-정규식 파서 계약 불일치(예:
  db5effc·245a633·7e599e3·4098d31 — 프롬프트 문구를 고치면 파서가 조용히
  깨지는데 그 계약을 강제하는 장치가 없음), ② 조용한 스킵/사각지대(위 두 건,
  8/14 KIS자동종료 5주 공백, daily_health 자체가 2주간 죽어있던 사고 — "실패도
  성공도 아닌 분기"에 경보가 비대칭적으로만 존재), ③ 외부 데이터소스 전부-
  아니면-전무(`clients/kis_client.py`의 조회 메서드 8개 중 폴백 있는 게 0개 —
  `clients/market_data_client.py`의 KIS→yfinance 폴백 패턴이 실제로 쓰인 곳은
  2026-09-21 반전분석 수정 단 한 곳뿐). 새 코드를 추가하거나 기존 분기를 고칠
  때 이 세 패턴에 해당하는지 스스로 점검할 것 — 사고가 나야만 발견되는 게 아니라
  구조적으로 반복되도록 짜여 있다는 뜻이다.
- **[개선, 2026-09-29] 위 메타 패턴의 클러스터 ③(KIS 단일장애점) 중 "가격류"만
  범위로 잡아 폴백 확대.** `clients/kis_client.py`에 `get_stock_price_with_fallback()`
  추가 — KIS 실패 시 `market_data_client.fetch_kr_stock_realtime()`(9/21 반전분석
  수정에 쓰인 것과 같은 KIS→yfinance 패턴)으로 가격·등락률만 대체, PER/PBR 등
  KIS 전용 필드는 대체 불가(범위 밖). `portfolio_service.calculate_pnl()`(NAV·
  보유평가 전반의 기반)과 `alert_service.py`의 시장감시·위험관리 현재가 조회
  2곳에 적용. 등락률순위·수급순위류(`get_fluctuation_rank` 등 나머지 KIS 메서드)는
  yfinance에 동등 데이터가 없어 대상에서 제외 — 사용자 승인(사고 비용 대비 낮은
  리스크만 우선 처리, 나머지는 필요시 별도 요청). `tests/test_kis_client_price_fallback.py`가
  회귀 테스트.
- **[개선, 2026-09-29] 메타 패턴 대응 4종 — 구조로 막는 방식으로 전환.**
  ① **배포 실패 감지**: `/api/status`가 `commit`(RENDER_GIT_COMMIT)을 노출하고
  `.github/workflows/deploy-check.yml`(push 시 + 평일 07:30)이 `scripts/check_deploy.py`로
  master HEAD와 비교, 20분 대기 후에도 다르면 텔레그램 경보. 절대원칙 1번의 수동
  curl 확인을 자동화한 것 — 단 `commit` 필드가 아직 없던 첫 배포 직후엔 일시 불일치가
  정상. `tests/test_check_deploy.py`. ② **`job_runs.status='empty'`**: 예외는 없었으나
  산출 0건인 잡(daily_nav의 NAV 0건, daily_tracker의 처리 0건)은 success 대신 empty로
  기록하고 daily_health가 경보 — 분기마다 경보를 따로 다는 대신 공통 규약. 새 잡을
  추가할 때 "0건이면 empty"를 적용할 것. ③ **프롬프트↔파서 계약 테스트**
  (`tests/test_ceo_prompt_parser_contract.py`, `test_weekly_picks_prompt_parser_contract.py`):
  프롬프트에서 스키마를 직접 추출해 파서와 대조. 도입 즉시 **실버그 발견 — CEO `exit`
  결정이 프롬프트의 빈 비중 칸(`exit|코드|종목명||이유`) 때문에 `float('')` 예외→`except: pass`로
  항상 조용히 버려지고 있었다**(decision_guard 고신뢰도 액션·생애주기 반영 무력화).
  프롬프트의 출력 스키마를 바꾸면 이 테스트의 `_EXPECTED_FIELDS`와 파서를 함께 갱신할 것.
  ④ **보유 원장 신선도**: 실거래가 미래에셋이라 원장은 사람이 맞춘다. `/holdings confirm`
  (add/remove도 확인으로 간주)이 `holdings.confirmed_at`을 기록, 35일 넘게 확인이 없으면
  월요일 헬스체크에 넛지(최초 실행은 조용히 기준점만 기록). `tests/test_holdings_freshness.py`.
  KIS 나머지 조회(순위·수급류) yfinance 폴백은 동등 데이터가 없어 범위 밖 유지.
- **[설계문제] 추천 시점 가격 기준 "가상 수익률"이 내 실제 손익처럼 알림·리포트에
  노출됐다 (2026-10-02 발견).** 목표가 달성 알림이 `stock_recommendations.entry_price`
  (시스템이 추천 때 기록한 가격) 기준 +10.59%를 "수익률"로 보냈는데, 사용자의 실제
  SK하이닉스는 평단 1,935,965원 → 현재가 기준 약 -5.6% 손실이었다 — 사용자가 "자기가
  추천하고 자기가 잘했다고 강조하는 거냐"고 지적. 가격 기록 자체는 정상(8/28 실제 종가와
  일치)이었고, 문제는 수치의 *의미*(내 매매가 아닌 가상 성과)를 표시도 구분도 안 한 설계다.
  손절 알림만 이미 실제 평단을 쓰도록 고쳐져 있어 목표가 분기만 빠진 비대칭이기도 했다.
  수정: ① 실보유 종목은 `portfolio_positions` 실제 평단 기준 손익을 먼저, 추천가 기준은
  "참고용·실제 매매 아님"으로 아래에(`recommendation_tracker_service.basis_line()` /
  `format_target_alert()`) ② 추천가 기준 수치가 나가는 모든 경로(경보모니터·가격알림·
  `/tracker` 리포트·주간통계·웹 대시보드)에 가상 성과 표시 ③ **정책: 사용자가 매수하지
  않은(미보유) 추천 종목은 텔레그램 알림을 보내지 않는다** — 추적·기록은 조용히 계속해
  학습 루프(적중률·귀인분석 재개 기준)는 유지. 알림은 실보유 종목에만 의미가 있다.
  교훈: 새 알림·리포트에 수익률을 넣을 땐 "누구의 매매 기준인가(내 평단 vs 추천 시점
  가상)"를 먼저 정하고 라벨로 구분할 것. `tests/test_recommendation_tracker_alerts.py`가 회귀 테스트.

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
