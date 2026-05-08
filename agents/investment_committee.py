import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.learning_service import load_learned_weights

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자위원회 의장입니다.
각 분석팀의 리포트를 종합하여 투자 의사결정을 위한 통합 의견을 도출하세요.

평가 항목:
1. 각 팀 의견 점수화 (강세·약세 신호 집계)
2. 시장 방향성 결정 (강한상승·상승·중립·하락·강한하락)
3. 오늘의 최우선 투자 전략
4. 포지션 크기 제안 (공격적·일반·보수적·관망)
5. 핵심 리스크 요인과 대응 방안

출력:
- 시장 방향성: [방향]
- 투자 전략: [전략]
- 포지션 크기: [크기]
- 위원회 종합 의견 (5줄 이내)
- 핵심 근거 3가지"""

_DIRECTIONS = ["강한상승", "상승", "강한하락", "하락", "중립"]


def run(state: InvestmentState) -> InvestmentState:
    try:
        parts = [
            f"[선물/파생팀]\n{state.get('futures_report', 'N/A')}",
            f"[미국시장팀]\n{state.get('us_market_report', 'N/A')}",
            f"[한국현물팀]\n{state.get('korea_spot_report', 'N/A')}",
            f"[글로벌팀]\n{state.get('global_market_report', 'N/A')}",
            f"[뉴스분석팀]\n{state.get('news_report', 'N/A')}",
            f"[섹터/테마팀]\n{state.get('sector_report', 'N/A')}",
            f"[수급팀]\n{state.get('money_flow_report', 'N/A')}",
            f"[리스크팀]\n{state.get('risk_report', 'N/A')}",
        ]

        weights = load_learned_weights()
        if weights and "(초기 데이터 없음)" not in weights:
            parts.insert(0, f"[학습된 가중치 — 반드시 우선 반영]\n{weights}")

        context = "\n\n".join(parts)
        result = chat(_SYSTEM, context, max_tokens=2500)
        state["committee_report"] = result
        state["market_direction"] = next(
            (d for d in _DIRECTIONS if d in result), "중립"
        )
        logger.info("[투자위원회] 완료 — 방향: %s", state["market_direction"])
    except Exception as e:
        logger.error("[투자위원회] 실패: %s", e)
        state["committee_report"] = "위원회 의견 생성 실패"
        state["errors"].append(f"committee: {e}")
    return state
