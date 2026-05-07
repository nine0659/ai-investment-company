import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 선물/파생상품 시장 분석 전문가입니다.
주어진 데이터를 분석하여 오늘 한국 주식시장에 미칠 영향을 판단하세요.

분석 항목:
1. 미국 주요 선물 (ES, NQ, YM) 방향성 및 의미
2. 달러인덱스(DXY)와 원달러환율 동향
3. 금리 동향 (미국 2/10년물) 및 채권시장 신호
4. 금·원유 등 원자재 동향
5. VIX 공포지수 수준 및 시장 리스크

출력: 핵심 요약 3-5줄 / 시장 방향성(상승압력·하락압력·중립) / 주요 관전 포인트"""

_LABELS = {
    "sp500_futures": "S&P500선물", "nasdaq_futures": "나스닥선물", "dow_futures": "다우선물",
    "dxy": "달러인덱스", "usd_krw": "달러원", "us10y": "미국10년물금리",
    "us2y": "미국2년물금리", "gold": "금", "oil_wti": "WTI원유", "vix": "VIX",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        lines = [
            f"{_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _LABELS if (d := data.get(k))
        ]
        result = chat(_SYSTEM, "현재 시장 데이터:\n" + ("\n".join(lines) or "데이터 없음"))
        state["futures_report"] = result
        logger.info("[선물팀] 완료")
    except Exception as e:
        logger.error("[선물팀] 실패: %s", e)
        state["futures_report"] = "데이터 수집 실패"
        state["errors"].append(f"futures_team: {e}")
    return state
