"""
중기 투자 분석 에이전트 (1~6개월 보유 관점)
매주 일요일 20:00 KST 실행
분석 대상: KOSPI 시가총액 상위 30개 종목
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.kis_client import KISClient
from clients.telegram_client import send_message, send_error_alert
from services.valuation_service import get_stock_valuation, format_for_prompt
from services.review_service import save_midterm_report

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Seoul")

# KOSPI 시가총액 상위 30개 종목 (2026 기준)
KOSPI_TOP30 = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("207940", "삼성바이오로직스"),
    ("373220", "LG에너지솔루션"),
    ("005380", "현대차"),
    ("000270", "기아"),
    ("005490", "POSCO홀딩스"),
    ("068270", "셀트리온"),
    ("035420", "NAVER"),
    ("028260", "삼성물산"),
    ("105560", "KB금융"),
    ("055550", "신한지주"),
    ("006400", "삼성SDI"),
    ("051910", "LG화학"),
    ("086790", "하나금융지주"),
    ("035720", "카카오"),
    ("012330", "현대모비스"),
    ("066570", "LG전자"),
    ("017670", "SK텔레콤"),
    ("034730", "SK"),
    ("032830", "삼성생명"),
    ("012450", "한화에어로스페이스"),
    ("033780", "KT&G"),
    ("316140", "우리금융지주"),
    ("034020", "두산에너빌리티"),
    ("096770", "SK이노베이션"),
    ("003670", "포스코퓨처엠"),
    ("247540", "에코프로비엠"),
    ("000810", "삼성화재"),
    ("003550", "LG"),
]

_SYSTEM = """당신은 중기 투자(1~6개월 보유) 전문 포트폴리오 매니저입니다.
제공된 KOSPI 시총 상위 30개 종목의 재무 데이터와 밸류에이션 지표를 분석하여
중기 투자 추천 종목 5개를 선정하세요.

N/A 항목은 해당 데이터가 없는 것이므로 가용한 데이터로 판단하세요.

선정 기준:
1. 밸류에이션: PER·PBR이 업종 평균 대비 합리적인 수준
2. 성장성: 매출·영업이익 증가 추세
3. 수익성: ROE 10% 이상, 영업이익률 개선
4. 재무 안정성: 부채비율 200% 이하
5. 모멘텀: 52주 저점 대비 상승 여력

각 추천 종목 출력 형식:
- 종목명 (코드)
- 현재가 / 목표가 (3~6개월, 상승여력 %)
- 매수 근거 3가지 (재무 데이터 수치 인용)
- 핵심 리스크 2가지
- 매수 전략 (분할매수 여부, 비중 제안)

마지막에 전체 시장 환경과 중기 투자 전략 총평을 작성하세요."""


def run_analysis():
    """중기 분석 실행 및 텔레그램 발송"""
    now = datetime.now(TZ)
    logger.info("[중기에이전트] 분석 시작: %s", now.strftime("%Y-%m-%d %H:%M"))

    kis = KISClient()
    stock_data_list = []
    fail_count = 0

    for code, name in KOSPI_TOP30:
        try:
            data = get_stock_valuation(kis, code, name, years=3)
            stock_data_list.append(data)
            has_dart = bool(data.get("financials"))
            has_kis  = bool(data.get("price"))
            logger.info("  수집 완료: %s (%s) | DART=%s KIS=%s",
                        name, code, "O" if has_dart else "X", "O" if has_kis else "X")
        except Exception as e:
            logger.warning("  수집 실패 (%s): %s", name, e)
            fail_count += 1

    # DART 재무 데이터가 하나라도 있으면 분석 진행
    valid = [d for d in stock_data_list if d.get("financials") or d.get("price")]
    if not valid:
        send_error_alert("중기 분석 실패: 유효한 종목 데이터 없음")
        return

    logger.info("[중기에이전트] 유효 종목 %d개 / 전체 %d개", len(valid), len(KOSPI_TOP30))

    stock_text = "\n\n".join(format_for_prompt(d) for d in valid)
    context = (
        f"분석 기준일: {now.strftime('%Y년 %m월 %d일')}\n"
        f"분석 대상: KOSPI 시가총액 상위 30개 종목 중 데이터 수집 완료 {len(valid)}개\n\n"
        f"=== 종목별 재무 데이터 ===\n\n"
        f"{stock_text}"
    )

    try:
        report = chat(_SYSTEM, context, max_tokens=4000)
    except Exception as e:
        logger.error("[중기에이전트] OpenAI 호출 실패: %s", e)
        send_error_alert(f"중기 분석 OpenAI 오류: {e}")
        return

    header = (
        f"📊 *AI 중기 투자 분석* ({now.strftime('%Y.%m.%d')})\n"
        f"KOSPI 시총 상위 30개 종목 분석 | 1~6개월 보유 관점\n\n"
    )
    send_message(header + report)

    try:
        save_midterm_report(now.strftime("%Y-%m-%d"), report)
    except Exception as e:
        logger.warning("[중기에이전트] DB 저장 실패: %s", e)

    logger.info("[중기에이전트] 완료")


def run(state: dict) -> dict:
    run_analysis()
    return state
