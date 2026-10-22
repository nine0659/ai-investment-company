"""후보 종목 뉴스 헤드라인 회귀 테스트 — 자동 생성 시세 기사·기관 보유 변동 스팸 제거, 오래된 기사 제외, 실패는 조용히 빈 값."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from services import candidate_news as cn


def _pub(days_ago=0):
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1))


def _item(title, days_ago=0):
    return {"title": title, "published": _pub(days_ago), "link": "x", "summary": ""}


@pytest.fixture(autouse=True)
def clear_cache():
    cn._CACHE.clear()
    yield
    cn._CACHE.clear()


def test_split_source():
    assert cn._split_source("한국전력, 목표가 하향 - 파이낸셜포스트") == ("한국전력, 목표가 하향", "파이낸셜포스트")
    assert cn._split_source("제목만 있음") == ("제목만 있음", "")


def test_keeps_real_news_and_drops_auto_generated_junk():
    items = [_item("LG씨엔에스 주가, 10월 22일 장중 74,000원 2.35% 상승 - TopStarNews"),
             _item("Intuit Inc. Stock Position Cut by Robeco Institutional - MarketBeat"),
             _item("Intuit Position Raised by Some Fund LLC - Defense World"),
             _item("LG씨엔에스 AI·클라우드 수주 실적화 관심 - 잡포스트"),
             _item("대신證 \"한국전력, 저평가 국면 진입\" - 뉴스1")]
    out = cn.fetch_headlines("X", "KR", n=5, fetch_fn=lambda: items)
    assert {o["title"] for o in out} == {"LG씨엔에스 AI·클라우드 수주 실적화 관심", "대신證 \"한국전력, 저평가 국면 진입\""}


def test_old_articles_excluded_and_sorted_newest_first():
    items = [_item("오래된 기사 - A", days_ago=9), _item("어제 기사 - B", days_ago=1), _item("오늘 기사 - C", days_ago=0)]
    out = cn.fetch_headlines("Y", "KR", n=5, fetch_fn=lambda: items)
    assert [o["title"] for o in out] == ["오늘 기사", "어제 기사"]


def test_duplicate_titles_collapsed_and_limit_applied():
    items = [_item("같은 제목의 기사입니다 오늘 - A"), _item("같은 제목의 기사입니다 오늘 - B"), _item("다른 기사 하나 - C"), _item("또 다른 기사 - D")]
    out = cn.fetch_headlines("Z", "KR", n=2, fetch_fn=lambda: items)
    assert len(out) == 2 and len({o["title"][:18] for o in out}) == 2


def test_unparseable_date_skipped():
    out = cn.fetch_headlines("W", "KR", fetch_fn=lambda: [{"title": "날짜 없음 - A", "published": "???"}])
    assert out == []


def test_fetch_failure_returns_empty():
    def boom():
        raise RuntimeError("net")
    assert cn.fetch_headlines("V", "KR", fetch_fn=boom) == []
    assert cn.lines_for("V", "KR", fetch_fn=boom) == []


def test_results_cached():
    calls = []
    fn = lambda: calls.append(1) or [_item("캐시 기사 - A")]
    cn.fetch_headlines("C", "KR", fetch_fn=fn)
    cn.fetch_headlines("C", "KR", fetch_fn=fn)
    assert len(calls) == 1


def test_lines_format():
    lines = cn.lines_for("L", "KR", fetch_fn=lambda: [_item("좋은 기사 - 매체")])
    assert len(lines) == 1 and lines[0].startswith("     📰 좋은 기사 (매체,")
