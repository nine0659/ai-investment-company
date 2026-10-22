"""후보 종목별 최근 뉴스 헤드라인 — '왜 지금 이 종목인가'의 맥락. 구글뉴스 RSS에서 제목만 가져와 그대로 보여준다.

코드는 제목을 해석·요약하지 않는다(기사 원문 제목 그대로, 매체·날짜 병기). 사용자에게만 보이는 일일 요약용이며
LLM 컨텍스트에는 넣지 않는다(외부 텍스트가 프롬프트에 섞이는 걸 피한다). 수집 실패는 조용히 비운다 — 요약의 필수 요소가 아니다.
"""
import logging
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)
import re

_CACHE: dict[str, tuple[float, list]] = {}
# 시세·기관 보유 변동을 자동 생성하는 저정보 기사는 거른다(2026-10-22 실측: 한국은 '○○ 주가, 10월 22일 74,000원 2.35% 상승',
# 미국은 MarketBeat '○○ Stock Position Cut by …'가 대부분이었다).
_JUNK_SOURCES = {"TopStarNews", "MarketBeat", "Defense World", "ETF Daily News", "Stock Titan"}
_JUNK_TITLE = re.compile(
    r"주가[ ,].*(상승|하락|보합)|장중 .*원|\d+월 \d+일 .*원 .*%|(Position|Stake|Shares) (Cut|Raised|Boosted|Lowered|Trimmed|Acquired|Sold)|"
    r"(Cut|Raised|Boosted|Lowered|Trimmed|Acquired|Sold) .* (Position|Stake|Shares) |13F|Short Interest", re.IGNORECASE)
_TTL = 3 * 3600


def _published(s: str):
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _split_source(title: str) -> tuple[str, str]:
    """구글뉴스 제목은 '기사 제목 - 매체명' 형식이다."""
    if " - " in title:
        head, _, src = title.rpartition(" - ")
        if head and len(src) <= 25:
            return head.strip(), src.strip()
    return title.strip(), ""


def fetch_headlines(name: str, market: str = "KR", n: int = 2, max_age_days: int = 5, fetch_fn=None) -> list[dict]:
    """[{title, source, date}] 최신순, 최대 n개. 못 가져오면 []."""
    key = f"{market}:{name}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1][:n]
    if fetch_fn is None:
        from clients import news_client
        base = news_client._GNEWS_KO if market == "KR" else news_client._GNEWS_EN
        query = (f'"{name}" (실적 OR 수주 OR 계약 OR 전망 OR 투자 OR 인수 OR 목표가)' if market == "KR"
                 else f'"{name}" (earnings OR guidance OR outlook OR deal OR analyst OR upgrade OR downgrade)')
        fetch_fn = lambda: news_client.fetch_news("후보뉴스", base + urllib.parse.quote(query), 10)
    try:
        items = fetch_fn() or []
    except Exception as e:
        logger.debug("[후보뉴스] %s 수집 실패: %s", name, e)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    out, seen = [], set()
    for it in items:
        dt = _published(it.get("published", ""))
        if not dt or dt < cutoff:
            continue
        title, source = _split_source(it.get("title", ""))
        sig = title[:18]
        if not title or sig in seen or source in _JUNK_SOURCES or _JUNK_TITLE.search(title):
            continue
        seen.add(sig)
        out.append({"title": title, "source": source, "date": dt.astimezone().strftime("%m/%d"), "_ts": dt})
    out.sort(key=lambda x: x["_ts"], reverse=True)
    for o in out:
        o.pop("_ts", None)
    if out:
        _CACHE[key] = (time.time(), out)
    return out[:n]


def lines_for(name: str, market: str = "KR", n: int = 2, fetch_fn=None) -> list[str]:
    """요약 메시지에 붙일 줄들. 없으면 []."""
    return [f"     📰 {h['title']}" + (f" ({h['source']}, {h['date']})" if h["source"] else f" ({h['date']})")
            for h in fetch_headlines(name, market, n, fetch_fn=fetch_fn)]
