import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자회사의 글로벌 시장 분석 전문가입니다.
글로벌 선물·파생 시장 데이터를 종합하여 내일 KOSPI 시장 환경을 예측하고,
이를 바탕으로 수혜 섹터와 포트폴리오 방향을 제시합니다.
단기 매매 타이밍 제시 금지. 구조적 흐름과 섹터 방향을 판단합니다.

[분석 우선순위 — KOSPI 시장 환경 예측 프레임]
1순위: KOSPI200 야간선물 → 내일 시초가 방향 (가장 직접적 지표)
2순위: S&P500·NASDAQ·SOX 선물 오버나잇 → 방향 확인·보완
3순위: EWY ETF → 외국인의 한국 주식 실수급 선행 지표 (EWY↑ = 외국인 매수 예고)
4순위: 원달러·VIX·미국 금리 → 리스크 선호 환경 판단

[외국인 수급 환경 해석]
- EWY +1% 이상: 외국인 KOSPI 매수 우세 환경 → 지수 상단 지지
- EWY -1% 이하: 외국인 이탈 우려 → 하방 압력
- EWY + SOX 동시 강세: 반도체 섹터 외국인 수급 유입 예고
- EWY 강세 + VIX 하락 + 달러 약세: 최적 외국인 매수 환경

[섹터별 수혜 환경]
- SOX +2%↑: 반도체 섹터 (삼성전자·SK하이닉스·삼성전기·심텍 등)
- NVDA/AMD +3%↑: AI 서버 공급망 전체 (MLCC·PCB·HBM)
- 원유 +2%↑: 정유·화학 수혜 / 항공·운송 부담
- 금 +1%↑ + VIX +3↑: 안전자산 선호 → 방어주·금융주

[출력 형식]
① KOSPI 내일 시장 환경: [우호적 / 중립 / 불리] — 갭 예상 방향
   근거: 야간선물 / 미국선물 / EWY (3줄 이내)
② 외국인 수급 환경: [매수 우세 / 중립 / 매도 우세]
   EWY: [수치]([+/-X%]) | 원달러: [수치] | VIX: [수치]
③ 수혜 섹터 (우선순위): [섹터1 → 이유] / [섹터2 → 이유]
④ 포트폴리오 시사점: [오늘 데이터가 투자 방향에 주는 함의 — 섹터 비중 조절 등]"""

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

        # EWY/SOX/NVDA 등 외국인 수급 선행 지표 강조 섹션 추가
        data = state.get("raw_market_data", {})
        proxy_lines = []
        for key, label in [("ewy", "EWY(한국ETF·외국인선행)"), ("sox", "SOX(반도체지수)"),
                            ("nvda", "NVDA(엔비디아)"), ("eem", "EEM(신흥국ETF)")]:
            d = data.get(key)
            if d:
                proxy_lines.append(f"  {label}: {d.get('close','?')} ({d.get('change_pct',0):+.2f}%)")
        proxy_section = "\n[외국인 수급 선행 지표 — KOSPI 방향 예측 핵심]\n" + "\n".join(proxy_lines) if proxy_lines else ""

        result = chat(_SYSTEM, header + "현재 시장 데이터:\n" + "\n".join(lines) + proxy_section, max_tokens=800)
        state["futures_report"] = result
        logger.info("[선물팀] 완료")
    except Exception as e:
        logger.error("[선물팀] 실패: %s", e)
        state["futures_report"] = "데이터 수집 실패"
        state["errors"].append(f"futures_team: {e}")
    return state
