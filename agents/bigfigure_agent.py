"""
글로벌 빅피겨 발언/행보 분석 → 시장 영향 평가 + 즉시 알림
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)

# 중요 키워드: 이 단어가 제목에 포함되면 즉시 알림
_URGENT_KEYWORDS = [
    "금리 인상", "금리 인하", "긴급", "파산", "폭락", "급락", "위기",
    "rate hike", "rate cut", "emergency", "crash", "crisis", "bankrupt",
    "massive layoff", "acquisition", "breakthrough", "대규모 감원", "M&A",
]

_SYSTEM = """당신은 글로벌 빅피겨 발언 분석 전문가입니다.
수집된 뉴스를 분석하여 오늘 한국 시장에 미칠 영향을 평가하세요.

각 인물에 대해 (뉴스가 있는 경우만):
1. 핵심 발언/행보 요약 (1~2줄)
2. 시장 영향: 긍정/부정/중립 + 이유
3. 영향받을 한국 섹터·종목 (구체적으로)
4. 단기(당일) vs 중기(1주) 영향 구분
5. 중요도: 상/중/하

중요도 '상'인 경우 앞에 ⚡ 표시.
뉴스가 평범하거나 영향이 미미하면 "중/하"로 간결하게 처리.
출력 헤더: "[오늘 주목할 빅피겨 발언]" """


def _is_urgent(text: str) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in _URGENT_KEYWORDS)


def run(state: InvestmentState) -> InvestmentState:
    try:
        news_list = state.get("bigfigure_news", [])
        if not news_list:
            state["bigfigure_report"] = "빅피겨 뉴스 없음"
            return state

        # 컨텍스트 구성
        parts: list[str] = []
        urgent_alerts: list[str] = []

        for item in news_list:
            name   = item["name_ko"]
            org    = item["org"]
            sector = item["sector"]
            titles = [n["title"] for n in item["news_items"] if n.get("title")]

            if not titles:
                continue

            section = f"[{name} / {org} / {sector}]\n" + "\n".join(f"  - {t}" for t in titles)
            parts.append(section)

            # 긴급 키워드 감지
            for news_item in item["news_items"]:
                full_text = news_item.get("title", "") + " " + news_item.get("summary", "")
                if _is_urgent(full_text):
                    urgent_alerts.append(f"⚡ {name}({org}): {news_item['title']}")

        if not parts:
            state["bigfigure_report"] = "빅피겨 주요 뉴스 없음"
            return state

        context = "=== 글로벌 빅피겨 최신 뉴스 ===\n\n" + "\n\n".join(parts)
        result  = chat(_SYSTEM, context, max_tokens=1500)
        state["bigfigure_report"] = result

        # 긴급 발언 즉시 텔레그램 별도 알림
        if urgent_alerts:
            try:
                alert_text = "⚡ 빅피겨 중요 발언 긴급 알림 ⚡\n\n" + "\n".join(urgent_alerts[:3])
                send_message(alert_text)
                logger.info("[빅피겨] 긴급 발언 알림 발송 %d건", len(urgent_alerts))
            except Exception as e:
                logger.warning("[빅피겨] 텔레그램 알림 실패: %s", e)

        logger.info("[빅피겨팀] 완료")
    except Exception as e:
        logger.error("[빅피겨팀] 실패: %s", e)
        state["bigfigure_report"] = "분석 실패"
        state["errors"].append(f"bigfigure_agent: {e}")
    return state
