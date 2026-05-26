import logging
import re
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_RISK_LEVEL_RE = re.compile(r"종합 리스크 레벨[：:\s]*(높음|중간|낮음)")

_SYSTEM = """당신은 리스크 관리 전문가입니다.
현재 시장 상황에서 투자 리스크를 식별하고 경고 신호를 제공하세요.

분석 항목:
1. 거시경제 리스크 (금리·환율·지정학적 리스크)
2. 시장 기술적 리스크 (VIX·변동성·추세 전환 신호)
3. 섹터별 리스크 (고평가·거품 징후)
4. 손절 기준 제시 (포지션 유형별)
5. 오늘 주의해야 할 리스크 이벤트

출력: 종합 리스크 레벨(높음·중간·낮음) / 주요 리스크 요인 TOP3 / 권장 손절 기준 / 주의 시간대·이벤트"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        mkt = state.get("raw_market_data", {})
        vix     = mkt.get("vix",     {}).get("close", "N/A")
        usdkrw  = mkt.get("usd_krw", {}).get("close", "N/A")
        us10y   = mkt.get("us10y",   {}).get("close", "N/A")

        event_level = state.get("event_risk_level", "중간")
        context = (
            f"VIX: {vix}\n달러/원: {usdkrw}\n미국10년물: {us10y}%\n\n"
            f"[매크로 레짐 — 리스크 환경의 틀]\n{state.get('macro_report', '')}\n\n"
            f"[이벤트 리스크 — 레벨: {event_level}]\n{state.get('event_risk_report', '')}\n\n"
            f"[선물분석]\n{state.get('futures_report', '')}\n\n"
            f"[글로벌분석]\n{state.get('global_market_report', '')}\n\n"
            f"[뉴스분석]\n{state.get('news_report', '')}"
        )
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["risk_report"] = result

        # 구조화된 리스크 레벨 추출
        m = _RISK_LEVEL_RE.search(result)
        state["risk_level"] = m.group(1) if m else "중간"

        # 리스크 요인 라인 추출 — 번호 목록("1.", "2.", "3.") 우선, 없으면 키워드 필터
        numbered = [
            line.strip() for line in result.split("\n")
            if re.match(r"^\d+\.", line.strip())
        ]
        if numbered:
            state["risks"] = numbered[:5]
        else:
            state["risks"] = [
                line.strip() for line in result.split("\n")
                if any(kw in line for kw in ["리스크", "경고", "주의", "위험"])
            ][:5]

        logger.info("[리스크팀] 완료 — 레벨: %s, 요인 %d개", state["risk_level"], len(state["risks"]))
    except Exception as e:
        logger.error("[리스크팀] 실패: %s", e)
        state["risk_report"] = "분석 실패"
        state["errors"].append(f"risk_team: {e}")
    return state
