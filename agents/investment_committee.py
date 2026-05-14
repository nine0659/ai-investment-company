import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.learning_service import load_learned_weights

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자위원회 의장입니다.
각 분석팀의 리포트를 종합하여 오늘 한국 시장 투자 의사결정을 도출하세요.

핵심 원칙 (의결 우선순위):
  1순위: 전일 미국 증시 방향 — S&P500·NASDAQ·SOX 방향이 오늘 KOSPI 방향의 70%를 결정
  2순위: 달러/원 환율·미국 금리 — 외국인 수급 방향 결정
  3순위: 국내 수급(외국인·기관) + 섹터 모멘텀
  4순위: 뉴스·공시·빅피겨 발언 재료

[필수 출력 규칙]
- 시장 방향 판단은 반드시 확률(%)로 표현: "상승확률 XX%, 하락확률 YY%" 형식 사용
- 확률 없는 "상승 예상", "하락 가능" 같은 막연한 방향 표현 금지
- 미국 신호와 한국 신호가 불일치할 때 반드시 명시하고 확률 조정
- 근거 없는 확률 배분 금지 — 반드시 제공된 데이터에서 도출

평가 항목:
1. [미국 증시 선행 신호] S&P500·NASDAQ·SOX 방향 → KOSPI 방향 상승확률/하락확률 산출
2. [환율·금리 필터] 원달러 환율·미국 금리가 외국인 수급에 미치는 방향과 그 확률 조정 효과
3. [국내 섹터 수급] 미국 섹터 연동 + KIS 수급 데이터 교차 검증 → 섹터별 모멘텀 강도
4. 시장 방향성 최종 결정 (강한상승·상승·중립·하락·강한하락)
5. 포지션 크기: 미국 신호와 국내 신호가 같은 방향 → 공격적(자산의 50~70%), 엇갈림 → 보수적(30% 이하)

출력 형식:
- [미국 신호 판독]: 한 줄 결론 + S&P500·NASDAQ 방향 수치 포함
- 상승확률: XX% / 하락확률: YY% (반드시 합계 100%)
- 시장 방향성: [방향]
- 투자 전략: [전략]
- 포지션 크기: [크기] (투자 자산의 몇 %)
- 위원회 종합 의견 (5줄 이내, 각 줄 구체적 사실 기반)
- 핵심 근거 3가지 (1순위: 미국 연동, 2순위: 환율·금리, 3순위: 수급)"""

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

        if state.get("dart_disclosures"):
            from agents.dart_alert_agent import format_disclosures_for_briefing
            dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
            if dart_text:
                parts.append(f"[오늘 DART 공시 — 4순위 재료로 반영]\n{dart_text}")

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
