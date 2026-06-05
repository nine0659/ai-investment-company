import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 선물/파생상품 시장 분석 전문가입니다.

분석 우선순위:
1순위: KOSPI200 야간선물(미니선물) 전일대비 변화 → 오늘 KOSPI 시초가의 가장 직접적 선행 지표
2순위: 미국 선물(S&P500·NASDAQ·DOW) 오버나잇 변화 → KOSPI 방향 확인·보완
3순위: 원달러 환율·미국 금리·VIX → 외국인 수급 방향 필터

분석 항목:
1. [한국 야간선물] KOSPI200 미니선물 전일대비 변화율 → 오늘 KOSPI 시초가 갭 방향 직접 판단
   - 데이터 없을 경우: 미국 선물 기반으로 KOSPI200 연동 추정값 제시 (추정임을 명시)
2. [미국 선물] S&P500·NASDAQ·DOW 오버나잇 방향 → KOSPI 방향 1·2차 확인
3. [환율·금리] 원달러 1,350원 기준 / 미국10년물 4.5% 기준 — 외국인 수급 방향 결정
4. [VIX] 20 이상: 리스크오프, 15 이하: 리스크온
5. [원자재] 원유·금 방향 — 에너지·안전자산 섹터 수혜 여부

출력:
- [전일 한국시장 야간선물 분석] KOSPI200 미니선물 수치 + 방향 한 줄
- [오늘 KOSPI 시초가 예측] 갭업(+__%) / 갭다운(-__%) / 보합 + 근거 (야간선물 우선, 미국선물 보완)
- [핵심 신호] 3줄 이내
- [시장 방향성] 상승압력·하락압력·중립
- [주요 관전 포인트] 오늘 장중 주시할 지표·수준"""

_LABELS = {
    "kospi200_futures": "KOSPI200미니선물(야간)",
    "sp500_futures": "S&P500선물", "nasdaq_futures": "나스닥선물", "dow_futures": "다우선물",
    "dxy": "달러인덱스", "usd_krw": "달러원", "us10y": "미국10년물금리",
    "us2y": "미국2년물금리", "gold": "금", "oil_wti": "WTI원유", "vix": "VIX",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        lines = []

        # KOSPI200 기준값을 최상단에 표시
        k200 = data.get("kospi200_futures")
        if k200:
            direction = "▲" if k200["change_pct"] >= 0 else "▼"
            if k200.get("is_index"):
                lines.append(
                    f"★ KOSPI200지수 전일종가(야간선물 기준): {k200['close']} "
                    f"{direction}{k200['change_pct']:+.2f}%  "
                    f"(고:{k200['high']} / 저:{k200['low']})  "
                    f"※야간선물 방향은 아래 미국선물로 추정"
                )
            else:
                lines.append(
                    f"★ KOSPI200미니선물(야간): {k200['close']} "
                    f"{direction}{k200['change_pct']:+.2f}%  "
                    f"(고:{k200['high']} / 저:{k200['low']})"
                )
        else:
            lines.append("★ KOSPI200 기준값: 수집 실패 — 미국 선물 연동으로 방향 추정")

        for k in _LABELS:
            if k == "kospi200_futures":
                continue  # 이미 위에서 출력
            d = data.get(k)
            if not d:
                continue
            base = f"{_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            if k in ("sp500_futures", "nasdaq_futures") and "realtime_pct" in d:
                base += f"  ★오버나잇현재: {d['realtime_current']} ({d['realtime_pct']:+.2f}%)"
            lines.append(base)

        # 데이터 신선도 레이블 — 첫 줄에 기준 날짜 명시
        freshness = state.get("data_freshness", {})
        freshness_label = freshness.get("label", "")
        freshness_warning = freshness.get("warning", "")
        header_lines = []
        if freshness_label:
            header_lines.append(f"📅 데이터 기준: {freshness_label}")
        if freshness_warning:
            header_lines.append(freshness_warning)
        header = ("\n".join(header_lines) + "\n\n") if header_lines else ""

        result = chat(_SYSTEM, header + "현재 시장 데이터:\n" + "\n".join(lines), max_tokens=800)
        state["futures_report"] = result
        logger.info("[선물팀] 완료")
    except Exception as e:
        logger.error("[선물팀] 실패: %s", e)
        state["futures_report"] = "데이터 수집 실패"
        state["errors"].append(f"futures_team: {e}")
    return state
