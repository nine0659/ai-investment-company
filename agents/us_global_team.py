"""
agents/us_global_team.py
미국·글로벌 통합 분석 에이전트

us_market_team + us_impact_agent + global_market_team 통합
- LLM 호출: 3회 → 1회 (구분자로 세 섹션 파싱)
- US_SECTOR_TO_KR은 us_impact_agent.py에 그대로 유지 (korea_flow_team 등이 import)
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.us_stock_client import format_us_impact_for_prompt

logger = logging.getLogger(__name__)

_INDEX_LABELS = {
    "sp500": "S&P500", "nasdaq": "NASDAQ", "dow": "DOW",
    "sox": "SOX반도체지수", "nvda": "NVIDIA", "tsmc": "TSMC",
}
_GLOBAL_LABELS = {
    "kospi": "KOSPI", "kosdaq": "KOSDAQ", "nikkei": "닛케이",
    "hang_seng": "항셍", "shanghai": "상하이", "usd_krw": "달러/원", "dxy": "달러인덱스",
}

_SYSTEM = """당신은 미국·글로벌 증시 → 한국 시장 연동 통합 분석 전문가입니다.
아래 데이터를 바탕으로 세 섹션을 반드시 아래 구분자 형식으로 출력하세요.

=== 미국 증시 분석 ===
1. 전일 미국 3대 지수(S&P500·NASDAQ·DOW) → 오늘 KOSPI·KOSDAQ 방향성 (확률 수치 필수)
   예: "NASDAQ +1.8%, SOX +2.3% → 오늘 반도체 갭업 확률 75%, 갭다운 확률 25%"
2. SOX 반도체 지수 → SK하이닉스·삼성전자·한미반도체 방향
3. 전일 미국 급등 TOP5 → 한국 공급망·경쟁사 연관 종목 (수혜 강도: 강/중/약)
4. 미국 섹터 강세 순위 → 오늘 한국 매수 우선순위
5. [오늘 피해야 할 섹터] 미국 약세 섹터 연동

=== 미국발 한국 연동 분석 ===
[미국발 한국 섹터 구조적 연동 분석]
1. [미국 강세 섹터 → 한국 공급망 연동] 연동 강도(높음/보통) 포함
   예: "SMH(반도체ETF) +3.2% → HBM 공급망: SK하이닉스·한미반도체 (연동 높음)"
2. [미국 약세 섹터 → 한국 영향] 하방 압력 섹터 (리스크 관리용)
3. [52주 신고가 종목 공급망] 한국 부품·소재 공급사 (있는 경우)
단기 매수 타이밍·"오늘 살 종목" 제시 금지. "지금 매수" "즉시 진입" 표현 절대 사용 금지.

=== 글로벌·아시아 분석 ===
1. 일본(닛케이)·중국(상하이·항셍) 아시아 증시 동향
2. 달러·원(USD/KRW) + 외국인 수급 예상
3. 달러인덱스(DXY) → 신흥국 시장 영향
4. 오늘 KOSPI·KOSDAQ 시가 방향 예측
5. 글로벌 매크로 특이사항"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})

        # 미국 지수
        index_lines = [
            f"{_INDEX_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _INDEX_LABELS if (d := data.get(k))
        ]

        # 미국 급등 종목
        us_hot = state.get("us_hot_stocks", [])
        us_impact_text = format_us_impact_for_prompt(us_hot)

        # 미국 섹터 데이터
        sectors = state.get("us_sector_data", {})
        if sectors:
            sorted_sec = sorted(sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True)
            sector_lines = "\n".join(
                f"  {s}: {d.get('change_pct', 0):+.2f}% ({d.get('symbol', '')})"
                for s, d in sorted_sec
            )
            # 강세 섹터 → 한국 종목 사전 매핑 (높음만)
            try:
                from agents.us_impact_agent import US_SECTOR_TO_KR
                strong = [(s, d) for s, d in sorted_sec if d.get("change_pct", 0) > 0.3]
                mapped_lines = []
                for sec, sec_data in strong[:4]:
                    kr = US_SECTOR_TO_KR.get(sec, [])
                    high_conf = [x for x in kr if x["strength"] == "높음"][:3]
                    if high_conf:
                        stocks_str = ", ".join(f"{x['name']}({x['code']})" for x in high_conf)
                        mapped_lines.append(f"▲ {sec} {sec_data['change_pct']:+.1f}% → {stocks_str}")
                mapped_text = "\n".join(mapped_lines) or "강세 섹터 없음"
            except Exception:
                mapped_text = "매핑 데이터 없음"
        else:
            sector_lines = "데이터 없음"
            mapped_text  = "데이터 없음"

        # 52주 신고가
        highs = state.get("us_52w_highs", [])
        highs_text = "\n".join(
            f"  {h['name']}({h['ticker']}): 고점 대비 {h['pct_from_high']:+.1f}%  전일 {h['change_pct']:+.1f}%"
            for h in highs[:5]
        ) or "없음"

        # 글로벌·아시아 지수
        global_lines = [
            f"{_GLOBAL_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _GLOBAL_LABELS if (d := data.get(k))
        ]

        context = (
            "=== 미국 지수 ===\n" + ("\n".join(index_lines) or "데이터 없음") +
            "\n\n=== 미국 급등·거래량 상위 ===\n" + (us_impact_text or "데이터 없음") +
            "\n\n=== 미국 섹터 등락률 ===\n" + sector_lines +
            "\n\n=== 강세 섹터 → 한국 공급망 사전 매핑 ===\n" + mapped_text +
            "\n\n=== 52주 신고가 근접 미국 종목 ===\n" + highs_text +
            "\n\n=== 글로벌·아시아 지수 ===\n" + ("\n".join(global_lines) or "데이터 없음")
        )

        combined = chat(_SYSTEM, context, max_tokens=2500)

        # 세 섹션 파싱
        sec_us     = ""
        sec_impact = ""
        sec_global = ""

        if "=== 미국발 한국 연동 분석 ===" in combined:
            part1, rest = combined.split("=== 미국발 한국 연동 분석 ===", 1)
            sec_us = part1.replace("=== 미국 증시 분석 ===", "").strip()
            if "=== 글로벌·아시아 분석 ===" in rest:
                part2, part3 = rest.split("=== 글로벌·아시아 분석 ===", 1)
                sec_impact = part2.strip()
                sec_global = part3.strip()
            else:
                sec_impact = rest.strip()
        else:
            sec_us = combined

        state["us_market_report"]  = sec_us or combined
        state["us_impact_report"]  = sec_impact or combined
        state["global_market_report"] = sec_global or combined

        logger.info("[미국글로벌팀] 완료 — 지수 %d개, 섹터 %d개", len(index_lines), len(sectors))
    except Exception as e:
        logger.error("[미국글로벌팀] 실패: %s", e)
        state["us_market_report"]     = "분석 실패"
        state["us_impact_report"]     = "분석 실패"
        state["global_market_report"] = "분석 실패"
        state["errors"].append(f"us_global_team: {e}")
    return state
