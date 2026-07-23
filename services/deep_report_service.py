"""
services/deep_report_service.py
메인 텔레그램 브리핑은 결론(헤드라인+포지션 액션)만 짧게 유지한다.
글로벌 시장 서사·전문가/텔레그램 채널 시각·빅피겨 발언·종목별 기술적·수급 분석처럼
그 압축 과정에서 잘려나가는 분석 원문을 모아 별도 "심층 리포트"로 보존한다.

기술적 분석은 LLM이 지어내지 않도록 services.chart_service의 실제 계산값
(이동평균·거래량·52주 위치·볼린저밴드)을 그대로 사용한다.
"""
import logging

from services.chart_service import analyze_chart, format_chart_summary

logger = logging.getLogger(__name__)


def _collect_stock_codes(state: dict, limit: int = 8) -> list[dict]:
    """오늘 브리핑이 다룬 종목(신규/조정/보유/후보)을 중복 제거해 모은다."""
    seen: dict[str, str] = {}

    decisions = state.get("ceo_decisions") or {}
    for group in ("new_positions", "position_changes", "position_holds"):
        for item in decisions.get(group, []):
            code = item.get("code", "")
            if code and code not in seen:
                seen[code] = item.get("name", code)

    for cand in state.get("candidates", []) or []:
        code = cand.get("code", "")
        if code and code not in seen and len(seen) < limit:
            seen[code] = cand.get("name", code)

    return [{"code": c, "name": n} for c, n in list(seen.items())[:limit]]


def build_deep_report(state: dict) -> str:
    """state에 이미 계산된 분석 리포트들 + 종목별 차트 지표를 모아 심층 리포트 텍스트 생성."""
    sections: list[str] = []

    macro = state.get("macro_report", "")
    if macro:
        sections.append("🌍 *매크로 분석*\n" + macro)

    intel = state.get("market_intelligence_report", "")
    if intel:
        sections.append("🌐 *글로벌 시장 서사 · 전문가/텔레그램 채널 시각*\n" + intel)

    bigfigure = state.get("bigfigure_report", "")
    if bigfigure:
        sections.append("🎙 *빅피겨 발언*\n" + bigfigure)

    news = state.get("news_report", "")
    if news:
        sections.append("📰 *뉴스 분석*\n" + news)

    issue = state.get("issue_stocks_report", "")
    if issue:
        sections.append("🔥 *이슈 종목*\n" + issue)

    flow = state.get("money_flow_report", "")
    if flow:
        sections.append("💰 *수급 분석*\n" + flow)

    stocks = _collect_stock_codes(state)
    if stocks:
        lines = ["📈 *종목별 기술적·수급 체크* (이동평균·거래량·52주 위치 실측값)"]
        for s in stocks:
            try:
                ch = analyze_chart(s["code"], s["name"])
                lines.append("  " + format_chart_summary(s["code"], s["name"], ch))
            except Exception as e:
                logger.debug("[심층리포트] 차트분석 실패 %s(%s): %s", s["name"], s["code"], e)
        if len(lines) > 1:
            sections.append("\n".join(lines))

    return "\n\n".join(sections)
