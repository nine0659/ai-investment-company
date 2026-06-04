"""
utils/market_calendar.py
KRX 거래일 판단 유틸리티

- 토/일 → 비거래일
- 한국 공휴일(holidays 패키지) → 비거래일
- 나머지 평일 → 거래일

임시 휴장(선거일 포함): holidays 패키지가 대통령선거·국회의원선거 등 법정 공휴일을
커버하므로 대부분 자동 감지됨. KRX 자체 임시휴장은 별도 처리 불가.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def is_krx_trading_day(check_date: date | None = None) -> bool:
    """주어진 날짜(기본: 오늘 KST)가 KRX 거래일인지 반환."""
    if check_date is None:
        check_date = datetime.now(_KST).date()

    if check_date.weekday() >= 5:  # 토(5)·일(6)
        return False

    try:
        import holidays
        kr_holidays = holidays.KR(years=check_date.year)
        if check_date in kr_holidays:
            return False
    except ImportError:
        pass  # 패키지 미설치 시 weekday만으로 판단 (경고는 호출부에서)

    return True


def get_holiday_name(check_date: date | None = None) -> str:
    """비거래일이면 이름(예: '현충일', '토요일'), 거래일이면 빈 문자열."""
    if check_date is None:
        check_date = datetime.now(_KST).date()

    if check_date.weekday() == 5:
        return "토요일"
    if check_date.weekday() == 6:
        return "일요일"

    try:
        import holidays
        kr_holidays = holidays.KR(years=check_date.year)
        return kr_holidays.get(check_date, "")
    except ImportError:
        return ""
