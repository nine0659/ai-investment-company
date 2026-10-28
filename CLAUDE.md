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

## 지켜야 할 규칙 요약 (전부 실제 사고에서 나왔다 — 근거·경위·회귀 테스트 이름은 [docs/CLAUDE_HISTORY.md](docs/CLAUDE_HISTORY.md))

새 항목을 추가할 때는 맨 앞에 `[버그]`(설계는 맞는데 구현이 틀림) 또는 `[설계문제]`(구현은 의도대로인데 구조가 사고를 유발 — 재발 방지책까지 필요)를 붙이고, 상세 경위는 HISTORY에, 여기엔 규칙 한 줄만 남긴다.

**병렬·그래프**
- LangGraph 병렬 노드는 전체 state가 아니라 **바뀐 필드만 담은 델타 dict**를 반환(`_parallel()` 참조). `errors`는 병렬 구간에서 리듀서에 넘기지 말 것. 병렬↔순차로 옮기면 무변화 감지(`_track_sequential`)도 같이 옮길 것.

**데이터 정확성**
- DART `history[0]`은 분기 보고서일 수 있다 — 분기·연간 손익을 섞지 말 것. yfinance `dividendYield`는 `_normalize_dividend_yield()` 경유.
- 알파/수익률은 같은 시작점·같은 기간끼리. 보유 누적수익률 ≠ 주간수익률 — 프롬프트에 라벨 명시.
- NAV 이상 판정은 총평가 원값이 아니라 **평가배율(value/cost)**.
- 추천가 기준 수익률은 가상 성과다 — 새 알림·리포트에 수익률을 넣을 땐 "누구의 매매 기준인가"를 라벨로 구분. 미보유 추천은 텔레그램 알림 금지(추적·기록만).
- 실시간 조회는 단일 소스·전부-아니면-전무로 짜지 말 것(KIS→yfinance 폴백, 종목별 실패 구분). 미국 데이터는 종목별 Yahoo 호출을 늘리지 말 것(일괄 시세·EDGAR·나스닥 사용).
- ECB HICP(ICP)는 폐기 시리즈, 네이버 `prevGoalPrice`는 이전 목표가가 아님 — 새 시리즈는 as_of부터 확인.

**실보유·자동화 금지선**
- 이 사용자의 실거래는 **미래에셋**이다(KIS 잔고는 0이 정상). KIS 잔고로 보유를 자동 종료하는 `sync_from_kis` 재도입 금지, 드로다운 자동매도 금지(경보만) — 재도입은 사용자 승인 필수. 신뢰 못 할 신호로 실보유 데이터를 자동 파괴하지 말 것.
- 체결 문자·캡처는 반드시 사람 확인(`/ledger ok`) 뒤에만 원장 반영.

**조용한 스킵 방지 (메타 패턴 — 한 분기에 경보를 달았다고 안심하지 말 것)**
- "성공도 실패도 아닌 분기"(보유 0개, 추적 0건, 빈 후보 풀, 발송 보류)는 반드시 흔적을 남긴다: 0건이면 `job_runs.status='empty'`, 데이터 수집 실패·발송 보류는 `fail`(또는 `branch:*` fail → daily_health 경보).
- 파싱 실패 시 기본값은 '판단'이 아니라 '없음'으로 표시(`stance_parsed` 등).
- LLM 출력을 파싱하는 코드: 탐욕 정규식(`\{.*\}`) 대신 `services/llm_json.extract_json`, 줄 서식 변형(불릿·볼드·번호·전각 파이프·%)은 정규화. 프롬프트의 출력 스키마를 바꾸면 계약 테스트(`test_*_prompt_parser_contract.py`)의 `_EXPECTED_FIELDS`와 파서를 함께 갱신. 새 파서는 단위 테스트 외에 **실모델로 1회 시험**해 유효 건수 > 0 확인.

**인프라·배포**
- DATABASE_URL 누락·`+psycopg` 스킴은 SQLite 폴백(데이터 증발)을 부른다 — 스킴은 `_make_engine()`이 정규화. GH Actions 신규 잡엔 `DATABASE_URL` 시크릿 주입.
- push 성공·CI green ≠ 배포 성공. Render는 배포 실패 시 옛 컨테이너를 계속 돌린다 — `/api/status`의 `commit`으로 확인(`deploy-check.yml`이 자동 비교). 환경변수(`WEB_PASSWORD` 등) 누락이 대표 원인.
- GH Actions cron은 정시에 안 돈다(정시성 필요한 잡은 Render). Render 재시작을 넘긴 APScheduler 실행은 증발한다(GH 백업이 job_runs 흔적을 보고 대신 실행). cron-job.org 트리거는 저장소 밖에 있으니 스케줄 축소 시 함께 정리.
- 워크플로 YAML에 `python -c "` 멀티라인 인라인 금지(스크립트 파일로). Windows 콘솔은 cp949 — `PYTHONUTF8=1`. 텔레그램 4096자 분할은 `send_message`가 처리. KIS 토큰 서킷 브레이커·KISClient 생성 try/except 유지.
- 메모리: Render 무료 512MB(스케줄러+웹 ~290MB). 대형 배치는 묶음 사이에 `memory_watch.guard()`를 넣고 로컬 최대 메모리를 측정. 일별 최고치는 `memory_watch.history()`(DB `mem.peak.history`).
- 교차검증 발송 보류(`decision_guard`)는 `branch:send_gate` fail로 기록된다. 보류되면 슬롯이 풀리므로 `report_claims`에 그날 슬롯이 없다.

**판단·학습 품질**
- 수치 계산은 코드가, LLM은 서술만. 괄호 밖 사실 주장은 `output_fact_check`·`briefing_facts`가 사후 대조·교정(발송은 막지 않음).
- 자기 판단의 채점·복기·토론 판정은 **다른 계열 모델**(`chat_independent`)이 한다. 추천/목표가/손절 로직을 바꾸기 전엔 `scripts/backtest_gate_check.py`.
- **새 신호는 점수에 넣기 전에 과거 검증**(`scripts/backtest_*_signals.py`): 신호 켜진 시점 N일 뒤 수익률 vs **같은 시점 전 종목 중앙값**, 시점별 일관성(이긴 시점 비율·t값). 평균 초과수익은 쓰지 말 것. 업종 구성이 편중된 신호는 섹터를 떼어 재검증(금융주 랠리 오염 전례). 시점이 겹치면 t가 부풀려진다. 현재 ✔ 검증 통과: **한국 자사주 취득 공시**, **비금융 업종 대비 저PER·저PBR**. 소각·배당·잠정실적 호조·미국 가격/내부자/자사주 신호·한국 상대강도·추세·낙폭은 통과 못 했다(점수 제외, '(참고·미검증)' 표시).
- 업종 전망·후보 행동안은 기록→20거래일 뒤 채점 구조이며 채점 30건 미만이면 "표본 부족"을 밝힌다. 비중(3%/2%)·손절(-15%) 상수는 초기값 — 채점으로 조정.
- 재료를 만들면 **사용자가 보는 곳까지 연결**하고 연결 여부를 헬스체크가 보게 할 것("계산은 되나 안 보임" 패턴 4회 전례).
- 집중도 규칙: 종목 상한 50%·섹터 60%(`상한` 명령), 위반 시 매도 수량·분할 일수를 코드가 계산해 브리핑에 항상 노출.
- 1회성 점검 잡(`post_deploy_*`)은 절대원칙 5번의 의도적 예외(job_ledger 기대목록에 넣지 않음).

**기능 이력(무엇이 어디 있나)**: 판단력 보강 8종·학습 고리·자문 역량 18종·리서치 인텔리전스·공식 통계·종목 판단서(`--thesis`)·전방위 스크리닝·업종 온도계·업종 전망 엔진·후보 행동안의 설계 경위와 실측 수치는 모두 [docs/CLAUDE_HISTORY.md](docs/CLAUDE_HISTORY.md)에 있다.

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
