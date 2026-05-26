"""
agents/event_risk_team.py
경제 이벤트 리스크 분석 — 이번 주·다음 주 주요 이벤트 경고 및 포지션 조정 권고

커버리지:
  - 미국: FOMC(연 8회)·CPI/PPI(월별)·NFP(매월 첫 금요일)·PCE(월말)·실적시즌
  - 한국: BOK 기준금리 결정(연 8회)·수출 데이터(매월 1일)·옵션만기일(매월 둘째 목요일)
  - 글로벌: ECB·BOJ 통화정책 회의
"""
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

_WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

_SYSTEM = """당신은 글로벌 경제 이벤트 리스크 전문가입니다.
오늘 날짜와 제공된 매크로 지표를 기반으로, 이번 주·다음 주에 예정된 주요 경제 이벤트를 분석하고
한국 주식시장에 미칠 변동성 리스크를 경고하세요.

[주요 이벤트 유형 및 발표 패턴]
■ 미국 (가장 큰 영향)
- FOMC 회의: 연 8회, 6주 간격. 결정일 전 2~3일이 가장 민감. 성명+점도표+기자회견.
- CPI(소비자물가): 매월 10~15일경 발표. 예상치 대비 ±0.1%p 초과 시 KOSPI 당일 ±0.5~1% 급변.
- NFP(비농업고용): 매월 첫 번째 금요일. 고용 > 예상 → 금리인상 우려 → 성장주 압박.
- PCE(개인소비지출): 매월 마지막 영업일. Fed 선호 물가지표.
- PPI(생산자물가): CPI 발표 1~2일 전.
- FOMC 의사록: 회의 3주 후 수요일 저녁.
- GDP 속보치: 분기별 (1/4/7/10월 말).

■ 한국
- 옵션만기일: 매월 둘째 목요일. 수급 왜곡·변동성 급증. 전날과 당일 포지션 위험.
- 선물만기일(쿼드러플위칭): 3/6/9/12월 둘째 목요일. 옵션+선물+ETF 동시만기. 특히 위험.
- BOK 기준금리 결정: 연 8회. 통상 목요일.
- 수출입 데이터: 매월 1일 (전월 수출). 반도체·자동차 섹터 직접 영향.
- 기업 실적 발표: 1/4/7/10월 중순~말.

■ 글로벌
- ECB 통화정책 회의: 연 8회. 유로 강세/약세 → DXY → 외국인 수급.
- BOJ: 연 8회. 엔화 방향 → 닛케이 연동 → 아시아 자금흐름.

[이벤트 리스크 등급]
🔴 HIGH: FOMC 결정일, 미국 CPI, 쿼드러플위칭 → 당일 포지션 50% 이상 축소 권고
🟡 MEDIUM: NFP, PCE, BOK 금리, 옵션만기일, ECB → 신규 진입 자제, 기존 포지션 유지
🟢 LOW: FOMC 의사록, PPI, 수출 데이터 → 참고용, 변동성 소폭 확대 가능

[출력 형식 — 반드시 이 구조로]
📅 오늘: [날짜] ([요일])

🗓 이번 주 이벤트 (잔여 일정)
[이벤트명] | [예상 날짜] | [리스크 등급] | [한국 영향 한 줄]
(해당 없으면 "이번 주 주요 이벤트 없음")

🗓 다음 주 이벤트
[이벤트명] | [예상 날짜] | [리스크 등급] | [한국 영향 한 줄]

⚠️ 이번 주 이벤트 리스크 요약
- 전반적 이벤트 리스크: 높음 / 중간 / 낮음
- 포지션 권고: [구체적 행동 지침]

💡 섹터별 이벤트 민감도
(예: "CPI 고점 우려 → 성장주·반도체 사전 익절 권고 / 은행·방산 상대적 안정")

[중요 규칙]
- 날짜가 불확실한 이벤트는 "약 X월 X주경 예상"으로 표기
- 확인되지 않은 구체적 날짜를 단정하지 말 것
- 현재 매크로 지표(VIX, 금리, 달러)와 연계해 이벤트 영향을 구체화
- 이벤트가 없는 주는 "이벤트 리스크 낮음 — 매크로 기조 흐름에 집중" 명시"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        now  = datetime.now(_KST)
        today = now.strftime(f"%Y년 %m월 %d일 ({_WEEKDAY_KO[now.weekday()]})")
        week_num = now.isocalendar()[1]
        month = now.month
        day   = now.day

        # 옵션만기일 계산: 이번 달 둘째 목요일
        first_day = date(now.year, month, 1)
        # 첫 번째 목요일: 0=월요일, 3=목요일
        days_to_first_thu = (3 - first_day.weekday()) % 7
        first_thu = first_day.day + days_to_first_thu
        second_thu = first_thu + 7
        options_expiry = date(now.year, month, second_thu)
        options_expiry_str = options_expiry.strftime("%m월 %d일")

        # 이번 달 첫날 (수출 데이터)
        next_month = month + 1 if month < 12 else 1
        next_year  = now.year if month < 12 else now.year + 1
        export_date = f"{next_year}년 {next_month}월 1일"

        # 쿼드러플위칭 여부 (3/6/9/12월)
        is_quad_month = month in (3, 6, 9, 12)
        expiry_type = "쿼드러플위칭(선물+옵션 동시만기) 🔴HIGH" if is_quad_month else "옵션만기일 🟡MEDIUM"

        # 매크로 지표 (이벤트 영향 크기 보정용)
        raw = state.get("raw_market_data", {})
        vix    = raw.get("vix",   {}).get("close", "N/A")
        us10y  = raw.get("us10y", {}).get("close", "N/A")
        dxy    = raw.get("dxy",   {}).get("close", "N/A")
        macro_brief = f"VIX {vix} / 미국10년물 {us10y}% / DXY {dxy}"

        context = f"""오늘 날짜: {today}
현재 매크로 지표: {macro_brief}
이번 달 옵션만기일: {options_expiry_str} — {expiry_type}
다음 달 수출 데이터 발표: {export_date}
이번 달이 쿼드러플위칭 월(3/6/9/12월)인가: {"예" if is_quad_month else "아니오"}
현재 주차: {week_num}주차 (월 중 {(day - 1) // 7 + 1}번째 주)

위 정보를 바탕으로 이번 주 잔여 일정과 다음 주의 주요 경제 이벤트를 분석하세요.
날짜가 불확실한 경우 "약 X월 X주경"으로 표기하고, 확정된 것만 날짜 명시."""

        result = chat(_SYSTEM, context, max_tokens=800)
        state["event_risk_report"] = result

        # 이벤트 리스크 레벨 추출 (리스크팀·투자위원회 참고용)
        if "🔴" in result or "HIGH" in result:
            state["event_risk_level"] = "높음"
        elif "🟡" in result or "MEDIUM" in result:
            state["event_risk_level"] = "중간"
        else:
            state["event_risk_level"] = "낮음"

        logger.info("[이벤트리스크팀] 완료 — 리스크 레벨: %s", state["event_risk_level"])
    except Exception as e:
        logger.error("[이벤트리스크팀] 실패: %s", e)
        state["event_risk_report"] = "이벤트 리스크 분석 실패"
        state["event_risk_level"] = "중간"
        state["errors"].append(f"event_risk_team: {e}")
    return state
