"""
agents/bull_bear_debate_team.py
CEO(CIO) 최종 판단 직전, 강세/약세 논리를 명시적으로 맞붙인다.

배경: 2026-09-03 TradingAgents(오픈소스 멀티에이전트 트레이딩 프레임워크) 리서치에서
확인한 Bull/Bear 토론 구조 적용. 기존엔 investment_committee(중립 팩트)·
portfolio_manager_agent(보유종목 관점)·risk_management_team(거시 리스크)이 각자
CEO에게 입력을 주고, CEO 혼자 "독립 판단 원칙"으로 스스로에게 반론을 제기하는
구조였다 — 같은 모델이 혼자 양쪽을 다 맡으면 실제 토론만큼의 긴장이 안 생긴다.

run_bear는 run_bull의 결과를 직접 받아 반박해야 하므로 반드시 순차로 실행한다
(병렬 브랜치 간 state 상호소거 함정과 무관 — 애초에 병렬이 아님).
CEO의 출력 스키마(=CIO_DECISION_START= 파싱)는 건드리지 않는다 — 이 팀은 CEO가
읽는 입력 컨텍스트만 풍부하게 할 뿐이다.
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_BULL_SYSTEM = """당신은 강세론자(Bull) 투자 분석가입니다.
아래 분석팀 리포트·포트폴리오 현황·시장 방향성을 근거로, 오늘 판단에 대한
가장 강력한 강세 논리를 제시하세요.

규칙:
- 300~500토큰 이내. 종목/섹터를 구체적으로 지목 — 막연한 낙관론 금지.
- 근거 없는 낙관은 금지 — 제공된 데이터의 수치를 반드시 인용.
- "왜 지금이 좋은 시점인가"에 집중. 이미 다 아는 얘기 반복 금지.
- 한국어."""

_BEAR_SYSTEM = """당신은 약세론자(Bear) 투자 분석가입니다.
방금 강세론자가 제시한 아래 논리를 최소 2가지 구체적으로 반박하세요.

규칙:
- 300~500토큰 이내. 강세론자가 언급한 종목/근거를 직접 지목해 반박 — 일반론 금지.
- 리스크를 새로 나열하지 말고, 반드시 "강세론자가 놓친 것"에 집중.
- 근거 없는 비관은 금지 — 제공된 데이터의 수치를 반드시 인용.
- 한국어."""


def _context(state: InvestmentState) -> str:
    return (
        f"[시장 방향성] {state.get('market_direction', '중립')}\n\n"
        f"[분석팀 인텔리전스]\n{state.get('committee_report', 'N/A')}\n\n"
        f"[포트폴리오 현황]\n{state.get('portfolio_report', 'N/A')}"
    )


def run_bull(state: InvestmentState) -> InvestmentState:
    _new_errors: list[str] = []
    try:
        result = chat(_BULL_SYSTEM, _context(state), max_tokens=500)
        state["bull_case_report"] = result
        logger.info("[Bull] 완료")
    except Exception as e:
        logger.error("[Bull] 실패: %s", e)
        state["bull_case_report"] = ""
        _new_errors.append(f"bull_case: {e}")
    state["errors"] = _new_errors
    return state


def run_bear(state: InvestmentState) -> InvestmentState:
    _new_errors: list[str] = []
    try:
        bull = state.get("bull_case_report", "")
        context = _context(state) + f"\n\n[강세론자 주장]\n{bull or '(강세 논리 없음)'}"
        result = chat(_BEAR_SYSTEM, context, max_tokens=500)
        state["bear_case_report"] = result
        logger.info("[Bear] 완료")
    except Exception as e:
        logger.error("[Bear] 실패: %s", e)
        state["bear_case_report"] = ""
        _new_errors.append(f"bear_case: {e}")
    state["errors"] = _new_errors
    return state
