# AI 투자 자문 시스템 — 개발·운영 가이드

전속 투자 자문 AI. 한국·미국 시장 데이터를 수집해 텔레그램으로 브리핑을 발송한다.
사용자는 중장기 가치투자자다.

**목표 (2026-10-22 사용자 재정의 — 이전의 "실보유 종목 기반 조언"은 폐기)**: 매일 모든 데이터와 뉴스를 분석해
**시장 분석 → 향후 전망 → 종목 발굴 → 투자 조언**을 전반적으로 하는, 실효성 있는 자문 에이전트.
보유 종목은 조언을 내 계좌에 맞출 때만 쓰는 *일부*일 뿐 분석의 범위가 아니다. 방어 장치(데이터 가드·검증·중복 방지)는
이 목표를 지키는 수단이지 목표 자체가 아니다. 데이터 출처를 계속 넓혀 분석 정확도를 높이는 것이 승인된 방향이다.

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

## 현재 스케줄 (2026-10-22 일일 발굴·공시 요약 추가 후 — 정기 메시지 주 6통 + 평일 일일 요약 5통 + 조건부 1통 + 무발송 잡)

| 시각 | 잡 | 내용 |
|---|---|---|
| 월·수·금 08:20 | pre_market | 장전 브리핑 |
| 화 19:00 | discovery_weekly | 종목 발굴 — **조건부 발송**(진짜 후보 있을 때만, 없으면 무발송) |
| 금 15:00 | rebound_screener | 반등 스크리너 자동 발송 (LLM 미사용, 그 외엔 /rebound 온디맨드) |
| 금 16:30 | close_market | 주간 마감 브리핑 |
| 일 20:00 | weekly_picks | 주간 추천 1통 (국내 중기 + 미국 통합) |
| 일 20:30 | backtest_snapshot | 백테스트 스냅샷 기록 + **학습 고리**(`learning_loop.run_weekly`: 시장예측 소급채점→복기→교훈 저장). 교훈이 새로 생길 때만 `weekly_reflection` 1통 발송(claim 가드) |
| 장중 매 15분 | market_monitor | 이상 신호 시에만 발송 |
| 매일 08:05 | daily_health | 어제 잡 누락·실패 감지, 문제 시에만 경보 |
| 매월 첫째 월 19:00 | monthly_thesis | 월간 투자관 (CEO 브리핑 근거 주입용) |
| 평일 16:10 / 16:20 | daily_nav / daily_tracker | 무발송 데이터 수집 |
| 평일 16:50 | dart_events | 코스피·코스닥 전체 DART 공시(주요사항·거래소공시) 분류·저장, 공급계약/잠정실적은 본문 숫자를 코드가 추출 (무발송) |
| 평일 17:00 | universe_screen | 한국 전 종목(약 1,290개) 스크리닝 스냅샷 `universe_screen_daily` + 추세·상대강도(yfinance 일괄 이력) + **업종 온도계 `industry_daily`**, 약 12분 소요 — 종목 발굴 후보 풀 (무발송) |
| 평일 17:15 | us_universe_screen | 미국 전 종목(시총 $30억+ 약 1,600개) 스냅샷 `us_screen_daily` — SEC EDGAR 재무 + 일괄 시세 + 나스닥 애널리스트 + 내부자 매수, **약 14분 소요** (무발송) |
| 평일 09~18시 30분마다 | holdings_disclosure | **보유 종목 새 공시 즉시 알림**(접수번호별 claim, 해당 없으면 무발송, 실패만 job_runs 기록 — market_monitor와 같은 의도적 예외) |
| 평일 17:40 | daily_digest | **오늘의 발굴·공시 요약 1통** (2026-10-22 사용자 승인, 코드만으로 생성·LLM 없음, 내용 없으면 무발송, 오늘분 스냅샷이 한국·미국 모두 없으면 fail, 스냅샷이 늦으면 최대 10분 대기, 업종 온도계 포함, 금요일엔 스크리너 성적표 `services/screen_scorecard.py` 추가) |
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
  ④ **추천→내 체결 연결(같은 날 추가)**: 의미 있는 추적은 "내가 추천을 보고 실제로 매수한
  뒤"다. `stock_recommendations`에 `fill_price/fill_qty/fill_date` 컬럼을 추가하고, 승인큐
  승인(`approve_draft_position`) 또는 `/holdings add`(`link_recommendation_fill`: 같은 종목·
  추천일≤체결일·45일 이내·미연결·기각 아님인 최근 추천에 자동 연결, 응답에 연결 사실 표시,
  잘못 연결되면 `/holdings unlink CODE`)로 내 체결을 연결한다. 연결된 추천은 `daily_tracker`가
  **내 체결가를 기준가**로 추적(`adopted`)하고, `/tracker` 리포트·웹은 "내가 매수한 추천(내 체결가
  기준)"과 "미채택(가상 성과)"을 분리 표시한다. 기준가가 바뀐 뒤 옛 기준의 최고/최저 수익률이
  섞이지 않게 `_get_max_min_history`는 같은 entry_price 이력만 본다. `/holdings add`로 기존
  보유를 등록할 때 최근 추천과 우연히 연결될 수 있어 unlink를 둠 — 적중률·귀인분석 재개 시
  "채택한 추천의 내 성과"가 핵심 지표(`tests/test_recommendation_fill_link.py`).
  교훈: 새 알림·리포트에 수익률을 넣을 땐 "누구의 매매 기준인가(내 평단 vs 추천 시점
  가상)"를 먼저 정하고 라벨로 구분할 것. `tests/test_recommendation_tracker_alerts.py`가 회귀 테스트.

- **[개선, 2026-10-13] 판단력·진단력 보강 8종 — "막는 장치"에서 "더 잘 판단하게 하는 장치"로.**
  진단: 지금까지 방어(data_guard·decision_guard·risk_gate)는 틀린 숫자를 막는 데 집중했고,
  판단 품질을 올리는 장치(검증·복기·토론·반증)는 형식뿐이었다. 보강 내용 ──
  ① `services/output_fact_check.py`: CEO 브리핑의 단위 붙은 숫자가 입력(프롬프트+컨텍스트)에
  있었는지 사후 대조, 5건 이상 근거 없으면 경고만(발송 안 막음). ② `services/review_facts.py`:
  복기 프롬프트가 LLM에게 "X/10점·승률"을 만들라 하던 걸 요일별·쏠림·표본경고를 코드 계산해
  주입하는 방식으로 교체. ③ data_guard에 `sanitize_market_snapshot`(지수·환율·VIX 오염 제거,
  `_guard_market_data`로 수집 직후 적용)·`sanitize_consensus_gap`(괴리율 -70%/+150% 밖 제거) 추가 —
  종목 데이터만 가드가 있고 CEO 핵심 입력인 시장 스냅샷·컨센서스는 무검증이었다.
  ④ Bull/Bear가 서로 다른 증거를 보고 중립 판정자(`run_judge`, 그래프 노드 `debate_judge`)가
  판정해 CEO에 전달. ⑤ `services/falsification_monitor.py`: 운영 DB의 반증 신호가 "수출 둔화"
  같은 측정 불가 문장뿐이었다 → CEO 프롬프트가 지표+기준값 형태를 요구하고, 코드가 종가선·
  평단 대비 %·KOSPI/환율/VIX/10Y 수준 트리거를 현재값과 비교(CEO 컨텍스트 주입 + 장중 경보 7일 1회).
  ⑥ decision_guard 교차검증 확대(같은 지수 두 피드 차이·S&P/나스닥·SOX/NVDA·TSM), 불일치는
  발송 보류 여부와 별개로 CEO 입력에도 경고로 주입. ⑦ 결과채점: `stock_recommendations.conviction`
  컬럼 추가, 확신도별 실제 적중(calibration)·과신 경고, 주입 게이트에 "서로 다른 추천일 3개+" 추가
  (같은 날 6건은 독립 표본이 아님). 마이그레이션 전 배포 직후에도 조용히 비지 않도록 폴백.
  ⑧ `services/briefing_quality.py`: 상류 리포트 4개+ 비거나 CEO 실패·토론판정 없음이면
  `branch:briefing_quality` fail로 기록 → daily_health가 기존 규약대로 경보(기준값은 초기 추정).
  **프롬프트 변경 주의**: `new`/`hold` 줄의 반증신호 칸 설명문이 바뀌었다(필드 수 불변, 계약 테스트 통과).
  이 칸에 파이프(`|`)를 넣지 말 것. 회귀 테스트: test_output_fact_check, test_review_facts,
  test_falsification_monitor, test_briefing_quality, test_data_guard·test_decision_guard·
  test_bull_bear_debate_team·test_decision_outcome_and_risk_rules 추가분.

- **[개선, 2026-10-13] 학습 고리 — "결과를 기다리는" 구조에서 "과거 판단을 지금 채점하는" 구조로.**
  `services/learning_loop.py`. 진단: 시장 방향 예측 50건 중 22건만 채점돼 있었다(마감 스냅샷이 금요일뿐이라
  월·수 예측은 영영 미채점) + 복기는 매번 백지 시작 + 결과 주입은 종료 8건 게이트 뒤에 잠김.
  ① yfinance ^KS11 일봉으로 미채점 예측 소급 채점(`backfill_market_outcomes`, daily_tracker·주간 잡에서 호출)
  ② 코드가 성적표 계산(적중률, '최빈 방향만 찍기' 기준선, 방향별·확률대별, 큰 움직임) ③ LLM이 오답을 복기해
  분석→사고→판단→교훈→의견 JSON 작성, 입력에 없는 숫자를 쓴 교훈은 코드가 폐기 ④ `learning_lessons`
  테이블(유효 60일)에 저장, `format_for_prompt()`가 CEO 입력에 성적표+교훈 주입(표본 8건 미만이면 무동작).
  첫 실측(50건): 적중 52% = 최빈 방향 기준선 52%와 동일, **'중립' 예측 10건 적중 0%**(KOSPI가 하루 ±1% 이상
  움직인 날이 35/50). ⑤ 자율 조사(`investigate_misses`): ±1% 이상 빗나간 예측의 원인을 그날 시장 조건·수집 정보로
  AI가 '가설:'로 추적해 `market_predictions.miss_reason`에 기록(주 5건 상한, 입력에 없는 숫자는 폐기), 복기에 투입.
  아직 미구현: 종목추천·CEO 비중 결정의 소급 채점, 반증 발동 시 자율 조사.
  **[정정, 같은 날]** `market_predictions.predicted_dir`은 순수 시장 방향 예측이 아니라 CEO **스탠스**(공격/중립/방어)를
  방향으로 환산한 값이다 — 위 '중립 0%'는 공정한 채점이 아니었다. 성적표에 이 한계를 명시했고, 시나리오 확률(우호-비관)
  기울기 적중을 별도 계산한다(표본 7건뿐).
  ⑥ `services/market_regime.py`: KOSPI 일봉으로 최근 20일 변동성·횡보일 수·'전일 ±2% 급변 뒤 재변동 확률'을 코드가 계산해
  CEO 입력에 주입(장중 미확정 봉 제외). ⑦ `services/decision_scoring.py`: cio_decisions_log의 신규편입·축소 판단을 N거래일 뒤
  KOSPI 초과수익으로 소급 채점(첫 실측: 축소·청산 5일 후 7건 중 6건 적중이나 10일 후 3건, 신규편입 1건 오답 — 표본 8건).
  ⑧ `clients/openai_client.chat_independent`: 토론 판정·복기·원인 조사를 주 모델(OpenAI)과 **다른 계열**(OpenRouter
  `OPENROUTER_MODEL_REVIEW`, 기본 google/gemini-2.5-flash)이 수행 — 자기 판단을 같은 모델이 채점하면 편향이 공유된다.
  키 없음·호출 실패 시 주 모델로 폴백. 회귀 테스트: tests/test_learning_loop.py.
  **순환 완성 (같은 날 추가, `services/forecast_loop.py`):** ⑨ 독립 예측가(다른 계열 모델)가 PRE에서 '방향·확률·예상범위·
  틀리는 조건'을 채점 가능한 JSON으로 예측 → `forecast_log` 기록 + CEO 입력에 "참고·반박" 블록으로 주입.
  ⑩ 다음 날 코드가 채점하고 오차(방향 적중/변동폭 과소평가/과신)를 분석한 **즉시 반성 한 줄**을 기록(LLM 없음,
  daily_tracker 16:20 훅). ⑪ 교훈 효과 검증: 교훈 생성 전/후 예측 적중률을 비교해 confirmed/neutral/retired
  (생성 후 표본 8건 미만이면 pending — 결론 안 냄, retired는 자동 비활성). 기준선(전일 방향 반복·최빈 방향) 대비
  우위(edge)와 Brier를 함께 본다. ⑫ 일요일 `weekly_reflection` 1통 = 예측→결과→반성→교훈 효과 + 새 교훈.
  forecast_log·learning_lessons 신규 컬럼은 init_db 마이그레이션(추가만). 회귀 테스트: tests/test_forecast_loop.py.
  **1회성 자동 점검 (절대원칙 5번의 의도적 예외):** 사람이 확인을 잊지 않도록 서버가 날짜 지정 1회 잡으로 스스로 점검해
  텔레그램 보고 — `post_deploy_check`(2026-10-14 09:00: 브리핑 실행·품질·독립 예측가 기록·메모리 최고치·재시작)와
  `post_deploy_result`(2026-10-15 09:00: 예측 채점·반성, 스탠스 대조, 급변 직후 중립 여부, 실패 잡). 정기 잡이 아니라
  job_ledger 기대목록에 넣지 않았다(넣으면 이후 매일 누락 오탐). 지난 날짜면 등록 자체를 건너뛴다. 이후엔 일요일 순환 리포트가 이어받는다.
  `services/memory_watch.py`는 잡 종료마다 컨테이너 RSS를 로그(`[메모리]`)하고 400MB 이상이면 경보(6시간 1회).

- **[개선, 2026-10-14] 자문 에이전트 역량 18종 — "틀린 눈 교정 + 내 계좌 중심 판단 + 학습".**
  계기: yfinance ^KS11 일봉이 10/13 봉을 통째로 빠뜨려(실제 -5.46%) 장전 브리핑이 10/12 값(-3.65%)을 "전일"로
  서술, 같은 브리핑에서 선물 부호·환율 방향도 반대로 썼다. ① **눈**: `clients/naver_client.py`(제2 출처),
  `market_data_client`의 한국 지수 네이버 보강/교체, `services/cross_validate.py`(환율·보유가 교차검증),
  `data_freshness.py`(자산군별 직전 거래일 대비 지연 감사), `investor_flow.py`(외국인·기관 순매수 금액 억원,
  system_settings `flow.*`에 확정치 누적), `briefing_facts.py`(수치 줄 코드 덮어쓰기·전일 급변 경고·환율/선물/수급
  방향 모순 교정·지연 경고 부착). ② **내 계좌**: `portfolio_risk.py`(비중·섹터 쏠림·변동성·VaR·시나리오 손실),
  `account_perf.py`(내 평단 기준 vs 지수), `scenario_engine.py`(/whatif 매도 시나리오, 세율은 가정치),
  `fill_parser.py`+`/ledger`(체결 문자→원장 갱신안, 반드시 사람 확인 뒤 반영 — 자동 반영 금지).
  ③ **판단**: `fair_value.py`(컨센서스 범위+5년 분위, 근거 2개 미만이면 "근거 약함"), `playbook.py`(국면별 사전
  규칙, 사용자 확정 전엔 "기본안" 표시), `event_calendar.py`(FOMC 하드코딩·옵션만기·실적일), 프롬프트에 사전부검·🧭
  섹션. ④ **학습**: `decision_scoring`이 보유유지 판단·확신도까지 채점, `calibration.py`(확신도별 실제 적중률·과신 시
  확신도 남발 금지 지시), `user_decisions`(+`/did`, 내가 따랐을 때 vs 무시했을 때). ⑤ **대화**: `/risk /perf /whatif
  /calendar /playbook /did /ledger`, 봇 AI 대화에도 같은 근거 블록 주입(`services/advisor_context.py`가 브리핑과
  봇의 공통 조립 지점, 블록별 try/except), `behavior_check.py`(물타기·공황매도·쏠림 되묻기), `briefing_mode.py`
  (결정 거리 있는 날만 🧭 충실히). **새 발송 경로·새 스케줄 잡은 없다**(절대원칙 4·5 해당 없음). 테스트에서는
  conftest가 네이버 호출을 차단한다. 이미지 체결 캡처 인식(OCR)은 미구현.

- **[개선, 2026-10-14 오후] 브리핑 간결화 + 체결 캡처 인식.** ① 메인 브리핑은 🧭핵심 판단 4줄까지만, 반론·사전부검·근거 상세는 `【더보기】` 아래로 분리해 `system_settings`(`brief.more`)에 보관하고 `/more`로 조회(`briefing_facts.split_more/save_more`; 사용자 요청 시에만 발송이라 새 발송 경로 아님). 요약 카드는 정상일 땐 침묵(누락 종목 경고만 유지). 한 시안 실측: 40줄/1,700자→26줄/950자. 프롬프트가 지수·가격의 새 절대 레벨을 창작하지 못하게 규칙 추가. ② 체결내역 캡처: 텔레그램 사진/이미지 파일 → `services/fill_ocr.py`(비전 모델은 글자 옮겨 적기만, 매칭·합산·정수/가격 검증·현재가 대조·중복 해시는 코드) → `/ledger ok` 확인 뒤에만 원장 반영(자동 반영 금지). 이미지는 저장·로깅하지 않는다. `clients/openai_client.chat_vision`(OpenAI→OpenRouter 폴백). 라이브 검증: 부분체결 2줄 합산·미체결 제외 정상. 회귀 테스트 tests/test_fill_ocr_and_more.py.

- **[개선, 2026-10-14 저녁] 성적 진단 후속 — 계좌성과 개편·집중도 규칙·주간 추천 관문.** 진단(실측): AI 추천 7건 중 시장(KOSPI)을 이긴 건 1건, 독립 표본은 4종목·3추천일, 계좌 -25%의 시장 몫 ≈ 2/3 · 하이닉스 하나가 손실의 79%(비중 84%). 원인 가설: ① 후보 풀 KOSPI 시총30 고정이라 같은 경기민감 대형주 반복 ② 계좌 84%가 반도체인데 같은 업종 재추천 ③ 지수 7,000선 고점권에 3종목 일괄 추천. 조치: `services/account_perf.py`(시장/종목 몫 분해·손실 기여도·실현손익—2026-08-14 KIS 동기화 사고의 가짜 매도 `memo`에 'KIS 동기화' 포함 건 제외·마지막 NAV 0% 재설정 이후만 비교), `rec_scorecard.py`(추천 시장 대비 성적표, 오래전 종료된 추적가는 현재가로 쓰지 않음), `/성과` 줄마다 시장 대비 병기, `/holdings date 코드 날짜`(매수일 보정), nav_service KOSPI NaN 방지. `services/concentration_rule.py`: 종목 상한 50%·섹터 60%(system_settings `rule.cap.*`, `상한` 명령으로 변경), 위반 시 현금화/재투자 두 경우의 매도 수량·하루 10% 한도 분할 일수를 코드가 계산해 매 브리핑 확정 블록에 항상 노출(LLM 서술 무관), CEO가 위반 종목 추가/신규 편입을 제안하면 리스크 게이트 경고, 원장 매수 시 경고. `services/pick_gate.py`(agents/midterm_agent 연결): 최근 28일 추천·보유·내 쏠림 업종 후보 제외(후보 8개 미만이면 업종 제외 해제), 시장 관문(급락 직후 최대 2·과열 최대 1·평시 3종목, 분할 진입 문구), LLM 출력 후 업종 중복 제거·개수 초과 잘라내기. 미국 추천(us_invest_agent)에는 아직 적용하지 않음. 회귀 테스트: tests/test_account_perf_v2.py, tests/test_concentration_and_pick_gate.py.

- **[개선, 2026-10-21] 리서치 인텔리전스 — 증권사 리포트·중앙은행 문서를 "본문까지 읽고 코드가 검증".**
  발견: 네이버가 리서치 페이지를 stock.naver.com으로 이전해 기존 HTML 스크래퍼가 에러 없이 **조용히 0건**
  (언제부터인지 불명, 메타 패턴 ②와 동형). 제목 키워드 집계(`services/report_digest.py`)만으로는 얕아 본문 분석 구축:
  `clients/securities_report_client.py`(모바일 JSON 목록 `front-api/research/list` + 상세 `api/research/{cat}/{id}`의
  본문·PDF 주소, pymupdf로 PDF 전문 추출, 보유종목 리포트 필터) · `clients/official_docs_client.py`(Fed 성명·의사록,
  한국은행 금통위/보도자료, ECB 원문) · `services/report_analyzer.py`(LLM이 주장·수치 추출 → **코드가 인용문·수치가
  원문에 실제로 있는지 대조해 없으면 폐기**, 검증 주장 0개면 입장 '판단불가', `report_analysis` 테이블에 캐시해
  같은 문서는 한 번만 읽음) · `services/research_synthesis.py`(증권사당 1표 집계, '전망' claim만 투표, 4곳+ 쏠림은
  경고, 보유종목 투자의견·목표가 통계) · `services/research_pipeline.py`(market_intelligence_team이 호출, 결과를
  LLM 서술을 거치지 않고 리포트 뒤에 그대로 덧붙임). 수집 0건/읽기 전멸은 `branch:report_sources`·
  `branch:report_analysis` fail → daily_health 경보. **함정: 네이버 상세 API의 `prevGoalPrice`는 이전 목표가가
  아니라 작성일 주가와 같은 값** — 처음에 "목표가 상향 7건"으로 잘못 집계했다. 목표가 변경은 같은 증권사의
  직전 리포트와 비교해서만 센다(`tests/test_research_pipeline.py`). 한계: claim의 *요약문*은 LLM이 쓴 것이라
  인용문보다 과하게 일반화할 수 있어 출력에 [AI 요약] 표기·원문 인용 병기. 매파/비둘기 성향은 실행마다 달라져 제외.
  한경컨센서스·네이버는 robots.txt가 전체 금지 — 개인용 저빈도로 네이버만 사용 중. 새 발송 경로·스케줄 없음.

- **[개선, 2026-10-21 후속] 공식 통계 대시보드 + 정책 연설 원문.** `clients/official_stats_client.py`(키 없이 되는
  재무부 일간 국채수익률·뉴욕연준 SOFR·BIS 정책금리(한국 포함)·OECD 경기선행지수·BLS CPI/실업률/고용) +
  `services/macro_dashboard.py`(변화폭·YoY·스프레드는 코드 계산, 모든 수치에 기준일, 오래된 시리즈 '⚠오래됨' 표시,
  6시간 프로세스 캐시, 절반 미만 성공이면 `branch:macro_data` fail). `research_pipeline`이 리서치 컨텍스트 맨 앞에 붙인다.
  **함정: ECB HICP(ICP 데이터셋)는 2025-11에서 멈춘 폐기 시리즈**(2026-02 방법론 변경)라 최신처럼 보이지만 아니다 —
  새 시리즈는 as_of가 최근인지 먼저 확인. FRED는 키 없이 접속 불가(타임아웃). official_docs에 Fed 연설·증언,
  뉴욕연준 연구, 샌프란시스코연준, BIS 연설, 일본은행 추가. 사용자 발급이 필요한 키(ECOS·FRED·공공데이터포털·KOSIS·EIA)는 미적용.

- **[개선, 2026-10-21 후속2] 키 필요 공식 통계 3종 연결 — ECOS·FRED·EIA.** `official_stats_client.fetch_ecos/fetch_fred/fetch_eia`
  (키는 환경변수 `ECOS_API_KEY`/`FRED_API_KEY`/`EIA_API_KEY`, 없으면 조용히 건너뜀 — **키 미설정은 `branch:macro_data`
  실패로 집계하지 않는다**: `_collect`가 키 있는 묶음만 dict에 넣고 `format_dashboard`의 total이 그만큼만 늘어난다).
  ECOS=기준금리·CPI·경상수지(백만달러→억달러)·수출(천불, 전년비)·원/달러, FRED=VIX·하이일드스프레드·연방기금금리·달러지수·
  신규실업수당·미시간심리(월간 시리즈는 매월 1일로 찍혀 나오므로 월 단위로 신선도 판정), EIA=WTI·브렌트·원유재고
  (현물가는 주 단위 갱신이라 약 1주 지연 — as_of 병기). 수집은 병렬(스레드), 로그에 키가 새지 않게 예외 문구에서 키를 치환.
  **관세청 수출입(2026-10-21 추가, `fetch_customs`, `DATA_GO_KR_KEY`)**: 처음엔 `nitemtrade`/`Itemtrade` 주소로 호출해
  `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`가 났는데 **키 문제가 아니라 엔드포인트 오류**였다 — 공공데이터포털은 서비스별로
  활용신청이 따로고(승인된 건 수출입총괄·세관별뿐), 수출입총괄의 실제 주소는 `apis.data.go.kr/1220000/Newtrade/getNewtradeList`.
  조회기간은 1년 이내만 가능(초과 시 resultCode 99)해 두 구간으로 나눠 받는다. **진행 중인 당월은 집계 중이라 전년비를 내면
  왜곡 → 직전 완결 월 기준으로만 전년비·무역수지를 내고 당월은 '집계 중' 라벨의 누계만 표시.** 반도체 등 품목별(HS코드)은
  `관세청_품목별 수출입실적(GW)` 활용신청이 추가로 필요(미신청, 코드 없음). KOSIS(`KOSIS_API_KEY`)는 키 유효·통계표 미확정.

- **[개선, 2026-10-21 후속3] 종목 판단서 `services/stock_thesis.py` (`python main.py --thesis 000660`, 출력만 — 발송 경로·스케줄 없음).**
  방향 전환: "틀리지 않게 막는 장치"가 아니라 "그래서 어떻게 하라"를 내놓는 분석. 코드가 모으는 것: 이익 추정(올해·내년·분기)과
  90일 리비전·상향/하향 건수, 목표가·투자의견 쏠림, 동종 비교(PEERS), 내년 EPS×PER 민감도, DART 12년 순이익+주가로 **과거 이익 정점
  때 주가가 어떻게 움직였나**(`cycle_peaks`: 정점 해 연말 낙폭·최저 낙폭·연말 PER·다음 해 수익률), 내 계좌 비중·감축 수량, 서로 어긋나는
  사실 신호(`find_signals`). LLM(기본 OpenRouter `anthropic/claude-sonnet-5.5`, 환경변수 `OPENROUTER_MODEL_THESIS`, 실패 시 CEO 모델)은 이
  사실 블록 위에서 '종목 판단'과 '계좌 판단'을 분리해 서술. **함정:** ① 로컬에서 KIS 403이면 `calculate_pnl`이 현재가를 평단으로 채워
  '손익 0%'가 된다 → 전 종목 현재가==평단이면 계좌 맥락 생략(테스트 고정). ② PER 과거 분포는 주식수를 현재값으로 근사(증자·소각 큰 종목은 어긋남).
  ③ 과거 정점 사례는 표본 2~3개뿐이라 출력에 표본 수를 밝히게 했다. ④ 반도체 사이클의 핵심 선행지표(D램 가격·재고)는 무료 데이터가 없어
  판단서 ⑧번에 '확인 못 하는 것'으로 남는다 — 관세청 품목별(HS 8542) 활용신청이 되면 보강 가능.

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

- **[개선, 2026-10-22] 전방위 자문으로 확장 — 시장 전체 스크리닝·공시·내부자 거래.** 목표 재정의(위 참조) 후 구축.
  ① `services/universe_screener.py`(한국 4,317종목 수신→시총 1,000억+ 약 1,290개 상세, 네이버 JSON, 신호 7종 묶음 점수·흑자 필수)
  ② `services/us_universe_screener.py`(나스닥 공개 목록 6,956종목 → SEC EDGAR frames 일괄 재무로 ROE·이익률·PER을 **코드 계산** +
  yfinance **일괄 시세**(52주·이평) + 나스닥 애널리스트 API(후보 250개만) + 내부자 매수). **함정: 종목별 yfinance `info`를 800~1,500회
  호출하면 Yahoo가 401(Invalid Crumb)/IP 차단으로 62%·0%만 받았다** — 일괄 시세 다운로드와 EDGAR·나스닥으로 우회. 새 미국 데이터 소스는
  종목별 Yahoo 호출을 늘리지 말 것. ③ `clients/sec_insider_client.py`(Form 4 분기 데이터셋, 임원·이사 공개시장 매수만, **발행사 CIK로 집계**
  — 티커는 재사용되고 `N/A`를 쪼개면 가짜 종목이 생겼다; SEC 폴더명이 분기마다 바뀌어 페이지에서 링크를 찾는다; 분기 공개라 최근 몇 주 지연)
  ④ `services/disclosure_events.py`(DART B·I 유형 전체 분류, 거래정지해제·파산신청기각 같은 *해소* 공시는 악재가 아님에 주의, 정정공시 제외,
  단위 못 읽은 금액은 숨김) ⑤ `services/market_ideas.py`가 위 스냅샷을 `advisor_context`에 주입해 CEO 브리핑·봇이 같은 후보 풀을 본다
  ⑥ `services/daily_digest.py` 평일 17:40 요약. **실수 기록: `pytest | tail`은 종료 코드를 가린다 — `set -o pipefail`로 `${PIPESTATUS[0]}`를
  확인한 뒤에만 push할 것(2026-10-22 테스트 4건 실패 상태로 push한 사고).** 회귀 테스트: test_universe_screener, test_universe_screen_daily,
  test_us_universe_screener, test_sec_insider, test_disclosure_events, test_market_ideas, test_daily_digest, test_us_invest_screener_link.

- **[개선, 2026-10-22 오후] 변별력·검증 보강 4종.** ① 한국 스크리너에 **추세·상대강도**(yfinance 일괄 이력 → 3개월 수익률 전 종목 내 순위, 50일선 회복, 200일선 훼손·단기 과열 경고) —
  시장 전체가 고점 대비 -40~60%라 낙폭·저평가 신호만으로는 변별이 안 됐다. 이력이 50% 미만 수신되면 조용히 열화하지 않고 잡을 fail 처리한다.
  ② `services/industry_board.py` **업종 온도계**: 업종별 3개월 상대강도·200일선 위 비율·이익 증가 기대·외국인 매수 비율을 코드가 집계하고 *시장 전체 대비*로
  강세/회복초입/약세 분류(지수 수준과 무관), 매일 `industry_daily`에 쌓아 변화를 본다 — 전망 엔진의 근거 시계열. ③ `services/screen_scorecard.py` **스크리너 성적표**:
  과거 후보(3묶음+)의 N거래일 뒤 수익률 vs 같은 기간 *전 종목 중앙값*(외부 지수 불필요), 30건 미만이면 '표본 부족' 표시. 신호 가중치 조정의 근거(데이터가 쌓여야 의미).
  ④ `services/holdings_alert.py` 보유 종목 공시 즉시 알림. 회귀 테스트: test_industry_board, test_screen_scorecard, test_holdings_alert, test_universe_screener(추세).

- **[신호 검증 결과, 2026-10-22] 새 신호를 점수에 넣기 전에 반드시 과거 검증을 거쳐라** (`scripts/backtest_price_signals.py [kr|us]`, `scripts/backtest_event_signals.py insider|dart|dart2|usbuy`).
  방법: 신호가 켜진 시점의 20거래일 뒤 수익률 vs **같은 시점 전 종목 중앙값**, 시점별 일관성(이긴 시점 비율·t값)으로 판정 — *평균* 초과수익은 쓰지 마라(무작위 종목 평균−중앙값이 구조적으로 +3~4%p).
  결과(2024-10~2026-10, 생존 편향·겹치는 표본·2년이라 '방향성 참고'): ✔ **한국 자사주 취득·소각·배당 결정 공시만 통과**(20일 +1.62%p, 이긴 시점 64%, t=+3.2) → 점수 신호 + 요약 전용 섹션 + 발굴 후보 풀 직접 편입.
  ✘ 한국 상대강도(t=-0.9)·추세회복(이긴 시점 21%, t=-2.8)·낙폭 → 점수 제외. ✘ 미국 가격 신호 전부(|t|<1, 단기회복 t=+0.6) 제외. ✘ 미국 임원·이사 내부자 매수 군집(강한 군집 60일 t=-3.8, 오히려 부진) → '(참고)' 표시만.
  ✘ 한국 잠정실적 호조(영업이익 YoY +100%↑ 발표 후 60일 -4.26%p, t=-2.8 — 좋은 실적 후 오히려 부진)를 '호재'로 분류하던 규칙 폐기, 적자전환만 악재. ✘ 한국 대형 공급계약(매출대비 10%+) 효과 없음 → '(참고·미검증)'.
  ✘ **미국 자사주 매입 승인 8-K**(SEC 전문 검색, 단독 공시 60일 -1.91%p, |t|<2) 효과 없음 → 미국엔 검증된 사건 신호가 아직 없다. 재무·컨센서스·수급 신호는 과거 스냅샷이 없어 검증 불가 →
  매일 쌓는 `universe_screen_daily`/`us_screen_daily`(추정EPS·의견 시계열 포함)와 금요일 성적표(`services/screen_scorecard.py`)로 앞으로 검증. 요약·LLM 컨텍스트 머리말에 '검증되지 않은 후보' 고지가 들어 있으니 지우지 말 것.
- **[메모리 안전장치, 2026-10-22]** Render 무료 플랜 512MB(스케줄러+웹이 ~290MB 사용). 미국 스크리너가 내부자 거래 데이터를 파일 통째로 읽어 순간 +150MB(잡 최대 292MB)를 써서 스트리밍으로 교체(최대 183MB).
  대형 배치는 묶음 사이에서 `memory_watch.guard()`를 호출해 460MB 이상이면 스스로 중단(MemoryError→잡 fail→헬스체크 경보)한다 — OOM으로 서비스 전체가 조용히 재시작되는 것(10/12 전례)을 막기 위함. 새 대형 배치를 추가할 땐 같은 가드를 넣고 로컬에서 최대 메모리를 측정할 것.

- **[검증 정정, 2026-10-22 저녁] 한국 '저PER·저PBR 통과'는 금융주 랠리가 섞인 결과였다.** `scripts/backtest_fundamental_signals.py`(DART 사업보고서 FY2024·2025, 공개 시점 이후만 사용, 약 17개월):
  금융업(은행·증권·보험 등 59개 종목)은 60일 뒤 +6.81%p(이긴 시점 84%, t=+5.1)로 압도적이라 '부채비율 300%+'(금융업의 구조적 특징)도 가치와 무관하게 t=+5.5로 나왔다.
  **금융을 뺀 결과**: 비금융 PER≤10 60일 +1.36%p·이긴 시점 53%·t=+2.1(약한 지지), 비금융 PBR<1 +0.68%p·47%·t=+1.3(지지 안 됨), PBR<0.5 t=+2.1. ROE 10%+ 단독·순이익 +20% 단독은 효과 없음.
  → 코드 반영: 저평가(PER≤10)는 **비금융에만 ✔**, 자산대비저평가(PBR)는 ✔ 없음, 수익성(ROE)은 참고(info). 금융업 여부는 실행 시점의 네이버 업종 이름(`FIN_KEYWORDS`)으로 판정, 업종을 못 받으면 ✔를 달지 않는다.
  교훈: **업종 구성이 편중된 신호(고부채·저PBR 등)는 반드시 섹터를 떼어 재검증**할 것. 2025~26 한국은 금융주·밸류업 랠리라는 시기 특유의 효과가 있어 17개월 결과를 일반화하지 말 것.
- **[검증 정정 2, 2026-10-22 저녁] 한국 '검증된 공시 호재'는 자사주 *취득* 하나뿐이다.** DART 주요사항보고서(B) 목록엔 자사주소각·배당결정이 없어 표본이 0건이었다(앞서 '취득·소각·배당 묶음'이라 부른 결과는 사실상 취득 단독).
  금융을 뺀 자사주 취득: 20일 +1.74%p·이긴 시점 67%(t=+3.4), 60일 +2.58%p(t=+2.5)로 견고. 소각·배당은 '(참고·미검증)'(필요하면 I유형 거래소공시 목록으로 별도 검증). 악재 묶음(유증·메자닌·감자)은 비금융도 20일 -1.79%p·60일 -3.20%p, 이긴 시점 39~40%(경고 유지).
  `disclosure_events.VALIDATED_GOOD_TYPES`·`market_ideas._VALIDATED_GOOD`은 ("자사주취득")만.

- **[개선, 2026-10-23] 업종 전망 엔진 `services/sector_outlook.py` — 전망을 기록하고 채점하는 구조.** 평일 17:00 `universe_screen` 잡 끝에서 실행(새 잡·새 발송 경로 없음):
  업종 온도계 값에서 코드가 근거(컨센서스PER 시장대비 할인·이익기대·외국인 매수 비율·200일선 비율 변화)를 계산 → 독립 예측가(다른 계열 모델)가 우호/비우호 업종 각 최대 4개를
  확률·틀리는 조건과 함께 판단(가격 모멘텀은 검증상 예측력 없어 근거에서 제외, 근거에 없는 숫자를 쓴 판단은 코드가 폐기) → `sector_outlook` 테이블 기록 → 20거래일 뒤
  업종 구성원 수익률 중앙값 − 전 종목 중앙값(±1%p 밴드 밖일 때만 적중)으로 채점. 채점용으로 `universe_screen_daily.industry` 컬럼 추가(오늘 이후 스냅샷부터 채워짐 —
  첫 채점은 최소 20거래일 뒤). 결과는 `advisor_context`가 CEO·봇에 "참고·반박 대상, 확신 근거 금지"로 주입하고 채점 30건 미만이면 '표본 부족'을 밝힌다.
  전망 단계 실패는 잡을 fail시키지 않되 job detail에 `⚠업종 전망 실패`로 남는다. 회귀 테스트: tests/test_sector_outlook.py.
- **[신호 검증, 2026-10-23] 소각·배당 공시 — 둘 다 검증 통과 못 함** (`scripts/backtest_event_signals.py dart3`, 거래소공시 I유형 24개월, 접수일 다음 거래일 진입, 소각 883건·배당결정 3,352건).
  자사주 소각: 비금융 20일 -0.31%p·이긴 시점 49%(t=+0.4), 60일 +0.46%p(t=+0.9) — 효과 없음. 배당결정: 비금융 20일 +0.74%p(t=+1.2), 60일 +0.63%p(t=+1.4) — 방향은 양(+)이나 약해 통과 기준 미달;
  금융만 60일 +6.53%p(t=+2.6)는 금융주 랠리 시기 효과라 제외. → `VALIDATED_GOOD_TYPES`는 계속 ("자사주취득")만. 소각·배당은 '(참고·미검증)' 유지.
  참고: I유형에서 자사주취득은 61건뿐(B유형이 본체)이라 표본 부족. 새 검증 모드를 스크립트에 추가했다 — `dart3`.
- **[버그, 2026-10-23] 10/23 장전 브리핑 두 결함 — 포맷 변형(볼드)이 정규식을 빗나가 조용히 틀린 값이 나갔다 (메타 패턴 ①과 동형).**
  ① LLM이 `**수치:** …`로 쓰자 `briefing_facts.apply`의 `^수치\s?:` 정규식이 놓쳐, LLM이 지어낸 수치 줄(S&P500선물 -1.18%)과 코드 확정 줄(-0.54%)이 둘 다 나갔다 → 볼드·불릿 변형 허용.
  ② 본문엔 "**줄일 것:** 하이닉스 83%→50% / **팔 것:** TIGER ETF 전량"이 있는데 요약 카드는 "오늘은 사고팔 것 없음" — 구조화 블록(cio_decisions_log)이 비어 카드가 빈 dict로 조립됐고,
  누락 경보(`_find_untracked_action_line`)는 "줄일 것·팔 것" 합친 꼴만 인식해 침묵했다 → 정규식이 분리·볼드 변형을 잡고, 누락이 감지되면 카드가 "사고팔 것 없음" 대신 확인 요청 문구를 낸다.
  **남은 의문:** 왜 구조화 블록이 비었는지(LLM이 블록을 빠뜨렸나, 파싱이 실패했나)는 운영 로그 없이는 확인 못 함 — 다음 브리핑에서 경보가 오면 그 원인을 추적할 것. 회귀 테스트: test_briefing_facts, test_ceo_untracked_action_alert.
  **후속(같은 날, 일관성 점검):** 구조화 블록이 없으면 `_parse_cio_decisions`의 **기본값(스탠스 중립·현금 30%)이 요약 카드에 'CEO의 한 줄 결론'처럼 나갔다**(본문은 비관 85%·방어 — 정반대).
  `stance_parsed` 플래그를 추가해 stance 줄을 실제로 읽었을 때만 결론 톤을 표시하고, 아니면 "구조화 기록이 없어 요약을 생략" 문구를 낸다. 블록 마커 정규식은 코드펜스·볼드 변형을 허용하고,
  인식 실패 시 응답 끝 200자를 `[CIO] 결정 로그 블록 인식 실패` 경고 로그로 남겨 다음에 원인(LLM 누락 vs 포맷 변형)을 구분할 수 있게 했다. 교훈: **파싱 실패 시의 기본값은 '판단'이 아니라 '없음'으로 표시해야 한다.**
- **[신호 검증, 2026-10-23] 한국 '싸다'는 업종 안에서 비교해야 의미가 있었다** (`python scripts/backtest_fundamental_signals.py neutral`, 비금융·업종 표본 8개+, 확정 실적, 약 17개월).
  업종 중앙값의 70% 이하: PBR 20일 +0.64%p(이긴 시점 72%, t=+3.4)·60일 +1.34%p(69%, t=+4.0), PER 20일 +0.45%p(67%, t=+2.4)·60일 +1.56%p(62%, t=+3.3). 대비: 시장 전체 기준 PBR<1은 t=+1.3(효과 없음).
  ROE 업종 상위·순이익 성장 업종 상위는 효과 없음(t=-0.9/-1.2), '업종 대비 비싸고 수익성 낮음'은 60일 -0.83%p(t=-2.8, 이긴 시점 41%)로 반대 방향 확인. **주의: 시점을 10거래일 간격으로 겹치게 뽑아 t가 부풀려져 있다
  (20일 ≈√2배, 60일 ≈√6배) — 보정하면 약 1.6~2.4의 약~중간 지지.** 반영: `업종대비저평가`(PER)와 신설 `업종대비저PBR`에 비금융만 ✔(같은 '가치' 묶음이라 점수·✔ 개수 부풀리지 않음). 금융·분류 불명은 ✔ 없음.
  `industry_medians`는 같은 dict에 `pbr:업종코드` 키로 업종 PBR 중앙값을 보관한다. 새 검증이 가장 통과한 신호이지만 17개월·밸류업 시기 한계는 그대로 — 성적표(금요일)가 쌓이면 전진 검증할 것.

## 최종 목표 로드맵 (2026-10-23 정리 — 개별 수정이 방향을 흐리지 않게 이 틀에 맞춰 진행)
목표: 매일 **시장 분석 → 전망 → 종목 발굴 → 투자 조언**이 한 흐름으로 사용자 눈에 보이는 자문 에이전트.
| 단계 | 재료(코드) | 사용자에게 보이는 곳 | 상태 |
|---|---|---|---|
| 시장 분석 | 전종목 스크리닝·업종 온도계·공시·증권사 리포트·공식 통계 | 브리핑 본문·`daily_digest`·봇 | 갖춤 |
| 전망 | 독립 예측가(KOSPI 일간)·업종 전망(`sector_outlook`) → 채점 | 브리핑 끝 `🧭 업종 전망` 섹션 | 10/23 가동, 첫 채점 ~11월 말 |
| 종목 발굴 | 스크리너 후보 + 검증된 신호(✔) | 브리핑 끝 `🔎 발굴 후보`·`✔ 자사주 취득 결정` 섹션(`services/briefing_sections.py`) | 10/23 가시화 |
| 투자 조언 | CEO 브리핑(내 계좌 기준: 집중도·평단·위험) | 브리핑 상단 | 운영 중, 품질 점검 계속 |
| 학습 | 예측·추천·후보 성적표 → 신호 가중치·교훈 | 금요일 성적표·일요일 `weekly_reflection` | 데이터 축적 중 |
원칙: 재료를 만들면 **사용자가 보는 곳까지 연결**하고, 연결됐는지(`branch:briefing_sections` 등)를 헬스체크가 본다. 새 신호는 검증 후 ✔, 검증 전이면 '검증 전' 표시.
- **[설계문제→개선, 2026-10-23] 전망·발굴이 CEO 입력에만 있고 사용자 눈엔 안 보였다** (CEO 템플릿에 전망·발굴 칸이 없어 CEO가 신규 편입을 결정하지 않으면 노출 0 — "계산은 되나 안 보임" 패턴의 네 번째).
  `services/briefing_sections.py`가 스냅샷에서 코드로 줄을 만들어 PRE·CLOSE 브리핑 끝에 붙이고(보유 종목 제외, 쉬운 말, ✔ 표시, 매수 권고 아님 고지), 전부 비거나 소스가 실패하면 `branch:briefing_sections` fail로 기록한다.
  알려진 한계: 자사주 취득 검증은 비금융 기준인데 목록엔 금융사가 섞일 수 있음(스냅샷에 금융 여부 없음). 회귀 테스트: tests/test_briefing_sections.py.
