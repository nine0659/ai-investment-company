import logging
from datetime import datetime
from graph.state import InvestmentState
from clients.openai_client import chat_ceo
from clients.us_stock_client import format_us_impact_for_prompt
from services.recommendation_service import (
    parse_recommendations, save_recommendations,
    update_close_prices, format_returns_for_report,
)
from config.settings import RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

logger = logging.getLogger(__name__)

_PROMPT_PRE = """당신은 AI 투자리서치 회사의 CEO입니다.
팀 분석을 바탕으로 장전 브리핑을 작성하되, 반드시 아래 항목을 순서대로 출력하세요.
부서별 요약 반복 없이 핵심만 담아 짧고 강력하게 작성하세요.

━━━━━━━━━━━━━━━━━━━━━━
① 오늘의 핵심 판단 (단 한 문장)
   예: "반도체 단기 반등 구간, 삼성전자 눌림 매수 유효"

② 최우선 관심 종목 1~2개 — 분할 매수 전략 포함
   종목당 반드시 아래 형식 (종목 이름과 코드 먼저, 그 다음 줄에 항목):
   종목명(코드) | 근거 한 줄
   · 1차(50%): XXX원 진입
   · 2차(50%): XXX원 (조건 명시 — 예: -2% 눌림 확인 후 / 양봉 전환 후)
   · 손절: XXX원 (1차 기준 -X%)
   · 목표: XXX원 (리스크 대비 2:1 이상)

③ 오늘 절대 하지 말 것 (단 하나)
   예: "갭상승 추격매수 금지" / "2차전지 섹터 오늘 전량 회피"

④ 미국발 오늘 주목 한국 종목 (2~3개, 한 줄씩)
   예: 한화에어로스페이스 — 미국 방산 ETF +3.2% 수혜 직결

⑤ 오늘 주목할 빅피겨 발언 (있을 경우만, 1~2줄)
   예: ⚡ 파월 발언 "금리 동결 재확인" → 금융주 긍정

⑥ 시초가 시나리오 — 위 추천 종목(②)에 대해 반드시 포함
   갭상승 +2% 이상 → 추격 금지, 눌림 대기 후 2차 매수 기회 확인
   갭상승 +1~2%   → 1차 물량 절반(25%)만 진입, 손절 타이트 유지
   보합 출발      → 계획대로 1차 진입
   갭하락 -1% 이하 → 반등 양봉 확인 후 진입, 당일 진입 포기도 고려
━━━━━━━━━━━━━━━━━━━━━━

형식: 텔레그램 전송용, 이모지 활용, 한국어
모든 가격은 제공된 데이터 기반 구체적 수치로 반드시 제시하세요."""

_PROMPT_INTRA1 = """당신은 AI 투자리서치 회사의 CEO입니다. 장중 1차 점검(10시) 브리핑을 작성하세요.

반드시 포함:
① 오전 장 한줄 판단 (상승/하락/박스 + 주도 섹터)
② 오전 전략 유효성 — 유효하면 "유지", 바뀌었으면 새 전략 제시
③ 오후 핵심 관전 포인트 1가지

형식: 텔레그램 전송용, 이모지, 한국어, 간결하게"""

_PROMPT_INTRA2 = """당신은 AI 투자리서치 회사의 CEO입니다. 장중 2차 점검(13시) 브리핑을 작성하세요.

반드시 포함:
① 오후 장 방향 판단 (한 문장)
② 포지션 관리 지침 — 홀드 / 손절 / 추가매수 여부
③ 마감 전 주의사항 1가지

형식: 텔레그램 전송용, 이모지, 한국어, 간결하게"""

_PROMPT_CLOSE = """당신은 AI 투자리서치 회사의 CEO입니다. 장마감 복기 및 내일 전략(15:50)을 작성하세요.

반드시 포함:
① 오늘 장 총평 한 문장 (예측 vs 실제)
② 오늘 추천 종목 수익률 결과 (제공된 데이터 그대로 인용)
③ 내일 최우선 관심 종목 1~2개 — 분할 매수 전략 포함
   종목명(코드) | 근거 한 줄
   · 1차(50%): XXX원 진입
   · 2차(50%): XXX원 (조건 명시)
   · 손절: XXX원 | 목표: XXX원 (리스크 2:1)
④ 내일 절대 하지 말 것 1가지
⑤ CEO 한마디 (투자 철학·교훈)

형식: 텔레그램 전송용, 이모지, 한국어, 간결하게"""

_PROMPTS = {
    RUN_TYPE_PRE:    _PROMPT_PRE,
    RUN_TYPE_INTRA1: _PROMPT_INTRA1,
    RUN_TYPE_INTRA2: _PROMPT_INTRA2,
    RUN_TYPE_CLOSE:  _PROMPT_CLOSE,
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        run_type = state.get("run_type", RUN_TYPE_PRE)
        now  = datetime.now(TZ)
        date = state.get("date", now.strftime("%Y-%m-%d"))

        candidates_text = "\n".join(
            f"- {c.get('name', c.get('code', ''))}: {c.get('change_pct', 0):+.1f}% "
            f"(점수 {c.get('score', 0)})"
            for c in state.get("candidates", [])[:5]
        ) or "후보 없음"

        context_parts = [
            f"날짜: {date}  시간: {now.strftime('%H:%M')}",
            f"시장 방향성: {state.get('market_direction', '중립')}",
            f"\n[위원회 종합]\n{state.get('committee_report', '')}",
            f"\n[주목 종목]\n{candidates_text}",
            f"\n[리스크]\n{chr(10).join(state.get('risks', [])[:3])}",
        ]

        if run_type == RUN_TYPE_PRE:
            us_hot = state.get("us_hot_stocks", [])
            if us_hot:
                context_parts.append(
                    "\n[미국 시장 → 오늘 코스피 이슈 종목]\n"
                    + format_us_impact_for_prompt(us_hot)
                )
            if state.get("us_impact_report"):
                context_parts.append(
                    "\n[미국발 오늘 주목 한국 종목]\n"
                    + state["us_impact_report"]
                )
            if state.get("bigfigure_report"):
                context_parts.append(
                    "\n[오늘 주목할 빅피겨 발언]\n"
                    + state["bigfigure_report"]
                )

        if run_type == RUN_TYPE_CLOSE:
            # 장마감: 오늘 추천 종목 종가 수집 → 수익률 포함
            try:
                from clients.kis_client import KISClient
                kis = KISClient()
                results = update_close_prices(date, kis)
                returns_text = format_returns_for_report(results)
                context_parts.append(f"\n[오늘 추천 종목 수익률]\n{returns_text}")
            except Exception as e:
                logger.warning("[CEO] 종가 수집 실패: %s", e)

        if state.get("review_report"):
            context_parts.append(f"\n[복기]\n{state['review_report']}")

        context = "\n".join(context_parts)
        prompt  = _PROMPTS.get(run_type, _PROMPT_PRE)
        result  = chat_ceo(prompt, context, max_tokens=2000)
        state["ceo_report"] = result

        # 장전 브리핑: 추천 종목 파싱 → DB 저장
        if run_type == RUN_TYPE_PRE:
            try:
                recs = parse_recommendations(result)
                if recs:
                    n = save_recommendations(date, recs)
                    logger.info("[CEO] 추천 종목 %d건 DB 저장 완료", n)
                else:
                    logger.warning("[CEO] 추천 종목 파싱 실패 — 형식 불일치 가능")
            except Exception as e:
                logger.warning("[CEO] 추천 종목 저장 실패: %s", e)

        logger.info("[CEO] 브리핑 생성 완료")
    except Exception as e:
        logger.error("[CEO] 실패: %s", e)
        state["ceo_report"] = "브리핑 생성 실패"
        state["errors"].append(f"ceo_agent: {e}")
    return state
