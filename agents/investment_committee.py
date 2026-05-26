import logging
import re
from graph.state import InvestmentState
from clients.openai_client import chat
from services.learning_service import load_learned_weights

logger = logging.getLogger(__name__)

# "시장 방향성: 강한상승" 형식에서 추출 — 콜론 뒤 공백 포함, 전후 오염 제거
_DIR_LABEL_RE = re.compile(r"시장 방향성[：:\s]+([강한상승하락중립]+)")

_SYSTEM = """당신은 투자위원회 의장입니다.
각 분석팀의 리포트를 종합하여 오늘 한국 시장 투자 의사결정을 도출하세요.

핵심 원칙 (의결 우선순위):
  0순위: 글로벌 매크로 레짐 — RISK-ON/OFF/NEUTRAL이 전체 포지션 크기와 섹터 방향의 틀을 결정
  0-1순위: 이벤트 리스크 — FOMC·CPI·옵션만기 등 HIGH 이벤트 시 포지션 자동 50% 축소
  1순위: 전일 미국 증시 방향 — S&P500·NASDAQ·SOX 방향이 오늘 KOSPI 방향의 70%를 결정
  2순위: 달러/원 환율·미국 금리 — 외국인 수급 방향 결정
  3순위: 국내 수급(외국인·기관) + EWY/EEM 글로벌 자금 흐름 + 섹터 모멘텀
  4순위: 뉴스·공시·빅피겨 발언 재료

[매크로 레짐 → 포지션 크기 원칙]
- RISK-ON  + 미국 강세 일치: 공격적 (자산의 50~70%)
- RISK-ON  + 미국·국내 엇갈림: 보통 (30~50%)
- NEUTRAL  : 보통 (30~50%), 확신 종목만
- RISK-OFF : 보수적 (10~30%), 방어주·현금 비중 확대
- RISK-OFF + 미국 하락: 최소화 (10% 이하) 또는 관망
- 이벤트 리스크 HIGH 시: 위 기준에서 추가 50% 축소 (예: RISK-ON 50~70% → 25~35%)

[필수 출력 규칙]
- 시장 방향 판단은 반드시 확률(%)로 표현: "상승확률 XX%, 하락확률 YY%" 형식
- 확률 없는 "상승 예상", "하락 가능" 같은 막연한 표현 금지
- 미국 신호와 매크로 레짐이 불일치할 때 반드시 명시하고 확률 조정
- 근거 없는 확률 배분 금지 — 제공된 데이터에서 도출

평가 항목:
1. [매크로 레짐 확인] 오늘 레짐(Risk-On/Off/Neutral) + 섹터 로테이션 힌트
2. [미국 증시 선행 신호] S&P500·NASDAQ·SOX → KOSPI 상승확률/하락확률
3. [환율·금리 필터] 원달러·미국 금리 → 외국인 수급 방향
4. [국내 섹터 수급] KIS 수급 데이터 교차 검증
5. 시장 방향성 최종 결정 (강한상승·상승·중립·하락·강한하락)

출력 형식:
- [매크로 레짐]: RISK-ON/OFF/NEUTRAL + 한 줄 근거
- [미국 신호 판독]: 한 줄 결론 + S&P500·NASDAQ 수치
- 상승확률: XX% / 하락확률: YY%
- 시장 방향성: [방향]
- 투자 전략: [전략]
- 포지션 크기: [크기] (투자 자산의 몇 %)
- 위원회 종합 의견 (5줄 이내)
- 핵심 근거 3가지"""

_DIRECTIONS = ["강한상승", "상승", "강한하락", "하락", "중립"]


def run(state: InvestmentState) -> InvestmentState:
    try:
        event_level = state.get("event_risk_level", "중간")
        parts = [
            f"[매크로팀 — 0순위: 전체 투자 환경의 틀]\n{state.get('macro_report', 'N/A')}",
            f"[이벤트리스크팀 — 0-1순위: 이벤트 레벨={event_level}]\n{state.get('event_risk_report', 'N/A')}",
            f"[인텔리전스팀 — 글로벌 전문가 서사·강세론/약세론·컨센서스]\n{state.get('market_intelligence_report', 'N/A')}",
            f"[선물/파생팀]\n{state.get('futures_report', 'N/A')}",
            f"[미국시장팀]\n{state.get('us_market_report', 'N/A')}",
            f"[한국현물팀]\n{state.get('korea_spot_report', 'N/A')}",
            f"[글로벌팀]\n{state.get('global_market_report', 'N/A')}",
            f"[뉴스분석팀]\n{state.get('news_report', 'N/A')}",
            f"[섹터/테마팀]\n{state.get('sector_report', 'N/A')}",
            f"[수급팀 — EWY/EEM 글로벌 자금 포함]\n{state.get('money_flow_report', 'N/A')}",
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
        # "시장 방향성: XXX" 라벨 우선 추출, 없으면 첫 발견 단어로 폴백
        m = _DIR_LABEL_RE.search(result)
        if m:
            label = m.group(1).strip()
            direction = next((d for d in _DIRECTIONS if d in label), "중립")
        else:
            direction = next((d for d in _DIRECTIONS if d in result), "중립")
        state["market_direction"] = direction
        logger.info("[투자위원회] 완료 — 방향: %s", direction)
    except Exception as e:
        logger.error("[투자위원회] 실패: %s", e)
        state["committee_report"] = "위원회 의견 생성 실패"
        state["errors"].append(f"committee: {e}")
    return state
