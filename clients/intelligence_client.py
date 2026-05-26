"""
clients/intelligence_client.py
글로벌 투자 인텔리전스 RSS 수집 — 단순 뉴스가 아닌 전문가 분석·해석 중심 소스 대상.

기존 news_client.py(국내 헤드라인)와의 분업:
  - news_client     : 국내 경제지 헤드라인 수집 (팩트 레이어)
  - intelligence_client: 글로벌 분석 매체 + 전문가 관점 쿼리 (해석 레이어)
"""
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import feedparser

logger = logging.getLogger(__name__)

_MAX_AGE_HOURS = 36  # 36시간 이내 기사만 수집


# ── 분석·해석 중심 글로벌 RSS 소스 ─────────────────────────────────
# weight: 신뢰도/선행성/전문성 기준 (0~1) — LLM 컨텍스트 정렬 우선순위에 사용
INTELLIGENCE_SOURCES: dict[str, list[dict]] = {
    "global_macro": [
        {"name": "Reuters Markets",   "url": "https://feeds.reuters.com/reuters/businessNews",           "weight": 0.90},
        {"name": "MarketWatch",       "url": "https://feeds.marketwatch.com/marketwatch/topstories/",    "weight": 0.80},
        {"name": "CNBC Finance",      "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "weight": 0.80},
    ],
    "investment_analysis": [
        {"name": "Barrons",           "url": "https://www.barrons.com/xml/rss/3_7551.xml",               "weight": 0.85},
        {"name": "Seeking Alpha",     "url": "https://seekingalpha.com/market-outlook/rss.xml",           "weight": 0.75},
        {"name": "Motley Fool",       "url": "https://www.fool.com/feeds/index.aspx",                     "weight": 0.65},
    ],
    "korea_global_view": [
        {"name": "Korea Herald Biz",  "url": "https://www.koreaherald.com/rss/020100000000.xml",          "weight": 0.70},
    ],
}

# ── 전문가 관점·서사 중심 Google News 검색 쿼리 ──────────────────────
# "무슨 일이 있었나"가 아닌 "전문가들이 어떻게 해석하는가" 탐지용
_EXPERT_QUERIES: list[tuple[str, str, str]] = [
    # (key, 검색어, 언어)
    ("AI반도체_전망",     "AI semiconductor outlook analyst forecast 2025",          "en"),
    ("글로벌매크로_뷰",   "Federal Reserve rate cut 2025 outlook Wall Street",       "en"),
    ("중국증시_해석",     "China stock market Korea semiconductor impact analyst",    "en"),
    ("밸류에이션_논쟁",   "stock market overvalued bubble AI valuation concern",      "en"),
    ("외국인수급_논리",   "Korea KOSPI foreign investor emerging market outlook",     "en"),
    ("반도체_사이클",     "semiconductor cycle memory HBM recovery demand 2025",     "en"),
    ("강세약세_논쟁",     "bull bear case S&P500 market outlook analyst debate",     "en"),
    ("금리_컨센서스변화", "Fed rate cut consensus change market expectation shift",  "en"),
]

# ── 국내 투자 블로그 Google News 검색 쿼리 ─────────────────────────────
# site:blog.naver.com 필터로 개인 투자자 심층 분석 블로그 수집
_BLOG_QUERIES: list[tuple[str, str]] = [
    ("블로그_종목분석",  "site:blog.naver.com 종목분석 매수 투자전략"),
    ("블로그_반도체AI",  "site:blog.naver.com 반도체 AI 주식 전망"),
    ("블로그_수급분석",  "site:blog.naver.com 외국인 기관 순매수 코스피"),
    ("블로그_매크로",    "site:blog.naver.com 금리 환율 주식시장 분석"),
    ("블로그_실적시즌",  "site:blog.naver.com 실적 어닝 목표주가 상향"),
]

_GNEWS_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_GNEWS_KO = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def _is_fresh(entry, max_hours: int = _MAX_AGE_HOURS) -> bool:
    try:
        pp = entry.get("published_parsed")
        if not pp:
            return True
        pub_dt = datetime(*pp[:6], tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - pub_dt) <= timedelta(hours=max_hours)
    except Exception:
        return True


def _fetch_rss(url: str, max_items: int = 5) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items: list[dict] = []
        for entry in feed.entries:
            if not _is_fresh(entry):
                continue
            items.append({
                "title":     entry.get("title", "").strip(),
                "summary":   (entry.get("summary") or "")[:300].strip(),
                "link":      entry.get("link", ""),
                "published": entry.get("published", ""),
            })
            if len(items) >= max_items:
                break
        return items
    except Exception as e:
        logger.debug("인텔리전스 RSS 수집 실패 (%s): %s", url[:70], e)
        return []


def fetch_intelligence_feeds(max_per_source: int = 4) -> dict[str, list[dict]]:
    """분류별 글로벌 분석 RSS 수집. weight 기준 정렬 후 반환."""
    result: dict[str, list[dict]] = {}
    for category, sources in INTELLIGENCE_SOURCES.items():
        # weight 내림차순 정렬 → 신뢰도 높은 소스 우선 컨텍스트 배치
        sorted_sources = sorted(sources, key=lambda s: s["weight"], reverse=True)
        items: list[dict] = []
        for src in sorted_sources:
            fetched = _fetch_rss(src["url"], max_per_source)
            for item in fetched:
                item["source_name"]   = src["name"]
                item["source_weight"] = src["weight"]
            items.extend(fetched)
        if items:
            result[category] = items
    return result


def fetch_expert_queries(max_per_query: int = 3) -> dict[str, list[dict]]:
    """전문가 관점·서사 탐지 Google News RSS 쿼리 실행."""
    result: dict[str, list[dict]] = {}
    for key, query, lang in _EXPERT_QUERIES:
        template = _GNEWS_KO if lang == "ko" else _GNEWS_EN
        url = template.format(q=quote(query))
        items = _fetch_rss(url, max_per_query)
        if items:
            for item in items:
                item["query_key"] = key
            result[key] = items
    return result


def fetch_blog_queries(max_per_query: int = 4) -> dict[str, list[dict]]:
    """국내 투자 블로그 Google News RSS 검색 (site:blog.naver.com 필터)."""
    result: dict[str, list[dict]] = {}
    for key, query in _BLOG_QUERIES:
        url = _GNEWS_KO.format(q=quote(query))
        items = _fetch_rss(url, max_per_query)
        if items:
            for item in items:
                item["query_key"] = key
                item["source_type"] = "blog"
            result[key] = items
    return result


def fetch_all_intelligence(max_per_source: int = 4) -> dict:
    """전체 인텔리전스 피드 + 전문가 쿼리 + 블로그 통합 수집."""
    feeds  = fetch_intelligence_feeds(max_per_source)
    expert = fetch_expert_queries(max_per_query=3)
    blogs  = fetch_blog_queries(max_per_query=4)
    total  = (sum(len(v) for v in feeds.values())
              + sum(len(v) for v in expert.values())
              + sum(len(v) for v in blogs.values()))
    logger.info(
        "[인텔리전스클라이언트] 수집 완료 — 피드 %d카테고리, 전문가쿼리 %d개, 블로그 %d개, 총 %d건",
        len(feeds), len(expert), len(blogs), total,
    )
    return {"feeds": feeds, "expert_queries": expert, "blog_queries": blogs}
