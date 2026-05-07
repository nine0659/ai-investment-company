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

_SYSTEM = """당신은 장기 투자(1년 이상 보유) 전문 가치투자 매니저입니다.
제공된 종목들의 재무 데이터와 밸류에이션 지표를 분석하여 장기 투자 추천 종목 3개를 선정하세요.

선정 기준:
1. 내재가치: PBR 기준 저평가 여부 (청산가치 대비 안전마진)
2. 수익성·지속성: ROE 15% 이상 지속, 영업이익 안정성
3. 재무 건전성: 부채비율 낮음, 잉여현금흐름 양호
4. 배당: 배당수익률·배당 성장성
5. 산업 성장성: 향후 3~5년 산업 전망
6. 경쟁 해자(Moat): 브랜드·기술·규모의 경제 등

각 추천 종목 출력 형식:
- 종목명 (코드)
- 현재가 / 적정가치 (1~3년, 상승여력 %)
- 장기 투자 논거 (산업 성장성·경쟁 우위 포함) 4가지
- 배당 전략 (배당수익률, 배당 재투자 수익률 추정)
- 주요 리스크 3가지 및 대응 방안
- 분할 매수 전략 (가격대별 비중)

마지막에 장기 투자 관점의 거시경제 전망과 포트폴리오 구성 제안을 포함하세요."""


def run_analysis():
    """장기 분석 실행 및 텔레그램 발송"""
    now = datetime.now(TZ)
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
        send_error_alert("장기 분석 실패: 종목 데이터 수집 불가")
        return

    stock_text = "\n\n".join(
        format_for_prompt(d) for d in stock_data_list if d.get("price")
    )
    context = (
        f"분석 기준일: {now.strftime('%Y년 %m월 %d일')}\n\n"
        f"=== 분석 대상 종목 ({len(stock_data_list)}개) ===\n\n"
        f"{stock_text}"
    )

    try:
        report = chat(_SYSTEM, context, max_tokens=4000)
    except Exception as e:
        logger.error("[장기에이전트] OpenAI 호출 실패: %s", e)
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
