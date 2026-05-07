import logging
from datetime import datetime
from graph.state import InvestmentState
from clients.openai_client import chat
from config.settings import RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

logger = logging.getLogger(__name__)

_PROMPTS = {
    RUN_TYPE_PRE: """당신은 AI 투자리서치 회사의 CEO입니다. 장전 브리핑을 작성하세요.

구성:
1. 오늘의 시장 한줄 요약
2. 글로벌/선물 시장 핵심 (2-3줄)
3. 오늘 주도 섹터·테마
4. 주목 종목 TOP3 (종목명, 이유, 진입 전략)
5. 리스크 주의사항
6. CEO 한마디 (오늘의 투자 전략 핵심)

형식: 텔레그램 전송용, 이모지 활용, 한국어, 간결하게""",

    RUN_TYPE_INTRA1: """당신은 AI 투자리서치 회사의 CEO입니다. 장중 1차 점검 브리핑(10시)을 작성하세요.

구성:
1. 현재 시장 상황 (KOSPI/KOSDAQ 흐름)
2. 오전 주도 섹터 확인
3. 기존 전략 유효성 점검
4. 추가 매수 / 손절 판단 가이드
5. 오후를 위한 관전 포인트

형식: 텔레그램 전송용, 이모지 활용, 한국어, 간결하게""",

    RUN_TYPE_INTRA2: """당신은 AI 투자리서치 회사의 CEO입니다. 장중 2차 점검 브리핑(13시)을 작성하세요.

구성:
1. 오후 시장 전망
2. 오전 대비 수급 변화
3. 마감을 앞둔 포지션 관리 전략
4. 장마감 전 주목할 이벤트
5. 내일을 위한 준비사항

형식: 텔레그램 전송용, 이모지 활용, 한국어, 간결하게""",

    RUN_TYPE_CLOSE: """당신은 AI 투자리서치 회사의 CEO입니다. 장마감 복기 및 내일 전략 브리핑(15:50)을 작성하세요.

구성:
1. 오늘 장 총평
2. 예측 대비 실제 결과 평가
3. 오늘의 Winner & Loser
4. 내일 시장 예측
5. 내일을 위한 주목 종목·섹터
6. CEO 투자 철학 한마디

형식: 텔레그램 전송용, 이모지 활용, 한국어, 간결하게""",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        run_type = state.get("run_type", RUN_TYPE_PRE)
        now = datetime.now(TZ)

        candidates_text = "\n".join(
            f"- {c.get('name', c.get('code', ''))}: {c.get('change_pct', 0)}% (점수 {c.get('score', 0)})"
            for c in state.get("candidates", [])[:5]
        ) or "후보 없음"

        context = (
            f"날짜: {state.get('date', now.strftime('%Y-%m-%d'))}  시간: {now.strftime('%H:%M')}\n\n"
            f"[위원회 종합]\n{state.get('committee_report', '')}\n\n"
            f"시장 방향성: {state.get('market_direction', '중립')}\n\n"
            f"[주목 종목]\n{candidates_text}\n\n"
            f"[리스크]\n{chr(10).join(state.get('risks', [])[:3])}\n\n"
            f"[복기]\n{state.get('review_report', '')}"
        )
        result = chat(_PROMPTS.get(run_type, _PROMPTS[RUN_TYPE_PRE]), context, max_tokens=3000)
        state["ceo_report"] = result
        logger.info("[CEO] 브리핑 생성 완료")
    except Exception as e:
        logger.error("[CEO] 실패: %s", e)
        state["ceo_report"] = "브리핑 생성 실패"
        state["errors"].append(f"ceo_agent: {e}")
    return state
