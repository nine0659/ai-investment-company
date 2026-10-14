"""입력 데이터 신선도 감사 — '이 수치의 기준 날짜가 직전 거래일과 같은가'.

2026-10-14: KOSPI 일봉이 하루 늦은(10/12) 채로 '전일'로 쓰였다. 기존 check_data_freshness는
3영업일 이상만 잡았다. 여기서는 자산군별 '직전 완결 거래일'을 계산해 1일이라도 뒤처지면 표시한다.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
_NY = ZoneInfo("America/New_York")

KR_KEYS = ("kospi", "kosdaq")
US_KEYS = ("sp500", "nasdaq", "dow", "sox", "nvda", "tsmc", "vix", "us10y")


def _is_us_open_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        import holidays
        return d not in holidays.financial_holidays("NYSE", years=d.year)
    except Exception:
        return True


def last_completed_kr_day(now: datetime | None = None) -> date:
    from utils.market_calendar import is_krx_trading_day
    now = now or datetime.now(_KST)
    d = now.date()
    if now.hour * 60 + now.minute < 15 * 60 + 45 or not is_krx_trading_day(d):
        d -= timedelta(days=1)
    while not is_krx_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_completed_us_day(now: datetime | None = None) -> date:
    ny = (now or datetime.now(_KST)).astimezone(_NY)
    d = ny.date()
    if ny.hour < 16 or not _is_us_open_day(d):
        d -= timedelta(days=1)
    while not _is_us_open_day(d):
        d -= timedelta(days=1)
    return d


def audit(raw: dict, now: datetime | None = None) -> list[str]:
    """뒤처진 지표 목록(사람이 읽는 문자열). 모두 정상이면 []."""
    out = []
    kr_exp, us_exp = last_completed_kr_day(now), last_completed_us_day(now)
    for keys, exp, label in ((KR_KEYS, kr_exp, "한국"), (US_KEYS, us_exp, "미국")):
        for k in keys:
            dd = ((raw or {}).get(k) or {}).get("data_date")
            if not dd:
                continue
            try:
                got = date.fromisoformat(dd)
            except ValueError:
                continue
            if got < exp:
                out.append(f"{k}: 기준일 {dd} < 직전 {label} 거래일 {exp.isoformat()} (수치가 하루 이상 늦음)")
    return out
