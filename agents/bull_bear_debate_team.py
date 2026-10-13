"""
agents/bull_bear_debate_team.py
CEO(CIO) 최종 판단 직전, 강세/약세 논리를 명시적으로 맞붙인다.

배경: 2026-09-03 TradingAgents(오픈소스 멀티에이전트 트레이딩 프레임워크) 리서치에서
확인한 Bull/Bear 토론 구조 적용. 기존엔 investment_committee(중립 팩트)·
portfolio_manager_agent(보유종목 관점)·risk_management_team(거시 리스크)이 각자
CEO에게 입력을 주고, CEO 혼자 "독립 판단 원칙"으로 스스로에게 반론을 제기하는
구조였다 — 같은 모델이 혼자 양쪽을 다 맡으면 실제 토론만큼의 긴장이 안 생긴다.

2026-10-13 보강: ① Bull/Bear가 서로 다른 증거(기회 vs 리스크 자료)를 추가로 보게 함 —
같은 입력이면 같은 결론이 나온다. ② 중립 판정자(run_judge)가 어느 쪽이 데이터로 더
탄탄했는지 판정해 CEO에 전달 — 토론을 CEO가 그냥 읽기만 하던 구조 개선.

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

_JUDGE_SYSTEM = """당신은 중립적인 토론 심판입니다. 강세·약세 어느 쪽 편도 들지 않습니다.
아래 두 논리 중 '제공된 데이터의 수치로 더 잘 뒷받침되는 쪽'을 판정하세요.

출력 형식 (정확히 이 4줄):
우세: [강세|약세|팽팽]
결정적 쟁점: [두 논리가 갈리는 핵심 사실 1가지 — 수치 인용]
검증 안 된 주장: [근거 수치 없이 단정한 주장 1가지, 없으면 '없음']
CEO 주의: [이 판정이 오늘 비중·현금 판단에 주는 시사점 한 줄]

규칙: 입력에 없는 수치는 만들지 말 것. 어느 쪽이 '말을 잘해서' 우세로 판정하지 말 것. 한국어."""

# 같은 정보를 보면 같은 결론이 나온다 — 진짜 토론이 되려면 각자 다른 증거를 가져야 한다.
# Bull은 기회·모멘텀 쪽 자료를, Bear는 리스크·악재 쪽 자료를 추가로 본다.
_BULL_EXTRA = [("매크로 레짐", "macro_report"), ("야간 선물·EWY", "futures_report"),
               ("중장기 수혜주", "midterm_stock_report"), ("이슈 종목", "issue_stocks_report")]
_BEAR_EXTRA = [("리스크 관리팀", "risk_report"), ("이벤트 리스크", "event_risk_report"),
               ("글로벌 전문가 서사", "market_intelligence_report"), ("뉴스", "news_report")]


def _slice(state: InvestmentState, parts: list[tuple[str, str]]) -> str:
    out = []
    for label, key in parts:
        v = state.get(key)
        if v:
            out.append(f"[{label}]\n{v}")
    return "\n\n".join(out)


def _context(state: InvestmentState) -> str:
    """공통 컨텍스트 — 시장 방향성·분석팀 인텔리전스·포트폴리오 현황."""
    return (
        f"[시장 방향성] {state.get('market_direction', '중립')}\n\n"
        f"[분석팀 인텔리전스]\n{state.get('committee_report', 'N/A')}\n\n"
        f"[포트폴리오 현황]\n{state.get('portfolio_report', 'N/A')}"
    )


def run_bull(state: InvestmentState) -> InvestmentState:
    _new_errors: list[str] = []
    try:
        context = _context(state) + "\n\n" + _slice(state, _BULL_EXTRA)
        result = chat(_BULL_SYSTEM, context, max_tokens=500)
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
        context = (_context(state) + "\n\n" + _slice(state, _BEAR_EXTRA)
                   + f"\n\n[강세론자 주장]\n{bull or '(강세 논리 없음)'}")
        result = chat(_BEAR_SYSTEM, context, max_tokens=500)
        state["bear_case_report"] = result
        logger.info("[Bear] 완료")
    except Exception as e:
        logger.error("[Bear] 실패: %s", e)
        state["bear_case_report"] = ""
        _new_errors.append(f"bear_case: {e}")
    state["errors"] = _new_errors
    return state


def run_judge(state: InvestmentState) -> InvestmentState:
    """Bull/Bear 논쟁을 판정해 CEO가 '누가 더 근거가 탄탄했는가'를 알게 한다.

    판정이 CEO의 결정을 대신하지는 않는다(CEO는 참고만). Bull/Bear 중 한쪽이라도
    비어 있으면 판정할 수 없으므로 스킵한다 — 빈 문자열은 CEO가 무시한다.
    """
    _new_errors: list[str] = []
    bull, bear = state.get("bull_case_report", ""), state.get("bear_case_report", "")
    if not (bull and bear):
        state["debate_verdict"] = ""
        state["errors"] = _new_errors
        return state
    try:
        ctx = (f"[강세 논리]\n{bull}\n\n[약세 반박]\n{bear}\n\n"
               f"[시장 방향성] {state.get('market_direction', '중립')}")
        state["debate_verdict"] = chat(_JUDGE_SYSTEM, ctx, max_tokens=300)
        logger.info("[Judge] 완료")
    except Exception as e:
        logger.error("[Judge] 실패: %s", e)
        state["debate_verdict"] = ""
        _new_errors.append(f"debate_judge: {e}")
    state["errors"] = _new_errors
    return state
