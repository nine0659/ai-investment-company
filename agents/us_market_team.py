import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 미국 주식시장 및 반도체 섹터 분석 전문가입니다.
미국 증시 데이터를 바탕으로 한국 시장(특히 반도체/IT 섹터)에 미치는 영향을 분석하세요.

분석 항목:
1. 미국 3대 지수(S&P500·NASDAQ·DOW) 전일 성과 및 방향성
2. 반도체 지수(SOX) 동향
3. NVDA·TSM 등 핵심 반도체주 동향
4. 한국 반도체 섹터(삼성전자·SK하이닉스) 영향도
5. 오늘 핵심 시그널

출력: 핵심 요약 3-5줄 / 한국 반도체 섹터 영향(긍정·부정·중립) / 주목 이슈"""

_LABELS = {
    "sp500": "S&P500", "nasdaq": "NASDAQ", "dow": "DOW",
    "sox": "SOX반도체지수", "nvda": "NVIDIA", "tsmc": "TSMC",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        lines = [
            f"{_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _LABELS if (d := data.get(k))
        ]
        result = chat(_SYSTEM, "미국 시장 데이터:\n" + ("\n".join(lines) or "데이터 없음"))
        state["us_market_report"] = result
        logger.info("[미국팀] 완료")
    except Exception as e:
        logger.error("[미국팀] 실패: %s", e)
        state["us_market_report"] = "데이터 수집 실패"
        state["errors"].append(f"us_team: {e}")
    return state
