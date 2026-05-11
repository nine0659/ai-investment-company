"""
글로벌 빅피겨 발언/행보 분석 → 시장 영향 평가 + 즉시 알림
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)

# AI 거부 응답 감지 (첫 100자 기준)
_REFUSAL_PHRASES = [
    "I'm sorry", "I cannot assist", "I can't assist",
    "Sorry, but I", "I apologize, but", "죄송합니다만",
]

_SYSTEM = """당신은 글로벌 빅피겨 발언 분석 전문가입니다.
수집된 뉴스를 분석하여 오늘 한국 시장에 미칠 영향을 평가하세요.

각 인물에 대해 (뉴스가 있는 경우만):
1. 핵심 발언/행보 요약 (1~2줄)
2. 시장 영향: 긍정/부정/중립 + 이유
3. 영향받을 한국 섹터·종목 (구체적으로)
4. 단기(당일) vs 중기(1주) 영향 구분
5. 중요도: 상/중/하

중요도 '상'인 경우 줄 앞에 ⚡ 표시.
뉴스가 평범하거나 영향이 미미하면 "중/하"로 간결하게 처리.
출력 헤더: "[오늘 주목할 빅피겨 발언]" """


def _is_refusal(text: str) -> bool:
    """OpenAI 거부 응답 여부 판단 — 첫 100자만 검사."""
    snippet = text.strip()[:100].lower()
    return not snippet or any(p.lower() in snippet for p in _REFUSAL_PHRASES)


def _extract_urgent_lines(ai_response: str) -> list[str]:
    """AI가 ⚡로 표시한 중요도 '상' 줄만 추출.
    20자 미만 줄(섹션 헤더·단순 기호)은 단순 뉴스 제목으로 간주해 제외.
    """
    lines = []
    for line in ai_response.split("\n"):
        line = line.strip()
        if line.startswith("⚡") and len(line) > 20:
            lines.append(line)
    return lines[:3]


def run(state: InvestmentState) -> InvestmentState:
    try:
        news_list = state.get("bigfigure_news", [])
        if not news_list:
            state["bigfigure_report"] = "빅피겨 뉴스 없음"
            return state

        # 컨텍스트 구성
        parts: list[str] = []
        for item in news_list:
            name   = item["name_ko"]
            org    = item["org"]
            sector = item["sector"]
            titles = [n["title"] for n in item["news_items"] if n.get("title")]
            if not titles:
                continue
            section = f"[{name} / {org} / {sector}]\n" + "\n".join(f"  - {t}" for t in titles)
            parts.append(section)

        if not parts:
            state["bigfigure_report"] = "빅피겨 주요 뉴스 없음"
            return state

        context = "=== 글로벌 빅피겨 최신 뉴스 ===\n\n" + "\n\n".join(parts)
        result  = chat(_SYSTEM, context, max_tokens=1500)

        # OpenAI 거부 응답 필터링 — 거부 시 텔레그램 전송 차단 후 조기 반환
        if _is_refusal(result):
            logger.warning("[빅피겨] AI 거부 응답 감지 — 텔레그램 전송 차단")
            state["bigfigure_report"] = "[빅피겨 분석 일시 불가]"
            return state

        state["bigfigure_report"] = result

        # 긴급 알림: 키워드 pre-filter 제거, AI 판단(⚡)만을 기준으로 발송
        # 장전 브리핑에도 동일 내용이 포함되므로 알림에 안내 문구 추가
        urgent_lines = _extract_urgent_lines(result)
        if urgent_lines:
            try:
                alert_text = (
                    "⚡ 빅피겨 긴급 발언 알림 ⚡\n"
                    "(전체 분석은 장전 브리핑에 포함)\n\n"
                    + "\n".join(urgent_lines)
                )
                send_message(alert_text)
                logger.info("[빅피겨] 긴급 발언 알림 발송 %d건", len(urgent_lines))
            except Exception as e:
                logger.warning("[빅피겨] 텔레그램 알림 실패: %s", e)
        else:
            logger.info("[빅피겨] 중요도 '상' 항목 없음 — 긴급 알림 미발송")

        logger.info("[빅피겨팀] 완료")
    except Exception as e:
        logger.error("[빅피겨팀] 실패: %s", e)
        state["bigfigure_report"] = "분석 실패"
        state["errors"].append(f"bigfigure_agent: {e}")
    return state
