# AI Investment Research Company

> AI 기반 투자 리서치 회사 시스템  
> 매일 장전/장중/장마감에 텔레그램으로 CEO 투자 브리핑을 수신합니다.

---

## 시스템 구조

> 이 문서는 신규 설치 가이드다. 현재 운영 중인 정확한 스케줄·아키텍처·알려진
> 함정은 `CLAUDE.md`가 항상 최신 기준이니 그쪽을 먼저 확인할 것 (2026-09-11 갱신).

에이전트는 30개(agents/), 계산·저장 로직은 31개(services/), 외부 연동은
19개(clients/)로 구성돼 있다. 핵심 브리핑 파이프라인(`graph/investment_graph.py`)
흐름은 다음과 같다:

```
collect_raw_data (시장·KIS·뉴스·DART·컨센서스 수집)
    │
    ├─ [병렬] futures_market_team · us_global_team · news_analysis_team ·
    │         bigfigure_agent · macro_team · event_risk_team
    │         └─ l2_barrier(fan-in) → market_intelligence_team (순차)
    │
    ├─ [병렬] korea_flow_team (국내 수급/섹터/자금흐름 통합)
    │         └─ l3_barrier(fan-in) → issue_stock_agent (순차)
    │
    └─ [순차] risk_management_team → review_feedback_team(CLOSE만) →
              investment_committee → portfolio_manager_agent →
              midterm_stock_agent → bull_case → bear_case → ceo_agent
                  │
                  ▼
        save_report → deep_report → record_nav(CLOSE만) → send_telegram
                  (decision_guard 교차검증 → risk_gate 비중검사)
```

이 외에 discovery_agent(종목발굴)·rebound_screener_agent(반등스크리너)·
attribution_agent(귀인분석)·thesis_agent(월간 투자관) 등은 메인 그래프에
배선되지 않고 스케줄러/CLI에서 개별 호출된다. 전체 30개 에이전트 목록과
현재 어떤 게 자동 스케줄에 있고 어떤 게 수동 전용인지는 `CLAUDE.md`의
"아키텍처 지도"·"일시 중단" 절 참조.

---

## 설치

### 1. Python 환경 준비

```bash
# Python 3.11+ 필요
python --version

# 가상환경 생성 (권장)
python -m venv venv
source venv/bin/activate        # Linux/Mac
# 또는
venv\Scripts\activate           # Windows

# 패키지 설치
pip install -r requirements.txt
```

### 2. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 열고 아래 항목을 입력하세요:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini

TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=123456789

KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=12345678         # 계좌번호 앞 8자리
KIS_ACCOUNT_PROD_CD=01
```

### 3. DB 초기화

```bash
python main.py --init-db
```

### 4. 환경변수 검증

```bash
python main.py --check
```

---

## 실행

### 수동 실행

```bash
# 장전 브리핑 (08:20)
python main.py --type pre

# 장중 1차 (10:00)
python main.py --type intra1

# 장중 2차 (13:00)
python main.py --type intra2

# 장마감 복기 (15:50)
python main.py --type close
```

### 자동 스케줄 실행

```bash
python scheduler.py
```

현재 실제 운영 스케줄(2026-09 기준, 정기 발송 6통/주 + 조건부 1통):
- `월·수·금 08:20` — 장전 브리핑
- `금 16:30` — 주간 마감 브리핑
- `일 20:00` — 주간 추천 (국내 중기 + 미국 통합)
- `화 19:00` — 종목 발굴 (조건부 발송, 후보 있을 때만)
- `금 15:00` — 반등 스크리너
- `장중 15분마다` — 시장 모니터 (이상 신호 시에만)
- `매일 08:05` — 헬스체크 (문제 있을 때만)

이 스케줄은 자주 바뀐다 — 최신 표는 항상 `CLAUDE.md`의 "현재 스케줄" 절을 볼 것.
장중 1차/2차(intra1/intra2)는 2026-06-23부터 자동 스케줄에서 빠졌고,
`python main.py --type intra1` 등으로 수동 실행만 가능하다.

---

## GitHub Actions + cron-job.org 스케줄 설정

GitHub Actions schedule은 수 시간 지연이 발생할 수 있습니다.
**정확한 시간 보장**을 위해 cron-job.org에서 `repository_dispatch` 이벤트를 직접 호출합니다.

### 1. GitHub Fine-Grained PAT 발급

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens

- **Repository access**: `nine0659/ai-investment-company`
- **Permissions**: `Actions` → Read and write (또는 `Contents` → Read and write)
- **Expiration**: 만료일 설정 후 갱신 필요

### 2. cron-job.org 잡 설정 (총 4개)

각 잡마다 아래 설정을 사용합니다:

| 잡 이름 | Cron (KST) | `event_type` |
|---|---|---|
| 장전브리핑_0820 | `20 8 * * 1-5` (KST) | `pre_market` |
| 장중1차_1000 | `0 10 * * 1-5` (KST) | `intra1` |
| 장중2차_1300 | `0 13 * * 1-5` (KST) | `intra2` |
| 장마감복기_1550 | `50 15 * * 1-5` (KST) | `close_market` |

**cron-job.org 잡 공통 설정:**

- **URL**: `https://api.github.com/repos/nine0659/ai-investment-company/dispatches`
- **Method**: `POST`
- **Headers**:
  ```
  Authorization: Bearer <발급한_PAT>
  Content-Type: application/json
  Accept: application/vnd.github+json
  X-GitHub-Api-Version: 2022-11-28
  ```
- **Request body** (잡마다 `event_type` 변경):
  ```json
  {"event_type": "close_market"}
  ```
- **Expected HTTP status**: `204` (No Content) = 성공

### 3. 설정 검증

cron-job.org 잡을 수동 실행한 뒤 GitHub Actions 탭에서 `repository_dispatch` 이벤트가 트리거됐는지 확인하세요.

또는 터미널에서 직접 테스트:

```bash
gh api --method POST repos/nine0659/ai-investment-company/dispatches \
  -f event_type=close_market
```

이 명령이 `204`를 반환하면 GitHub 측 설정은 정상입니다. cron-job.org에서 동일한 API 호출이 성공하지 않으면 PAT나 헤더 설정을 다시 확인하세요.

---

## 클라우드 서버 배포 (systemd)

```bash
# /etc/systemd/system/ai-investment.service 생성
sudo nano /etc/systemd/system/ai-investment.service
```

```ini
[Unit]
Description=AI Investment Research Company Scheduler
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ai-investment-company
ExecStart=/home/ubuntu/ai-investment-company/venv/bin/python scheduler.py
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-investment
sudo systemctl start ai-investment
sudo systemctl status ai-investment
```

---

## 테스트

```bash
# 전체 테스트
python -m pytest tests/ -v

# 특정 테스트
python -m pytest tests/test_kis_client.py -v
python -m pytest tests/test_agents.py -v
```

---

## 폴더 구조

```
ai-investment-company/
├─ README.md                  설치·실행 가이드 (이 문서)
├─ CLAUDE.md                  운영 가이드 — 최신 아키텍처·스케줄·알려진 함정 (항상 최신)
├─ .env.example
├─ requirements.txt
├─ main.py                    수동 실행 (브리핑·리서치·포트폴리오·워치리스트·기록)
├─ scheduler.py                자동 스케줄 실행 (Render 상주 프로세스)
├─ config/
│  └─ settings.py             환경변수 및 전역 설정
├─ data/logs/                 로그 파일
├─ db/
│  └─ database.py             SQLAlchemy 테이블 정의 (Neon Postgres / SQLite 폴백)
├─ clients/                   외부 연동 19개 — kis/dart/openai/telegram(client+bot)/
│                             yfinance 계열/뉴스/증권사리포트/카카오 등
├─ agents/                    분석·판단 에이전트 30개 — ceo_agent가 핵심,
│                             나머지는 그래프에 배선된 것과 스케줄러/CLI에서
│                             개별 호출되는 것으로 나뉜다 (CLAUDE.md 참조)
├─ graph/
│  ├─ state.py                LangGraph 상태 정의
│  └─ investment_graph.py     메인 분석 파이프라인 (병렬 수집 → 순차 심의 → 발송)
├─ services/                  계산·저장 로직 31개(LLM 미사용) — data_guard·
│                             decision_guard·risk_gate·job_ledger가 핵심 가드레일
├─ scripts/                   운영 보조 스크립트 — backtest_gate_check.py(추천 로직
│                             변경 전 점검), judgment_scenario_check.py(판단 로직
│                             변경 전 시나리오 체크리스트) 등
├─ web/
│  └─ app.py                  FastAPI 24/7 웹 대시보드
└─ tests/                     회귀 테스트 48개 — 전부 과거 실제 사고 재발 방지용
```

---

## 보안 주의사항

- `.env` 파일은 절대 커밋하지 마세요 (`.gitignore`에 포함됨)
- `data/kakao_tokens.json`, `data/kakao_pkce.json`, `*.session` 파일도 커밋하지 마세요
- `.env.example`만 커밋합니다
- API Key는 코드에 직접 입력하지 마세요
- 로그에 민감정보가 출력되지 않도록 설계되어 있습니다
- 웹 대시보드는 `/health`, `/api/status`를 제외하고 `WEB_PASSWORD`가 있어야 접근됩니다

---

## 면책 조항

> 본 시스템은 투자 참고용 정보 수집 및 분석 도구입니다.
> 본 리포트를 기반으로 한 투자 결정과 그에 따른 손익은 전적으로 투자자 본인의 책임입니다.
>
> **브리핑 판단에 따른 자동 매매(자동 주문 실행)는 없습니다.** 드로다운 등 위기
> 상황에서도 시스템은 경보만 보내고 절대 스스로 매도하지 않습니다(2026-07-09
> 사용자 승인 정책, `tests/test_drawdown_policy.py`가 강제).
>
> 현재 운영 원칙은 **KIS API로 시세·잔고 정보를 참고하고, 실제 매수·매도는
> 미래에셋증권 계좌에서 사용자가 직접 실행한 뒤 결과를 기록**하는 방식입니다.
> `ENABLE_KIS_TRADING=true`를 명시하지 않으면 KIS 주문 API는 코드 레벨에서 차단됩니다.
