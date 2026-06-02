"""
글로벌 빅피겨 발언/행보 분석 → 시장 영향 평가 + 즉시 알림
긴급 알림은 장전(pre_market) 실행 시에만 1회 발송 — 하루 중복 방지
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from clients.telegram_client import send_message
from config.settings import RUN_TYPE_PRE
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")


def _already_sent_today() -> bool:
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM bigfigure_alert_log WHERE date=:date"),
                {"date": today},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_sent_today():
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            conn.execute(
                text("INSERT INTO bigfigure_alert_log (date) VALUES (:date) ON CONFLICT (date) DO NOTHING"),
                {"date": today},
            )
    except Exception:
        pass


def _ensure_alert_table():
    try:
        with get_conn() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS bigfigure_alert_log "
                "(date TEXT PRIMARY KEY)"
            ))
    except Exception as e:
        logger.debug("[빅피겨] 테이블 생성 실패: %s", e)


# AI 거부 응답 감지 (첫 100자 기준)
_REFUSAL_PHRASES = [
    "I'm sorry", "I cannot assist", "I can't assist",
    "Sorry, but I", "I apologize, but", "죄송합니다만",
]

_SYSTEM = """당신은 글로벌 빅피겨 발언 분석 전문가입니다.
수집된 뉴스를 분석하여 오늘 한국 주식시장에 미칠 영향을 평가하세요.

각 인물에 대해 (뉴스가 있는 경우만):
1. 핵심 발언/행보 요약 (1~2줄)
2. 시장 영향: 긍정/부정/중립 + 이유
3. 영향받을 한국 섹터·종목 (구체적으로, 없으면 생략)
4. 단기(당일) vs 중기(1주) 영향 구분
5. 중요도: 상/중/하

⚡ 표시(중요도 '상') 기준 — 아래 조건을 **모두** 충족해야 부여:
  조건 A (내용): 아래 중 하나에 해당
    - 금리·통화정책 깜짝 변경 또는 강한 시그널 (연준·ECB·BOJ·BOK)
    - 관세·반도체 수출규제 신규 발표 또는 구체적 변경 (검토·예정 수준은 제외)
    - AI·반도체 업황에 직결되는 공식 계약/신제품/파트너십 (컨퍼런스 예고는 제외)
    - 전기차·배터리 구체적 수주금액/공장 투자 확정 발표
    - 시장에 즉각적 충격을 줄 수준의 발언 (예: "침체 임박", "현금 비중 역대 최고")
  조건 B (한국 영향): 오늘 또는 내일 한국 특정 섹터/종목에 직접적 가격 영향 예상

⚠️ ⚡ 절대 부여 금지 사례 (이런 경우는 반드시 '중/하'):
  - 정기 컨퍼런스·포럼에서의 일반적 발언
  - "AI가 중요하다", "경제를 주시하겠다" 등 원론적 언급
  - 이미 시장에 반영된 과거 정책 재확인
  - 직책 취임·이임·방문 등 행정적 일정
  - 한국 시장과 직접 연관이 없는 발언

⚡ 표시 행은 한 줄에 하나, 반드시 구체적인 내용과 한국 종목/섹터를 포함해야 합니다.
뉴스가 평범하거나 영향이 미미하면 "중/하"로 간결하게 처리하고 ⚡ 사용 금지.
출력 헤더: "[오늘 주목할 빅피겨 발언]" """


def _is_refusal(text: str) -> bool:
    """OpenAI 거부 응답 여부 판단 — 첫 100자만 검사."""
    snippet = text.strip()[:100].lower()
    return not snippet or any(p.lower() in snippet for p in _REFUSAL_PHRASES)


_URGENT_KEYWORDS = [
    "금리", "인상", "인하", "관세", "수출규제", "수주", "계약", "공장",
    "침체", "현금", "시장충격", "파산", "파탄", "폭락", "급락",
    "반도체", "HBM", "AI칩", "배터리", "전기차", "수혜",
]

_GENERIC_PHRASES = [
    "중요하다", "주시하겠다", "원론", "긍정적으로", "면밀히", "살펴보겠다",
    "지속적으로", "노력하겠다", "협력하겠다", "검토 중", "관심", "기대한다",
]


def _extract_urgent_lines(ai_response: str) -> list[str]:
    """AI가 ⚡로 표시한 줄 중 실제로 시장에 영향을 미칠 내용만 추출.

    필터 기준:
    - 40자 이상 (짧은 헤더·기호 제외)
    - 한국 시장 관련 키워드 1개 이상 포함
    - 일반론적 문구 포함 시 제외
    """
    lines = []
    for line in ai_response.split("\n"):
        line = line.strip()
        if not (line.startswith("⚡") and len(line) > 40):
            continue
        if any(phrase in line for phrase in _GENERIC_PHRASES):
            continue
        if not any(kw in line for kw in _URGENT_KEYWORDS):
            continue
        lines.append(line)
    return lines[:2]  # 최대 2건 (이전 3건 → 더 엄격하게 축소)


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

        # 긴급 알림: 장전(pre_market) 실행 시, 오늘 첫 발송에 한해만 전송
        run_type = state.get("run_type", "")
        if run_type != RUN_TYPE_PRE:
            logger.info("[빅피겨] 장전 실행 아님 (%s) — 긴급 알림 스킵", run_type)
            return state

        _ensure_alert_table()
        if _already_sent_today():
            logger.info("[빅피겨] 오늘 긴급 알림 이미 발송 완료 — 중복 스킵")
            return state

        urgent_lines = _extract_urgent_lines(result)
        if urgent_lines:
            try:
                alert_text = (
                    "⚡ 빅피겨 긴급 발언 알림 ⚡\n"
                    "(전체 내용은 장전 브리핑에 포함)\n\n"
                    + "\n".join(urgent_lines)
                )
                send_message(alert_text)
                _mark_sent_today()
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
