# AI Investment Research Company

> AI 기반 투자 리서치 회사 시스템  
> 매일 장전/장중/장마감에 텔레그램으로 CEO 투자 브리핑을 수신합니다.

---

## 시스템 구조

```
CEO Agent
├─ Futures Market Team       선물/환율/금리 분석
├─ US Market Team            미국 지수/반도체 분석
├─ Korea Spot Market Team    KIS API 기반 실시간 종목 탐지
├─ Global Market Team        글로벌 시장/아시아/달러 분석
├─ News Analysis Team        뉴스 재료 분석
├─ Sector & Theme Team       섹터 강도/순환매 분석
├─ Money Flow Team           수급 집중 종목 점수화
├─ Risk Management Team      리스크 경고/손절 기준
├─ Review & Feedback Team    복기 및 개선점 생성
└─ Investment Committee      팀 의견 점수화 → CEO 판단
```

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

평일(월~금) 자동 실행:
- `08:20` — 장전 CEO 브리핑
- `10:00` — 장중 1차 점검
- `13:00` — 장중 2차 점검
- `15:50` — 장마감 복기

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
├─ README.md
├─ .env.example
├─ requirements.txt
├─ main.py                    수동 실행
├─ scheduler.py               자동 스케줄 실행
├─ config/
│  └─ settings.py             환경변수 및 전역 설정
├─ data/
│  ├─ database.sqlite3         (자동 생성)
│  └─ logs/
├─ clients/
│  ├─ kis_client.py           한국투자증권 OpenAPI
│  ├─ openai_client.py        OpenAI API
│  ├─ telegram_client.py      텔레그램 봇
│  ├─ news_client.py          RSS 뉴스 수집
│  └─ market_data_client.py   글로벌 시장 데이터 (Yahoo Finance)
├─ agents/
│  ├─ ceo_agent.py
│  ├─ futures_market_team.py
│  ├─ us_market_team.py
│  ├─ korea_spot_market_team.py
│  ├─ global_market_team.py
│  ├─ news_analysis_team.py
│  ├─ sector_theme_team.py
│  ├─ money_flow_team.py
│  ├─ risk_management_team.py
│  ├─ review_feedback_team.py
│  └─ investment_committee.py
├─ graph/
│  ├─ state.py                LangGraph 상태 정의
│  └─ investment_graph.py     메인 분석 플로우
├─ services/
│  ├─ report_service.py       리포트 DB 저장
│  ├─ review_service.py       복기 기록 DB
│  └─ scoring_service.py      종목/섹터 점수화
├─ prompts/
│  └─ ceo_prompt.md           CEO 프롬프트 가이드
└─ tests/
   ├─ test_kis_client.py
   └─ test_agents.py
```

---

## 보안 주의사항

- `.env` 파일은 절대 커밋하지 마세요 (`.gitignore`에 포함됨)
- `.env.example`만 커밋합니다
- API Key는 코드에 직접 입력하지 마세요
- 로그에 민감정보가 출력되지 않도록 설계되어 있습니다

---

## 면책 조항

> 본 시스템은 투자 참고용 정보 수집 및 분석 도구입니다.  
> 본 리포트를 기반으로 한 투자 결정과 그에 따른 손익은 전적으로 투자자 본인의 책임입니다.  
> 자동 주문/매매 기능은 포함되어 있지 않습니다.
