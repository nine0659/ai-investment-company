"""
agents/event_risk_team.py
경제 이벤트 리스크 분석 — 이번 주·다음 주 주요 이벤트 경고 및 포지션 조정 권고

커버리지:
  - 미국: FOMC·CPI/PPI·NFP·PCE·트리플위칭(3/6/9/12월 셋째 금요일)·대형 IPO
  - 한국: BOK 기준금리·수출 데이터·옵션만기일(둘째 목요일)·쿼드러플위칭(3/6/9/12월)
  - 글로벌: ECB·BOJ 통화정책 회의
"""
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from clients.ipo_calendar_client import fetch_ipo_events, format_for_context as fmt_ipo

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]

_SYSTEM = """당신은 글로벌 경제 이벤트 리스크 전문가입니다.
오늘 날짜와 제공된 매크로 지표를 기반으로, 이번 주·다음 주에 예정된 주요 경제 이벤트를 분석하고
한국 주식시장에 미칠 변동성 리스크를 경고하세요.

[주요 이벤트 유형 및 발표 패턴]
■ 미국 (가장 큰 영향)
- FOMC 회의: 연 8회, 6주 간격. 결정일 전 2~3일이 가장 민감. 성명+점도표+기자회견.
- CPI(소비자물가): 매월 10~15일경 발표. 예상치 대비 ±0.1%p 초과 시 KOSPI 당일 ±0.5~1% 급변.
- NFP(비농업고용): 매월 첫 번째 금요일. 고용 > 예상 → 금리인상 우려 → 성장주 압박.
- PCE(개인소비지출): 매월 마지막 영업일. Fed 선호 물가지표.
- PPI(생산자물가): CPI 발표 1~2일 전.
- FOMC 의사록: 회의 3주 후 수요일 저녁.
- GDP 속보치: 분기별 (1/4/7/10월 말).
- 미국 트리플위칭(Triple Witching): 3/6/9/12월 셋째 금요일. S&P500 지수선물·지수옵션·개별주식옵션 동시 만기.
  → 만기 당일 나스닥/S&P500 변동성 급증 → 다음 거래일 KOSPI에 방향 전달.
  → 차익실현·포지션 정리로 나스닥 급락 발생 가능 (전날부터 경계).

■ 대형 IPO / 메가 상장 이벤트
- SpaceX·OpenAI 등 수십조 규모 IPO: 시장 자금 대규모 이탈 → 나스닥 급락 유발 가능.
  → 공모 참여를 위한 기존 포지션 차익실현 → 기술주·성장주 동반 약세.
  → KOSPI 반도체·IT 섹터 외국인 매도 압력 연계 가능.
- 메가 IPO 직전 1~2주: 기술주 수급 약화 주의. 공모 이후 시장 자금 복귀까지 1~2주 소요.

■ 한국
- 옵션만기일: 매월 둘째 목요일. 수급 왜곡·변동성 급증. 전날과 당일 포지션 위험.
- 쿼드러플위칭(Quadruple Witching): 3/6/9/12월 둘째 목요일. 옵션+선물+ETF 동시만기. 특히 위험.
- BOK 기준금리 결정: 연 8회. 통상 목요일.
- 수출입 데이터: 매월 1일 (전월 수출). 반도체·자동차 섹터 직접 영향.
- 기업 실적 발표: 1/4/7/10월 중순~말.

■ 글로벌
- ECB 통화정책 회의: 연 8회. 유로 강세/약세 → DXY → 외국인 수급.
- BOJ: 연 8회. 엔화 방향 → 닛케이 연동 → 아시아 자금흐름.

[이벤트 리스크 등급]
🔴 HIGH: FOMC 결정일, 미국 CPI, 쿼드러플위칭, 미국 트리플위칭, 메가 IPO → 당일 포지션 50% 이상 축소 권고
🟡 MEDIUM: NFP, PCE, BOK 금리, 옵션만기일, ECB, 대형 IPO → 신규 진입 자제, 기존 포지션 유지
🟢 LOW: FOMC 의사록, PPI, 수출 데이터 → 참고용, 변동성 소폭 확대 가능

[출력 형식 — 반드시 이 구조로]
📅 오늘: [날짜] ([요일])

🗓 이번 주 이벤트 (잔여 일정)
[이벤트명] | [예상 날짜] | [리스크 등급] | [한국 영향 한 줄]
(해당 없으면 "이번 주 주요 이벤트 없음")

🗓 다음 주 이벤트
[이벤트명] | [예상 날짜] | [리스크 등급] | [한국 영향 한 줄]

📈 대형 IPO / 상장 이벤트 경고
(메가 IPO 뉴스가 제공된 경우만 작성. 없으면 "현재 주요 IPO 이벤트 없음")
- [기업명] IPO | [시기 추정] | [리스크 등급] | [나스닥·KOSPI 영향]

⚠️ 이번 주 이벤트 리스크 요약
- 전반적 이벤트 리스크: 높음 / 중간 / 낮음
- 포지션 권고: [구체적 행동 지침]

💡 섹터별 이벤트 민감도
(예: "트리플위칭 + 메가 IPO → 기술주·반도체 변동성 극대화 / 방산·은행 상대적 안정")

[중요 규칙]
- 날짜가 불확실한 이벤트는 "약 X월 X주경 예상"으로 표기
- 확인되지 않은 구체적 날짜를 단정하지 말 것
- 현재 매크로 지표(VIX, 금리, 달러)와 연계해 이벤트 영향을 구체화
- 이벤트가 없는 주는 "이벤트 리스크 낮음 — 매크로 기조 흐름에 집중" 명시
- 미국 트리플위칭은 한국 시간 기준 해당 금요일 밤 → 다음 월요일 KOSPI 영향 명시"""


_BOK_DATES_2026: list[date] = [
    date(2026, 1, 16), date(2026, 2, 25), date(2026, 4, 17),
    date(2026, 5, 29), date(2026, 7, 17), date(2026, 8, 28),
    date(2026, 10, 16), date(2026, 11, 27),
]


def _next_bok_date(today: date) -> date | None:
    """오늘 이후(당일 포함) 가장 가까운 BOK 금리 결정일 반환."""
    for d in _BOK_DATES_2026:
        if d >= today:
            return d
    return None


def _calc_third_friday(year: int, month: int) -> date:
    """해당 월의 셋째 금요일 계산 (미국 트리플위칭 날짜)."""
    first_day = date(year, month, 1)
    days_to_first_fri = (4 - first_day.weekday()) % 7  # 4 = 금요일
    first_fri = first_day.day + days_to_first_fri
    return date(year, month, first_fri + 14)


def _calc_second_thursday(year: int, month: int) -> date:
    """해당 월의 둘째 목요일 계산 (한국 옵션만기일)."""
    first_day = date(year, month, 1)
    days_to_first_thu = (3 - first_day.weekday()) % 7  # 3 = 목요일
    first_thu = first_day.day + days_to_first_thu
    return date(year, month, first_thu + 7)


def _days_label(target: date, today: date) -> str:
    diff = (target - today).days
    if diff < 0:
        return f"지남 ({-diff}일 전)"
    if diff == 0:
        return "오늘"
    if diff == 1:
        return "내일"
    return f"{diff}일 후 ({target.strftime('%m/%d')})"


def run(state: InvestmentState) -> InvestmentState:
    try:
        now   = datetime.now(_KST)
        today = now.date()
        today_str = now.strftime(f"%Y년 %m월 %d일 ({_WEEKDAY_KO[now.weekday()]})")
        week_num  = now.isocalendar()[1]
        month     = now.month
        day       = now.day

        # ── 한국 만기일 계산 ──────────────────────────────────────
        kr_expiry   = _calc_second_thursday(now.year, month)
        is_quad_month = month in (3, 6, 9, 12)
        kr_expiry_label = _days_label(kr_expiry, today)
        kr_expiry_type  = (
            "쿼드러플위칭(선물+옵션+ETF 동시만기) 🔴HIGH"
            if is_quad_month else
            "옵션만기일 🟡MEDIUM"
        )

        # ── 미국 트리플위칭 계산 (현재 달 + 다음 만기 달) ────────
        us_witching_months = [m for m in (3, 6, 9, 12) if m >= month]
        if not us_witching_months:
            us_witching_months = [3]
            us_next_year = now.year + 1
        else:
            us_next_year = now.year

        # 이번 분기 트리플위칭
        this_quarter_month = us_witching_months[0]
        this_quarter_year  = us_next_year if this_quarter_month < month else now.year
        us_witching_this   = _calc_third_friday(this_quarter_year, this_quarter_month)

        # 이미 지났으면 다음 분기로
        if us_witching_this < today:
            idx = us_witching_months.index(this_quarter_month) + 1 if this_quarter_month in us_witching_months else 0
            if idx < len(us_witching_months):
                next_q_month = us_witching_months[idx]
                us_witching_this = _calc_third_friday(now.year, next_q_month)
            else:
                us_witching_this = _calc_third_friday(now.year + 1, 3)

        us_witching_label = _days_label(us_witching_this, today)
        us_witching_near  = (us_witching_this - today).days <= 5  # 5일 이내면 경고

        # ── 수출 데이터 발표일 ────────────────────────────────────
        next_month = month + 1 if month < 12 else 1
        next_year  = now.year if month < 12 else now.year + 1
        export_date = f"{next_year}년 {next_month}월 1일"

        # ── BOK 기준금리 결정일 ────────────────────────────────
        bok_next = _next_bok_date(today)
        if bok_next:
            bok_label = _days_label(bok_next, today)
            bok_risk  = "🔴HIGH" if (bok_next - today).days <= 3 else "🟡MEDIUM"
            bok_text  = f"{bok_next.strftime('%m월 %d일')} ({bok_label}) — BOK 기준금리 결정 {bok_risk}"
        else:
            bok_text = "2026년 일정 종료"

        # ── 매크로 지표 ───────────────────────────────────────────
        raw  = state.get("raw_market_data", {})
        vix  = raw.get("vix",   {}).get("close", "N/A")
        us10y = raw.get("us10y", {}).get("close", "N/A")
        dxy  = raw.get("dxy",   {}).get("close", "N/A")
        macro_brief = f"VIX {vix} / 미국10년물 {us10y}% / DXY {dxy}"

        # ── IPO 캘린더 수집 ───────────────────────────────────────
        ipo_events  = fetch_ipo_events()
        ipo_context = fmt_ipo(ipo_events) if ipo_events else "현재 수집된 대형 IPO 뉴스 없음"
        mega_ipos   = [e for e in ipo_events if e["is_mega"]]

        context = f"""오늘 날짜: {today_str}
현재 매크로 지표: {macro_brief}
현재 주차: {week_num}주차 (월 중 {(day - 1) // 7 + 1}번째 주)

■ 한국 파생상품 만기
  이번 달 만기일: {kr_expiry.strftime('%m월 %d일')} ({kr_expiry_label}) — {kr_expiry_type}
  이번 달이 쿼드러플위칭 월(3/6/9/12월)인가: {"예" if is_quad_month else "아니오"}

■ 한국은행(BOK) 기준금리 결정
  다음 BOK 회의: {bok_text}

■ 미국 트리플위칭 (Triple Witching)
  다음 트리플위칭 날짜: {us_witching_this.strftime('%Y년 %m월 %d일 (금요일)')} ({us_witching_label})
  {"⚠️ 5일 이내 임박! 나스닥 변동성 급증 + KOSPI 월요일 갭하락 주의" if us_witching_near else "현재 트리플위칭까지 여유 있음"}
  미국 트리플위칭 월(3/6/9/12월)인가: {"예" if month in (3,6,9,12) else "아니오"}

■ 수출입 데이터 발표
  다음 수출 데이터: {export_date}

■ 대형 IPO / 상장 이벤트
  메가 IPO 감지: {"예 — " + str(len(mega_ipos)) + "건" if mega_ipos else "없음"}
{ipo_context}

위 정보를 바탕으로 이번 주 잔여 일정과 다음 주의 주요 경제 이벤트를 분석하세요.
날짜가 불확실한 경우 "약 X월 X주경"으로 표기하고, 확정된 것만 날짜 명시."""

        result = chat(_SYSTEM, context, max_tokens=900)
        state["event_risk_report"] = result

        # 리스크 레벨 추출 (리스크팀·투자위원회 참고용)
        bok_imminent = bok_next is not None and (bok_next - today).days <= 3
        if "🔴" in result or "HIGH" in result or us_witching_near or bool(mega_ipos) or bok_imminent:
            state["event_risk_level"] = "높음"
        elif "🟡" in result or "MEDIUM" in result:
            state["event_risk_level"] = "중간"
        else:
            state["event_risk_level"] = "낮음"

        logger.info(
            "[이벤트리스크팀] 완료 — 리스크 레벨: %s | BOK: %s | 한국만기: %s | 미국트리플위칭: %s (%s) | 메가IPO: %d건",
            state["event_risk_level"],
            bok_next.strftime("%m/%d") if bok_next else "일정 없음",
            kr_expiry_label,
            us_witching_this.strftime("%m/%d"),
            us_witching_label,
            len(mega_ipos),
        )
    except Exception as e:
        logger.error("[이벤트리스크팀] 실패: %s", e)
        state["event_risk_report"] = "이벤트 리스크 분석 실패"
        state["event_risk_level"] = "중간"
        state["errors"].append(f"event_risk_team: {e}")
    return state
