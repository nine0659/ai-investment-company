"""
agents/investment_committee.py
투자 분석팀 — 시장 인텔리전스 보고서 작성

역할: 각 팀 데이터를 종합하여 CIO에게 팩트와 신호를 전달.
      투자 결론·전략·매수 추천 출력 금지 — 최종 판단은 CIO 권한.
"""
import logging
import re
from graph.state import InvestmentState
from clients.openai_client import chat
from services.learning_service import load_learned_weights

logger = logging.getLogger(__name__)

_DIR_LABEL_RE = re.compile(r"시장 방향성[：:\s]+([강한상승하락중립]+)")

_SYSTEM = """당신은 투자 분석팀 수석 애널리스트입니다.
각 팀의 리포트를 통합하여 CIO(최고투자책임자)에게 전달할 시장 인텔리전스 보고서를 작성하세요.

핵심 원칙:
- 투자 결론·전략·매수 추천 절대 출력 금지
- 팩트·수치·신호만 정리. 최종 판단은 CIO 권한
- 데이터가 엇갈릴 때 어느 쪽도 편들지 말고 양면을 모두 제시

분석 우선순위:
  0순위: 매크로 레짐 (RISK-ON/OFF/NEUTRAL) — 전체 포지션 크기의 틀
  0-1순위: 이벤트 리스크 (FOMC·CPI·옵션만기 등) — 변동성 구간 여부
  1순위: 미국 증시 선행 신호 (S&P500·NASDAQ·SOX)
  2순위: 환율·금리 → 외국인 수급 방향
  3순위: 국내 수급·섹터 모멘텀
  4순위: 뉴스·공시·빅피겨 발언

[필수 출력 형식]
[매크로 레짐] RISK-ON / NEUTRAL / RISK-OFF — 근거 수치 포함
[이벤트 리스크] HIGH / MEDIUM / LOW — 구체적 이벤트명
[시장 방향 확률] 상승확률 XX% / 하락확률 YY% — 근거 한 줄
[시장 방향성] [강한상승 / 상승 / 중립 / 하락 / 강한하락]
[수급 팩트] 외국인/기관 순매수도 규모 + EWY/EEM 신호
[섹터 신호] 강세 섹터 3개 / 약세 섹터 — 수급 근거 포함
[종목 신호] 수급·기술 신호가 강한 종목 5개 이내 — 신호 근거만, 추천 없음
[리스크 요인] 상위 3가지 — 구체적 조건과 임계값 명시
[분석팀 전달 사항] CIO에게 전달할 핵심 팩트 3가지 (판단 없이)"""

_DIRECTIONS = ["강한상승", "상승", "강한하락", "하락", "중립"]


def run(state: InvestmentState) -> InvestmentState:
    try:
        event_level = state.get("event_risk_level", "중간")
        parts = [
            f"[매크로팀 — 0순위: 전체 투자 환경의 틀]\n{state.get('macro_report', 'N/A')}",
            f"[이벤트리스크팀 — 이벤트 레벨={event_level}]\n{state.get('event_risk_report', 'N/A')}",
            f"[인텔리전스팀 — 글로벌 전문가 서사·컨센서스]\n{state.get('market_intelligence_report', 'N/A')}",
            f"[선물/파생팀]\n{state.get('futures_report', 'N/A')}",
            f"[미국시장팀]\n{state.get('us_market_report', 'N/A')}",
            f"[한국현물팀]\n{state.get('korea_spot_report', 'N/A')}",
            f"[글로벌팀]\n{state.get('global_market_report', 'N/A')}",
            f"[뉴스분석팀]\n{state.get('news_report', 'N/A')}",
            f"[섹터/테마팀]\n{state.get('sector_report', 'N/A')}",
            f"[수급팀 — EWY/EEM 포함]\n{state.get('money_flow_report', 'N/A')}",
            f"[리스크팀]\n{state.get('risk_report', 'N/A')}",
        ]

        if state.get("dart_disclosures"):
            from agents.dart_alert_agent import format_disclosures_for_briefing
            dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
            if dart_text:
                parts.append(f"[DART 공시 — 4순위 재료]\n{dart_text}")

        weights = load_learned_weights()
        if weights and "(초기 데이터 없음)" not in weights:
            parts.insert(0, f"[학습된 가중치 — 우선 반영]\n{weights}")

        context = "\n\n".join(parts)
        result = chat(_SYSTEM, context, max_tokens=2000)

        # 확률 합계 정규화 (100%로 보정)
        up_m   = re.search(r"상승확률[^\d]*(\d+)", result)
        down_m = re.search(r"하락확률[^\d]*(\d+)", result)
        if up_m and down_m:
            up_pct, down_pct = int(up_m.group(1)), int(down_m.group(1))
            total = up_pct + down_pct
            if total != 100 and total > 0:
                adj_up   = round(up_pct / total * 100)
                adj_down = 100 - adj_up
                result = result.replace(up_m.group(0), f"상승확률 {adj_up}%")
                result = result.replace(down_m.group(0), f"하락확률 {adj_down}%")
                logger.warning("[분석팀] 확률 합계 %d%% → 100%%로 보정", total)

        state["committee_report"] = result

        # 시장 방향 추출 (하위 노드 사용)
        m = _DIR_LABEL_RE.search(result)
        if m:
            label = m.group(1).strip()
            direction = next((d for d in _DIRECTIONS if d in label), "중립")
        else:
            direction = next((d for d in _DIRECTIONS if d in result), "중립")
        state["market_direction"] = direction

        logger.info("[분석팀] 완료 — 방향: %s (상승확률 %s%% / 하락확률 %s%%)",
                    direction,
                    up_m.group(1) if up_m else "?",
                    down_m.group(1) if down_m else "?")
    except Exception as e:
        logger.error("[분석팀] 실패: %s", e)
        state["committee_report"] = "분석팀 보고 실패"
        state["errors"].append(f"committee: {e}")
    return state
