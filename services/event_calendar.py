"""다가오는 이벤트 캘린더 — 변동성이 '언제' 오는지 미리 알린다.

고정 일정은 코드가 계산한다: FOMC(2026 연준 공표 일정 하드코딩), 한국 선물옵션 만기일(매월 둘째 목요일,
휴장이면 직전 거래일), 미국 트리플위칭(3·6·9·12월 셋째 금요일), 보유 종목 실적발표(yfinance, 있을 때만).
확실하지 않은 일정(한은 금통위, CPI 등)은 만들지 않는다 — 이벤트 리스크팀 리포트가 담당.
"""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 2026 FOMC 결정일(회의 둘째 날). 연준 공표 일정 기준.
FOMC_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
             date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9)]


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def kr_option_expiry(year: int, month: int) -> date:
    from utils.market_calendar import is_krx_trading_day
    d = nth_weekday(year, month, 3, 2)   # 둘째 목요일
    while not is_krx_trading_day(d):
        d -= timedelta(days=1)
    return d


def upcoming(today: date | None = None, days: int = 10, earnings: dict[str, date] | None = None) -> list[dict]:
    """오늘~days일 내 이벤트. [{date, d_day, title, impact}] 날짜순."""
    today = today or datetime.now(_KST).date()
    end = today + timedelta(days=days)
    evs = []
    for d in FOMC_2026:
        evs.append({"date": d, "title": "미국 FOMC 금리 결정(한국시간 다음날 새벽)", "impact": "HIGH"})
    for off in (0, 1):
        m = today.month + off
        y = today.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        evs.append({"date": kr_option_expiry(y, m), "title": "한국 선물옵션 만기일", "impact": "MEDIUM"})
        if m in (3, 6, 9, 12):
            evs.append({"date": nth_weekday(y, m, 4, 3), "title": "미국 트리플위칭(선물·옵션 동시 만기)", "impact": "MEDIUM"})
            evs.append({"date": kr_option_expiry(y, m), "title": "한국 쿼드러플위칭(분기 만기)", "impact": "HIGH"})
    for name, d in (earnings or {}).items():
        evs.append({"date": d, "title": f"{name} 실적 발표", "impact": "HIGH"})
    out = [dict(e, d_day=(e["date"] - today).days) for e in evs if today <= e["date"] <= end]
    seen, uniq = set(), []
    for e in sorted(out, key=lambda x: (x["date"], x["title"])):
        k = (e["date"], e["title"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def fetch_earnings(holdings: list[dict]) -> dict[str, date]:
    """보유 종목 다음 실적 발표일(yfinance calendar). 못 구하면 빠짐."""
    out = {}
    try:
        import yfinance as yf
        for h in holdings:
            for suffix in (".KS", ".KQ"):
                try:
                    cal = yf.Ticker(f"{h['code']}{suffix}").calendar
                    ed = (cal or {}).get("Earnings Date") if isinstance(cal, dict) else None
                    if ed:
                        d0 = ed[0] if isinstance(ed, (list, tuple)) else ed
                        out[h["name"]] = d0 if isinstance(d0, date) else date.fromisoformat(str(d0)[:10])
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.debug("[이벤트] 실적일 조회 실패: %s", e)
    return out


def format_upcoming(evs: list[dict]) -> str:
    if not evs:
        return ""
    lines = ["[앞으로 10일 일정 — 코드 계산, 이 외 일정은 지어내지 말 것]"]
    for e in evs:
        when = "오늘" if e["d_day"] == 0 else f"D-{e['d_day']}"
        lines.append(f"  {e['date'].strftime('%m/%d')}({when}) {e['title']} [{e['impact']}]")
    return "\n".join(lines)
