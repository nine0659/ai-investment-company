# AI 투자 리서치 회사 구축 요구사항서

## 0. 프로젝트명

AI Investment Research Company Agent

## 1. 내가 만들고자 하는 것

나는 단순한 주식 자동 알림봇이나 뉴스 요약봇을 만들고 싶은 것이 아니다.

내가 만들고자 하는 것은 내 자산 증식을 목적으로, 여러 전문 부서가 시장 데이터를 수집하고 분석한 뒤, 최종적으로 CEO 에이전트가 투자 판단을 내려 나에게 텔레그램으로 보고하는 AI 투자 리서치 회사 시스템이다.

이 시스템은 실제 투자 의사결정을 돕기 위한 정보 수집, 시장 흐름 분석, 종목 후보 도출, 리스크 판단, 복기와 개선을 자동화하는 것을 목표로 한다.

최종 산출물은 매일 장전, 장중, 장마감에 텔레그램으로 수신하는 투자 브리핑이다.

---

## 2. 핵심 철학

1. 고정 관심종목 기반이 아니라 실제 시장에서 돈이 몰리는 종목을 찾아야 한다.
2. 단순 뉴스 요약이 아니라 실제 데이터와 뉴스를 함께 해석해야 한다.
3. 여러 전문 에이전트가 각자의 역할을 수행해야 한다.
4. 최종 판단은 CEO 에이전트가 내려야 한다.
5. 매일 추천과 결과를 기록하고 복기하여 시스템이 점점 개선되어야 한다.
6. 실시간 초단타 매매봇이 아니라, 투자 판단을 돕는 리서치 회사 형태여야 한다.
7. 최종 목표는 내 자산 증식을 위한 돈 되는 정보 수집과 종목 투자 의사결정 지원이다.

---

## 3. 전체 시스템 구조

```text
CEO Agent
├─ Futures Market Team
├─ US Market Team
├─ Korea Spot Market Team
├─ Global Market Team
├─ News Analysis Team
├─ Sector & Theme Team
├─ Money Flow Team
├─ Risk Management Team
├─ Review & Feedback Team
└─ Investment Committee
```

각 팀은 독립적으로 데이터를 수집하고 분석한다. CEO Agent는 각 팀의 의견을 취합하여 최종 투자 판단을 내린다.

---

## 4. 에이전트별 역할

### 4.1 CEO Agent

역할:
- 모든 부서 리포트를 취합한다.
- 의견 충돌을 조정한다.
- 오늘 시장 판단을 최종 결정한다.
- 핵심 섹터와 핵심 종목을 선정한다.
- 매수/관망/회피 판단을 제시한다.
- 리스크 경고를 포함한다.

출력:
- 오늘 시장 판단: 공격 / 중립 / 방어 / 관망
- 오늘 주도 섹터 TOP 3
- 오늘 핵심 종목 후보 TOP 3~5
- 진입 조건
- 손절 기준
- 피해야 할 조건
- 최종 CEO 코멘트

### 4.2 Futures Market Team

역할:
- 코스피200 야간선물
- 나스닥 선물
- 미국 선물
- 원/달러 환율
- 유가
- 미국 10년물 금리

분석:
- 오늘 한국시장 시초 방향
- 갭상승/갭하락 가능성
- 선물시장 기준 위험선호/위험회피 판단

### 4.3 US Market Team

역할:
- 다우
- S&P500
- 나스닥
- 필라델피아 반도체지수
- 미국 빅테크
- AI, 반도체, 전력, 방산 관련 미국 테마

분석:
- 한국 반도체 영향
- 성장주 영향
- 위험자산 선호 여부
- 한국시장에 영향을 줄 미국발 모멘텀

### 4.4 Korea Spot Market Team

역할:
- 한국투자증권 OpenAPI를 통해 실제 시장 데이터를 수집한다.
- 거래대금 상위
- 거래량 상위
- 등락률 상위
- 급등주
- 상한가 근접 종목
- 시장별 KOSPI/KOSDAQ 분위기

분석:
- 오늘 실제 돈이 몰리는 종목
- 급등하지만 위험한 종목
- 대장주 후보
- 코스피/코스닥 상대 강도

### 4.5 Global Market Team

역할:
- 일본, 중국, 홍콩, 유럽 시장
- 달러인덱스
- 유가
- 금리
- 원자재

분석:
- 글로벌 위험선호/위험회피
- 외국인 수급에 미칠 가능성
- 한국시장에 유리한 환경인지 불리한 환경인지 판단

### 4.6 News Analysis Team

역할:
- Google News RSS
- 네이버 뉴스
- 경제지 헤드라인
- 정책 뉴스
- 실적 뉴스
- 수주 뉴스
- 지정학 뉴스
- 금리, 환율, 유가 뉴스

분석:
- 오늘 시장 재료
- 지속 가능한 뉴스인지 단기성 뉴스인지 판단
- 섹터별 뉴스 모멘텀
- 특정 종목의 급등 이유

### 4.7 Sector & Theme Team

역할:
- 반도체
- 방산
- 원전/전력
- 2차전지
- 로봇/AI
- 조선
- 바이오
- 금융
- 자동차
- 기타 당일 이슈 섹터

분석:
- 오늘 주도 섹터
- 2등 섹터
- 순환매 후보
- 과열 섹터
- 피해야 할 섹터

### 4.8 Money Flow Team

역할:
- 거래대금 집중
- 거래량 급증
- 외국인/기관 수급 가능 데이터
- 시장에서 실제 돈이 몰리는 종목 탐지

분석:
- 진짜 수급이 붙은 종목
- 뉴스만 있고 거래대금이 없는 종목 제거
- 대장주 후보 점수화

### 4.9 Risk Management Team

역할:
- 갭상승 과열
- 전일 급등 후 피로도
- 거래대금 없는 급등
- 지수 약세
- 외국인 선물 매도
- 추격매수 위험

분석:
- 오늘 들어가면 안 되는 조건
- 손절 기준
- 비중 조절
- 관망 조건

### 4.10 Review & Feedback Team

역할:
- 아침 추천 종목과 섹터를 기록한다.
- 장마감 결과를 기록한다.
- 예측이 맞았는지 틀렸는지 복기한다.
- 다음 리포트에 반영할 개선점을 생성한다.

분석:
- 적중 섹터
- 실패 섹터
- 잘 맞은 조건
- 틀린 조건
- 다음날 가중치 조정

### 4.11 Investment Committee

역할:
- 각 팀 의견을 점수화한다.
- 섹터와 종목 후보를 비교한다.
- CEO가 최종 판단하기 전 중간 결론을 만든다.

예시 점수:
```text
시장 방향성: +1
수급 강도: +2
뉴스 지속성: +1
리스크: -1
종합 점수: +3
판단: 제한적 공격 가능
```

---

## 5. 데이터 소스

### 5.1 필수 데이터

1. 한국투자증권 OpenAPI
   - 거래량 순위
   - 거래대금 상위
   - 등락률 순위
   - 종목 현재가
   - 시장 순위 데이터
   - 가능하면 외국인/기관 수급 데이터

2. 뉴스
   - Google News RSS
   - 네이버 뉴스 RSS 또는 검색
   - 경제/정책/산업 뉴스

3. 글로벌 시장
   - 미국 지수
   - 나스닥
   - S&P500
   - 다우
   - 필라델피아 반도체
   - 환율
   - 유가
   - 금리

4. 한국 선물시장
   - 코스피200 야간선물 또는 대체 지표

---

## 6. 현재까지 진행된 사항

현재 Google Apps Script 기반으로 아래 기능이 일부 구현되어 있다.

- OpenAI API 호출
- 텔레그램 메시지 발송
- Google News RSS 수집
- 한국투자증권 OpenAPI 토큰 발급
- 한국투자증권 거래량 순위 API 일부 연동
- 장전/장중/장마감 트리거 설계
- GPT 기반 리포트 생성

하지만 Apps Script는 복잡한 멀티 에이전트 구조에 한계가 있으므로, 앞으로는 Python 기반 프로젝트로 전환하고 싶다.

---

## 7. 원하는 기술 스택

권장 스택:
- Python 3.11+
- LangGraph
- LangChain 또는 OpenAI SDK
- OpenAI API
- 한국투자증권 OpenAPI
- Telegram Bot API
- SQLite 또는 DuckDB
- pandas
- requests
- python-dotenv
- APScheduler 또는 cron
- FastAPI는 선택 사항

목표:
- 로컬 PC 또는 저비용 클라우드에서 실행 가능한 구조
- 향후 확장을 고려한 모듈화된 폴더 구조

---

## 8. 원하는 폴더 구조

```text
ai-investment-company/
├─ README.md
├─ .env.example
├─ requirements.txt
├─ main.py
├─ scheduler.py
├─ config/
│  └─ settings.py
├─ data/
│  ├─ database.sqlite3
│  └─ logs/
├─ clients/
│  ├─ kis_client.py
│  ├─ openai_client.py
│  ├─ telegram_client.py
│  ├─ news_client.py
│  └─ market_data_client.py
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
│  ├─ state.py
│  └─ investment_graph.py
├─ services/
│  ├─ ranking_service.py
│  ├─ sector_service.py
│  ├─ scoring_service.py
│  ├─ review_service.py
│  └─ report_service.py
├─ prompts/
│  ├─ ceo_prompt.md
│  ├─ team_prompts.md
│  └─ report_template.md
└─ tests/
   ├─ test_kis_client.py
   ├─ test_agents.py
   └─ test_graph.py
```

---

## 9. 환경변수

`.env.example`에는 아래 항목이 필요하다.

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

KIS_APP_KEY=
KIS_APP_SECRET=
KIS_BASE_URL=https://openapi.koreainvestment.com:9443

TIMEZONE=Asia/Seoul
DATABASE_URL=sqlite:///data/database.sqlite3
```

---

## 10. 실행 스케줄

```text
08:20 장전 CEO 브리핑
10:00 장중 1차 시장 점검
13:00 장중 2차 시장 점검
15:50 장마감 복기
```

각 리포트는 텔레그램으로 발송한다.

---

## 11. 장전 리포트 형식

```text
[CEO 장전 투자 브리핑]

1. 오늘 시장 판단
- 공격 / 중립 / 방어 / 관망
- 핵심 이유

2. 부서별 요약
- 선물시장팀
- 미국시장팀
- 한국현물시장팀
- 세계주요시장팀
- 기사분석팀
- 섹터/테마팀
- 수급팀
- 리스크팀

3. 오늘 주도 가능 섹터 TOP 3

4. 오늘 핵심 종목 후보 TOP 3~5
- 종목명
- 근거
- 진입 조건
- 손절 조건
- 주의사항

5. 오늘 피해야 할 종목/조건

6. CEO 최종 판단
```

---

## 12. 장중 리포트 형식

```text
[CEO 장중 시장 점검]

1. 현재 시장 분위기
2. 장중 강한 섹터
3. 거래대금 집중 종목
4. 장중 관심 종목 TOP 3
5. 지금 진입 가능 / 대기 구분
6. 추격매수 위험
7. CEO 코멘트
```

---

## 13. 장마감 복기 형식

```text
[CEO 장마감 복기]

1. 오늘 강했던 섹터
2. 오늘 약했던 섹터
3. 아침 판단 적중 여부
4. 추천 종목 성과
5. 실패 원인
6. 내일 반영할 개선점
7. CEO 총평
```

---

## 14. 핵심 기능 요구사항

### 14.1 실제 시장 기반 종목 탐지

고정 WATCHLIST를 사용하지 말고, 한국투자 OpenAPI에서 실제 시장 랭킹 데이터를 가져와야 한다.

필요:
- 거래량 순위
- 거래대금 순위
- 등락률 순위
- 급등주
- 시장별 KOSPI/KOSDAQ 구분

### 14.2 섹터 강도 분석

수집된 종목들을 섹터별로 분류하고 다음 점수를 계산한다.

- 평균 등락률
- 거래대금 합계
- 거래량 증가율
- 종목 수
- 뉴스 모멘텀

### 14.3 종목 점수화

종목별 점수는 다음 요소를 반영한다.

- 등락률
- 거래대금
- 거래량 증가율
- 뉴스 연관성
- 섹터 강도
- 리스크 패널티

### 14.4 리스크 필터

아래 종목은 경고하거나 제외한다.

- 거래대금 없는 급등
- 전일 급등 후 거래대금 감소
- 뉴스만 있고 수급 없는 종목
- 과도한 갭상승
- 하락 추세 중 일시 반등
- 시초 급등 후 긴 윗꼬리

### 14.5 복기와 개선

매일 추천 결과를 DB에 기록한다.

기록:
- 날짜
- 추천 시간
- 추천 종목
- 추천 섹터
- 추천 이유
- 당시 가격
- 장마감 가격
- 수익률
- 성공/실패
- 실패 원인
- 다음날 반영할 개선점

---

## 15. LangGraph 상태 설계

`graph/state.py`에는 아래 상태가 필요하다.

```python
from typing import TypedDict, List, Dict, Any

class InvestmentState(TypedDict):
    run_type: str
    timestamp: str
    raw_market_data: Dict[str, Any]
    futures_report: str
    us_market_report: str
    korea_spot_report: str
    global_market_report: str
    news_report: str
    sector_report: str
    money_flow_report: str
    risk_report: str
    committee_report: str
    ceo_report: str
    candidates: List[Dict[str, Any]]
    sector_scores: List[Dict[str, Any]]
    risks: List[str]
    errors: List[str]
```

---

## 16. LangGraph 흐름

```text
START
→ collect_raw_data
→ futures_market_team
→ us_market_team
→ korea_spot_market_team
→ global_market_team
→ news_analysis_team
→ sector_theme_team
→ money_flow_team
→ risk_management_team
→ investment_committee
→ ceo_agent
→ save_report
→ send_telegram
→ END
```

초기 버전은 순차 실행으로 만들고, 이후 병렬 실행을 고려한다.

---

## 17. Claude Code에게 요청할 개발 작업

Claude Code는 아래를 수행해 달라.

1. 위 요구사항에 맞춰 Python 프로젝트를 생성한다.
2. 폴더 구조를 만든다.
3. `.env.example`을 만든다.
4. 한국투자증권 OpenAPI 클라이언트를 만든다.
5. OpenAI 클라이언트를 만든다.
6. Telegram 클라이언트를 만든다.
7. 각 부서 에이전트 파일을 만든다.
8. LangGraph 기반 플로우를 만든다.
9. SQLite 저장 구조를 만든다.
10. 장전/장중/장마감 실행 함수를 만든다.
11. `main.py`에서 수동 실행 가능하게 한다.
12. `scheduler.py`에서 자동 스케줄 실행 가능하게 한다.
13. README에 설치와 실행 방법을 작성한다.
14. 가능한 경우 간단한 테스트 코드를 작성한다.

---

## 18. 우선 구현 범위

1차 MVP에서는 실제 자동매매는 하지 않는다.

포함:
- 데이터 수집
- 분석
- 리포트 생성
- 텔레그램 발송
- 추천 기록
- 장마감 복기

제외:
- 자동 주문
- 실제 매수/매도
- 레버리지/파생상품 주문
- 초단타 실시간 매매

---

## 19. 보안 요구사항

- API Key는 코드에 직접 넣지 않는다.
- `.env` 파일을 사용한다.
- `.env.example`만 커밋한다.
- 로그에 APP_SECRET, OPENAI_API_KEY, TELEGRAM_TOKEN을 출력하지 않는다.
- 에러 발생 시 민감정보를 마스킹한다.

---

## 20. 최종 목표

이 프로젝트의 최종 목표는 내 자산 증식을 위한 AI 투자 리서치 회사다.

이 시스템은 매일 시장을 조사하고, 돈이 몰리는 섹터와 종목을 찾아내며, 리스크를 점검하고, CEO 에이전트가 최종 투자 판단을 내려 나에게 보고한다.

나는 이 보고서를 바탕으로 최종 투자 결정을 한다.
