"""
services/deep_report_service.py
메인 텔레그램 브리핑은 결론(헤드라인+포지션 액션)만 짧게 유지한다.
글로벌 시장 서사·전문가/텔레그램 채널 시각·빅피겨 발언·종목별 기술적·수급 분석처럼
그 압축 과정에서 잘려나가는 분석 원문을 모아 별도 "심층 리포트"로 보존한다.

기술적 분석은 LLM이 지어내지 않도록 services.chart_service의 실제 계산값
(이동평균·거래량·52주 위치·볼린저밴드)을 그대로 사용한다.
"""
import logging

from clients.openai_client import chat
from services.chart_service import analyze_chart, format_chart_summary

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM = """당신은 투자 브리핑 편집자입니다. 아래 심층 분석 전문에서
텔레그램으로 바로 보낼 핵심 요약만 뽑아냅니다. 전문은 그대로 저장되어 있으니
당신은 "무엇을 더 볼 가치가 있는가"만 추려내면 됩니다.

규칙:
- 최대 5줄. 섹션이 여러 개여도 정말 중요한 3~5개만 골라 한 줄씩.
- 각 줄은 반드시 구체적으로: 수치·종목명·방향 포함
  (예: "매크로: 미 10Y 금리 4.3%로 상승, 위험회피 전환")
- 전문에 없는 수치·사실을 만들어내지 마라. 없는 정보는 언급하지 않는다.
- 평소와 다르지 않거나("특이사항 없음" 류) 밋밋한 섹션은 아예 생략 —
  뽑을 내용이 없으면 그 섹션은 건너뛴다.
- 한국어. 헤더나 인사말 없이 요약 줄만 출력."""


def summarize_deep_report(content: str, max_tokens: int = 350) -> str:
    """전문(build_deep_report 결과)을 텔레그램 자동 발송용 3~5줄 요약으로 압축.

    전문 자체는 이 함수 호출 전에 이미 DB에 저장돼 /insight로 조회 가능하므로,
    여기서는 순수 요약 실패가 있어도 전문으로 대체 발송하지 않는다(그러면
    다시 5~7통 플러딩 문제로 되돌아간다) — 실패 시 빈 문자열 반환.
    """
    if not content:
        return ""
    try:
        return chat(_SUMMARY_SYSTEM, content[:8000], max_tokens=max_tokens).strip()
    except Exception as e:
        logger.warning("[심층리포트] 요약 생성 실패 (자동 발송 생략, 전문은 /insight로 조회 가능): %s", e)
        return ""


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

    futures = state.get("futures_report", "")
    if futures:
        sections.append("🌙 *선물시장 분석 (오버나이트 신호 → 오늘 수혜 섹터/종목)*\n" + futures)

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
