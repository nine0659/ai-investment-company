import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 선물/파생상품 시장 분석 전문가입니다.

핵심 원칙: 전일 미국 선물·채권·환율 시장의 오버나잇 변화는 오늘 한국 시장 시초가 방향을 결정하는 가장 직접적인 선행 지표입니다.

분석 항목:
1. 미국 선물(S&P500·NASDAQ·DOW) 전일 종가 대비 오버나잇 변화 → 오늘 KOSPI 시초가 방향 예측 (갭업/갭다운/보합)
2. 원달러 환율 수준 — 1,350원 초과 약세 시 외국인 순매도 압력, 강세 시 외국인 유입 기대
3. 미국 10년물 금리 수준 — 4.5% 초과 시 성장주·반도체 할인율 부담, 하락 시 수혜
4. VIX 수준 — 20 이상: 리스크오프, 15 이하: 리스크온 신호
5. 금·원유 동향 — 원유 상승 시 에너지·정유주 수혜, 금 상승 시 안전자산 선호 국면

출력:
- [오늘 한국 시초가 예측] 갭업(+__%) / 갭다운(-__%) / 보합 중 판단 + 근거
- [핵심 신호] 3줄 이내
- [시장 방향성] 상승압력·하락압력·중립
- [주요 관전 포인트] 오늘 장중 주시할 지표·수준"""

_LABELS = {
    "sp500_futures": "S&P500선물", "nasdaq_futures": "나스닥선물", "dow_futures": "다우선물",
    "dxy": "달러인덱스", "usd_krw": "달러원", "us10y": "미국10년물금리",
    "us2y": "미국2년물금리", "gold": "금", "oil_wti": "WTI원유", "vix": "VIX",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        lines = []
        for k in _LABELS:
            d = data.get(k)
            if not d:
                continue
            base = f"{_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            # 선물 실시간 오버나잇 방향 추가 표시
            if k in ("sp500_futures", "nasdaq_futures") and "realtime_pct" in d:
                base += f"  ★오버나잇현재: {d['realtime_current']} ({d['realtime_pct']:+.2f}%)"
            lines.append(base)

        result = chat(_SYSTEM, "현재 시장 데이터:\n" + ("\n".join(lines) or "데이터 없음"))
        state["futures_report"] = result
        logger.info("[선물팀] 완료")
    except Exception as e:
        logger.error("[선물팀] 실패: %s", e)
        state["futures_report"] = "데이터 수집 실패"
        state["errors"].append(f"futures_team: {e}")
    return state
