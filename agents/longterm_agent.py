"""
장기 투자 분석 에이전트 (1년 이상 보유 관점)
매월 첫째 주 일요일 20:30 KST 실행
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.kis_client import KISClient
from clients.telegram_client import send_message, send_error_alert
from services.valuation_service import get_stock_valuation, format_for_prompt
from services.review_service import save_longterm_report

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Seoul")

# 장기 분석 대상 종목 (퀄리티·가치·배당 중심)
WATCHLIST = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("005380", "현대차"),
    ("000270", "기아"),
    ("105560", "KB금융"),
    ("055550", "신한지주"),
    ("086790", "하나금융지주"),
    ("000810", "삼성화재"),
    ("005490", "POSCO홀딩스"),
    ("033780", "KT&G"),
    ("028260", "삼성물산"),
    ("003550", "LG"),
    ("017670", "SK텔레콤"),
    ("032830", "삼성생명"),
    ("012450", "한화에어로스페이스"),
]

_SYSTEM = """당신은 1년 이상 보유 관점의 가치투자 자문가입니다.
바쁜 개인 투자자가 30초 안에 읽을 수 있도록, 어려운 전문용어 없이 짧게 씁니다.

제공된 종목 데이터에서 장기 투자 추천 종목 3개만 선정하세요.

원칙:
- 수치는 제공된 데이터에 있는 것만 인용하세요. 없는 수치는 절대 만들지 마세요.
  (특히 배당수익률이 N/A면 배당 이야기를 아예 하지 마세요)
- [직전 추천]과 같은 종목을 다시 추천하면 종목명 옆에 "(지난 추천 유지)"만 붙이고,
  근거를 다시 쓰지 마세요. 지난번과 달라진 점이 있을 때만 1줄 추가하세요.
- 선정 기준(내부 판단용, 출력 금지): 저평가 여부, ROE 지속성, 재무 건전성,
  배당, 3~5년 산업 전망, 경쟁 우위.

출력 형식 (전체 25줄 이내, 아래 형식 외 다른 텍스트 금지):

💡 한 줄 결론: (장기 투자자가 기억할 것 1문장)

1. 종목명 (코드) — 현재가 X원 → 1~3년 적정가치 Y원 (+Z%)
   왜 오래 들고 갈 만한가: (쉬운 말로 1줄)
   조심할 것: (1줄)
   사는 법: (예: "X원 이하로 내려올 때마다 나눠서 매수" 1줄)

2. (같은 형식)
3. (같은 형식)

📌 큰 그림 한 줄: (경제 전망 1문장)"""


def run_analysis():
    """장기 분석 실행 및 텔레그램 발송"""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    # Render 스케줄러와 GitHub Actions가 같은 날 각각 실행해도 1회만 발송
    from services.report_service import claim_report_slot, release_report_slot
    if not claim_report_slot(today, "longterm"):
        logger.info("[장기에이전트] 오늘 이미 실행됨 — 스킵 (중복 발송 방지)")
        return

    logger.info("[장기에이전트] 분석 시작: %s", now.strftime("%Y-%m-%d %H:%M"))

    kis = KISClient()
    stock_data_list = []

    for code, name in WATCHLIST:
        try:
            data = get_stock_valuation(kis, code, name, years=4)
            stock_data_list.append(data)
            logger.info("  데이터 수집: %s (%s)", name, code)
        except Exception as e:
            logger.warning("  수집 실패 (%s): %s", name, e)

    if not stock_data_list:
        release_report_slot(today, "longterm")
        send_error_alert("장기 분석 실패: 종목 데이터 수집 불가")
        return

    # 이상치가 광범위하면 데이터 소스 장애 가능성 — 관리자 경보
    from services.data_guard import alert_if_widespread
    alert_if_widespread(
        [w for d in stock_data_list for w in d.get("_data_warnings", [])],
        "장기분석(KIS/DART)",
    )

    # 직전 추천 리포트 — 같은 종목 반복 서술 방지용
    prev_section = ""
    try:
        from services.review_service import get_last_longterm_report
        prev = get_last_longterm_report(today)
        if prev:
            prev_section = (
                f"\n\n=== 직전 추천 ({prev['date']}) ===\n{prev['report'][:900]}"
            )
    except Exception as e:
        logger.debug("[장기에이전트] 직전 리포트 조회 실패: %s", e)

    stock_text = "\n\n".join(
        format_for_prompt(d) for d in stock_data_list if d.get("price")
    )
    context = (
        f"분석 기준일: {now.strftime('%Y년 %m월 %d일')}\n\n"
        f"=== 분석 대상 종목 ({len(stock_data_list)}개) ===\n\n"
        f"{stock_text}"
        f"{prev_section}"
    )

    try:
        report = chat(_SYSTEM, context, max_tokens=1200)
    except Exception as e:
        logger.error("[장기에이전트] OpenAI 호출 실패: %s", e)
        release_report_slot(today, "longterm")
        send_error_alert(f"장기 분석 OpenAI 오류: {e}")
        return

    header = f"🏦 *AI 장기 투자 분석* ({now.strftime('%Y.%m.%d')})\n1년 이상 보유 관점 추천 종목\n\n"
    send_message(header + report)

    try:
        save_longterm_report(now.strftime("%Y-%m-%d"), report)
    except Exception as e:
        logger.warning("[장기에이전트] DB 저장 실패: %s", e)

    logger.info("[장기에이전트] 완료")


def run(state: dict) -> dict:
    run_analysis()
    return state
