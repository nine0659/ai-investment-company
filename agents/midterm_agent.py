"""
중기 투자 분석 에이전트 (1~6개월 보유 관점)
매주 일요일 20:00 KST 실행
분석 대상: KOSPI 시가총액 상위 30개 종목
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat_ceo
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

_SYSTEM = """당신은 1~6개월 보유 관점의 투자 자문가입니다.
바쁜 개인 투자자가 30초 안에 읽을 수 있도록, 어려운 전문용어 없이 짧게 씁니다.

제공된 KOSPI 시총 상위 종목 데이터에서 중기 추천 종목 3개만 선정하세요.

원칙:
- 수치는 제공된 데이터에 있는 것만 인용하세요. 없는 수치는 절대 만들지 마세요.
- N/A 항목은 무시하고 가용한 데이터로 판단하세요.
- [직전 추천]과 같은 종목을 다시 추천하면 종목명 옆에 "(지난 추천 유지)"만 붙이고,
  근거를 다시 쓰지 마세요. 지난번과 달라진 점이 있을 때만 1줄 추가하세요.
- 선정 기준(내부 판단용, 출력 금지): 밸류에이션 합리성, 연간 매출·이익 성장,
  ROE·영업이익률, 부채비율 200% 이하, 52주 저점 대비 위치.

출력 형식 (전체 25줄 이내, 아래 형식 외 다른 텍스트 금지):

💡 한 줄 결론: (이번 주 중기 투자자가 기억할 것 1문장)

1. 종목명 (코드) — 현재가 X원 → 목표가 Y원 (상승여력 +Z%)
   왜: (쉬운 말로 1줄)
   조심할 것: (1줄)

2. (같은 형식)
3. (같은 형식)

📌 시장 한 줄 평: (1문장)"""


def run_analysis(send: bool = True) -> str | None:
    """중기 분석 실행. send=True면 단독 텔레그램 발송, False면 리포트만 반환
    (일요일 주간 추천 통합 1통에서 사용). 실패·중복 시 None 반환."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    # Render 스케줄러와 GitHub Actions가 같은 날 각각 실행해도 1회만 발송
    from services.report_service import claim_report_slot, release_report_slot
    if not claim_report_slot(today, "midterm"):
        logger.info("[중기에이전트] 오늘 이미 실행됨 — 스킵 (중복 발송 방지)")
        return None

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
        release_report_slot(today, "midterm")
        send_error_alert("중기 분석 실패: 유효한 종목 데이터 없음")
        return None

    logger.info("[중기에이전트] 유효 종목 %d개 / 전체 %d개", len(valid), len(KOSPI_TOP30))

    # 이상치가 광범위하면 데이터 소스 장애 가능성 — 관리자 경보
    from services.data_guard import alert_if_widespread
    alert_if_widespread(
        [w for d in stock_data_list for w in d.get("_data_warnings", [])],
        "중기분석(KIS/DART)",
    )

    # 직전 추천 리포트 — 같은 종목 반복 서술 방지용
    prev_section = ""
    try:
        from services.review_service import get_last_midterm_report
        prev = get_last_midterm_report(today)
        if prev:
            prev_section = (
                f"\n\n=== 직전 추천 ({prev['date']}) ===\n{prev['report'][:900]}"
            )
    except Exception as e:
        logger.debug("[중기에이전트] 직전 리포트 조회 실패: %s", e)

    stock_text = "\n\n".join(format_for_prompt(d) for d in valid)
    context = (
        f"분석 기준일: {now.strftime('%Y년 %m월 %d일')}\n"
        f"분석 대상: KOSPI 시가총액 상위 30개 종목 중 데이터 수집 완료 {len(valid)}개\n\n"
        f"=== 종목별 재무 데이터 ===\n\n"
        f"{stock_text}"
        f"{prev_section}"
    )

    try:
        # 주간 1회 호출 — 사용자 의사결정에 직결되는 추천이므로 CEO급 모델 사용
        report = chat_ceo(_SYSTEM, context, max_tokens=1200)
    except Exception as e:
        logger.error("[중기에이전트] OpenAI 호출 실패: %s", e)
        release_report_slot(today, "midterm")
        send_error_alert(f"중기 분석 OpenAI 오류: {e}")
        return None

    if send:
        header = (
            f"📊 *AI 중기 투자 분석* ({now.strftime('%Y.%m.%d')})\n"
            f"KOSPI 시총 상위 30개 종목 분석 | 1~6개월 보유 관점\n\n"
        )
        send_message(header + report)

    try:
        save_midterm_report(now.strftime("%Y-%m-%d"), report)
    except Exception as e:
        logger.warning("[중기에이전트] DB 저장 실패: %s", e)

    # 추천 → 추적 루프 연결: 파싱·교차검증 후 stock_recommendations에 저장.
    # 이 저장이 있어야 성과추적기가 주간 추천을 따라가고, 적중률·귀인분석이
    # '데이터 없음'이 아닌 실측 기반으로 동작한다 (2026-07-06 루프 복원).
    try:
        from services.recommendation_service import (
            recs_from_weekly_picks, has_open_recommendation, save_recommendations,
        )
        price_lookup = {d["code"]: d["price"] for d in valid if d.get("price")}
        recs = recs_from_weekly_picks(report, price_lookup)
        recs = [r for r in recs if not has_open_recommendation(r["code"])]
        if recs:
            save_recommendations(today, recs)
            logger.info("[중기에이전트] 추천 %d건 추적 등록: %s",
                        len(recs), ", ".join(r["name"] for r in recs))
    except Exception as e:
        logger.warning("[중기에이전트] 추천 추적 등록 실패 (브리핑은 정상 발송): %s", e)

    logger.info("[중기에이전트] 완료")
    return report


def run(state: dict) -> dict:
    run_analysis()
    return state
