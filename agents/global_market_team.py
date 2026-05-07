import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 및 아시아 시장 분석 전문가입니다.
아시아 주요 증시와 외환시장을 분석하여 한국 시장 영향을 판단하세요.

분석 항목:
1. 일본(닛케이)·중국(상하이·항셍) 등 아시아 증시 동향
2. 달러·원(USD/KRW) 수준과 외국인 수급 예상
3. 달러인덱스(DXY)와 신흥국 시장 영향
4. KOSPI/KOSDAQ 당일 시작 예상 방향
5. 글로벌 매크로 특이사항

출력: 아시아 시장 요약 / 원달러 영향도 / 외국인 순매수·순매도 예상 / 시가 방향 예측"""

_LABELS = {
    "kospi": "KOSPI", "kosdaq": "KOSDAQ", "nikkei": "닛케이",
    "hang_seng": "항셍", "shanghai": "상하이", "usd_krw": "달러/원", "dxy": "달러인덱스",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        lines = [
            f"{_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _LABELS if (d := data.get(k))
        ]
        result = chat(_SYSTEM, "글로벌 시장 데이터:\n" + ("\n".join(lines) or "데이터 없음"))
        state["global_market_report"] = result
        logger.info("[글로벌팀] 완료")
    except Exception as e:
        logger.error("[글로벌팀] 실패: %s", e)
        state["global_market_report"] = "데이터 수집 실패"
        state["errors"].append(f"global_team: {e}")
    return state
