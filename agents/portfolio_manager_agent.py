"""
portfolio_manager_agent.py
실제 보유 포트폴리오 + 워치리스트 통합 분석 에이전트

역할:
  - 보유 종목별 hold/add/reduce/exit 판단
  - 워치리스트 진입 조건 충족 종목 신호
  - 단기/중기/장기 포지션 조정 전략
  - 매크로·수급·리스크 환경 반영한 포트폴리오 전략

파이프라인 위치: ceo_agent 직전 삽입 → CEO가 최종 통합
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from clients.kis_client import KISClient
from services.portfolio_service import calculate_pnl, get_portfolio_summary, format_portfolio_for_briefing
from services.watchlist_service import get_watchlist, check_triggers, format_watchlist_for_briefing

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

_SYSTEM = """당신은 실제 포트폴리오를 운용하는 전문 투자매니저입니다.
투자자의 실제 보유 포지션과 관심종목 현황을 분석하여 포트폴리오 방향을 제시하세요.
단기 매매 타이밍 제시 금지. 포트폴리오 관점의 중장기 방향과 리스크 관리를 중심으로 판단합니다.

[분석 원칙]
- 보유 종목: "왜 지금 보유해야 하는가" — 투자 근거 재검증
- 수익 중인 종목: 비중 조절 타당성 vs 추가 편입 여부 — 투자 근거 기반 판단
- 손실 중인 종목: 리스크 기준선 관리 vs 홀드 — 반드시 명확한 근거 제시
- 워치리스트: 편입 검토 조건 충족 여부 — 중장기 투자 근거와 조건 제시

[출력 규칙]
- 종목마다 방향 명확화: 홀드 / 비중 확대 / 비중 축소 / 편입 검토
- 포지션 크기 명시: "현재 비중 X% → 목표 비중 Y%"
- 리스크 기준선 확인: 현재 설정이 적절한지 시장 상황 반영하여 재검토
- "상황을 지켜보자"는 표현 절대 금지 — 반드시 조건부 방향 제시
- 단기 진입가·손절가·목표가 수치 제시 금지 (투기 조장)

[언어] 한국어 텔레그램 텍스트, 섹션 번호 유지"""


def _format_pnl_for_prompt(pnl_data: list[dict]) -> str:
    if not pnl_data:
        return "보유 포지션 없음"
    tf_map = {"short": "단기", "mid": "중기", "long": "장기"}
    lines = []
    for p in pnl_data:
        tf = tf_map.get(p.get("timeframe", "short"), "단기")
        target_str = f" | 목표가: {p['target_price']:,.0f}원" if p.get("target_price") else ""
        stop_str   = f" | 손절가: {p['stop_price']:,.0f}원" if p.get("stop_price") else ""
        lines.append(
            f"{p['name']}({p['code']}) [{tf}]"
            f"\n  {p['quantity']}주 | 평균단가 {p['avg_price']:,.0f}원 | 현재가 {p['current_price']:,.0f}원"
            f"\n  손익: {p['pnl_pct']:+.2f}% ({p['pnl_amt']:+,.0f}원) | {p['status_flag']}"
            f"{target_str}{stop_str}"
            + (f"\n  투자근거: {p['memo']}" if p.get("memo") else "")
        )
    return "\n\n".join(lines)


def _format_watchlist_for_prompt(items: list[dict], triggered: list[dict]) -> str:
    if not items:
        return "워치리스트 없음"
    triggered_codes = {t["code"] for t in triggered}
    tf_map = {"short": "단기", "mid": "중기", "long": "장기"}
    lines = []
    for item in items:
        tf = tf_map.get(item.get("timeframe", "short"), "단기")
        is_triggered = item["code"] in triggered_codes
        trigger_status = "🚨진입조건충족" if is_triggered else "⏳대기중"
        trigger_detail = ""
        if is_triggered:
            t = next((t for t in triggered if t["code"] == item["code"]), {})
            trigger_detail = f"\n  ★ {t.get('trigger_msg', '')}"
        entry_str = f" | 목표진입: {item['target_entry']:,.0f}원" if item.get("target_entry") else ""
        lines.append(
            f"{trigger_status} {item['name']}({item['code']}) [{tf}]{entry_str}"
            + (f"\n  주목이유: {item['reason']}" if item.get("reason") else "")
            + trigger_detail
        )
    return "\n\n".join(lines)


def run(state: InvestmentState) -> InvestmentState:
    try:
        portfolio = calculate_pnl()  # 현재가 없이 기본 계산 (KIS 호출 비용 절약)
        watchlist = get_watchlist("active")

        if not portfolio and not watchlist:
            logger.info("[포트폴리오매니저] 보유 포지션·워치리스트 모두 없음 — 스킵")
            state["portfolio_report"] = ""
            return state

        # KIS로 현재가 조회 (포트폴리오가 있을 때만)
        triggered = []
        if portfolio or watchlist:
            try:
                kis = KISClient()
                portfolio = calculate_pnl(kis)
                triggered = check_triggers(kis)
            except Exception as e:
                logger.warning("[포트폴리오매니저] KIS 조회 실패: %s", e)

        summary = get_portfolio_summary(portfolio)

        # 컨텍스트 구성
        pnl_text      = _format_pnl_for_prompt(portfolio)
        watchlist_text = _format_watchlist_for_prompt(watchlist, triggered)

        # 시장 환경 요약 (CEO 이전 에이전트들의 결과 활용)
        market_ctx_parts = []
        if state.get("macro_report"):
            market_ctx_parts.append(f"[매크로 레짐]\n{state['macro_report'][:500]}")
        if state.get("risk_report"):
            market_ctx_parts.append(f"[리스크 환경]\n{state['risk_report'][:400]}")
        if state.get("money_flow_report"):
            market_ctx_parts.append(f"[수급 현황]\n{state['money_flow_report'][:400]}")
        if state.get("sector_report"):
            market_ctx_parts.append(f"[섹터 현황]\n{state['sector_report'][:400]}")
        market_ctx = "\n\n".join(market_ctx_parts) if market_ctx_parts else "시장 분석 데이터 없음"

        # 포트폴리오 요약 통계
        summary_text = (
            f"총 {summary['count']}종목 | 총 투자금: {summary['total_invested']:,.0f}원 "
            f"| 평가금액: {summary['total_current']:,.0f}원 "
            f"| 총 손익: {summary['total_pnl_amt']:+,.0f}원 ({summary['total_pnl_pct']:+.2f}%)"
        ) if summary["count"] > 0 else "보유 포지션 없음"

        tf_breakdown = summary.get("timeframe_breakdown", {})
        tf_text = f"단기:{tf_breakdown.get('short',0)}% | 중기:{tf_breakdown.get('mid',0)}% | 장기:{tf_breakdown.get('long',0)}%"

        context = f"""날짜: {state.get('date', datetime.now(_TZ).strftime('%Y-%m-%d'))}
시장 방향성: {state.get('market_direction', '중립')}
이벤트 리스크: {state.get('event_risk_level', '중간')}

[포트폴리오 요약]
{summary_text}
기간별 비중: {tf_text}

[보유 종목 현황 (손익 포함)]
{pnl_text}

[관심종목 (워치리스트)]
{watchlist_text}

[시장 환경]
{market_ctx}"""

        prompt = f"""{_SYSTEM}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 포트폴리오 매니저 리포트
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 포트폴리오 총평 (한 줄)
→ [수익/손실 상태] | 오늘 시장 대비 포트폴리오 [아웃퍼폼/언더퍼폼]

① 보유 종목 행동 지시 (종목별 1줄 결론 → 근거 → 구체적 조건)
형식: [행동] 종목명 — 조건: [수치 기반 조건]
  ✅ 홀드: [유지 조건]
  ➕ 추가매수: [비중 X% 추가] → 조건: [진입 기준]
  ➖ 분할매도: [XX% 축소] → 트리거: [수치]
  🛑 전량매도: → 이유: [수치 근거]
  🔄 손절가 재설정: [기존 → 새 손절가] → 이유

② 워치리스트 진입 판단 (조건 충족 종목)
  ✅ 진입: 종목명(코드) — 진입가: [X원] | 손절: [Y원] | 목표: [Z원] | 비중: X%
  ⏳ 대기: 종목명 — [추가 확인 조건]

③ 오늘 포트폴리오 리스크 점검
→ 최대 손실 시나리오: [X% 손실 가능] — 트리거: [조건]
→ 헤지 필요 여부: [필요/불필요] — 이유 한 줄

④ 단기/중기/장기 조율 전략
단기: [오늘~이번주 행동]
중기: [이번달 목표·조정 방향]
장기: [분기 관점 리밸런싱 필요 여부]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

        result = chat(prompt, context, max_tokens=2000)
        state["portfolio_report"] = result
        logger.info("[포트폴리오매니저] 완료 — 보유 %d종목, 워치리스트 %d개, 트리거 %d개",
                    len(portfolio), len(watchlist), len(triggered))

        # 트리거된 워치리스트 종목이 있으면 별도 알림
        if triggered:
            state.setdefault("errors", [])
            # 트리거 정보는 portfolio_report에 포함되므로 별도 알림 불필요
            logger.info("[포트폴리오매니저] 진입 조건 충족 종목: %s",
                        ", ".join(f"{t['name']}({t['code']})" for t in triggered))

    except Exception as e:
        logger.error("[포트폴리오매니저] 실패: %s", e)
        state["portfolio_report"] = ""
        state.setdefault("errors", []).append(f"portfolio_manager_agent: {e}")
    return state
