import logging
import urllib.parse
import feedparser

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    "한국경제":    "https://www.hankyung.com/rss/finance.xml",
    "매일경제":    "https://www.mk.co.kr/rss/40300001/",
    "이데일리":    "https://www.edaily.co.kr/rss/sub_rss.xml",
    "머니투데이":  "https://news.mt.co.kr/mtviewRss.html",
    "연합인포맥스": "https://news.einfomax.co.kr/rss/allArticle.xml",
}

# 복합 지정학·AI 이벤트 — 한국어 Google News 검색 RSS
# 트럼프+AI/반도체, 미중협상, 빅피겨+한국증시 등 단일 RSS로 잡히기 어려운 복합 이슈 커버
_COMPOUND_QUERIES = [
    ("트럼프_관세_반도체",  "트럼프 관세 반도체 AI"),
    ("미중협상_증시",       "미중 협상 증시 반도체"),
    ("엔비디아_한국",       "엔비디아 젠슨황 한국 증시"),
    ("연준_금리_코스피",    "연준 금리 코스피 외국인"),
]
_GNEWS_BASE = "https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q="


def fetch_news(name: str, url: str, max_items: int = 10) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "source":    name,
                "title":     entry.get("title", ""),
                "summary":   (entry.get("summary") or entry.get("description") or "")[:300],
                "link":      entry.get("link", ""),
                "published": entry.get("published", ""),
            })
        return items
    except Exception as e:
        logger.warning("뉴스 수집 실패 [%s]: %s", name, e)
        return []


def fetch_compound_news(max_items: int = 5) -> dict[str, list[dict]]:
    """복합 지정학·AI 이벤트 Google News RSS 검색.
    단일 매체 RSS로는 잡히지 않는 트럼프+AI/반도체, 미중협상 등 복합 이슈를 추가 수집.
    """
    results: dict[str, list[dict]] = {}
    for key, query in _COMPOUND_QUERIES:
        url = _GNEWS_BASE + urllib.parse.quote(query)
        items = fetch_news(key, url, max_items)
        if items:
            results[key] = items
            logger.debug("[복합뉴스] %s: %d건", key, len(items))
        else:
            logger.debug("[복합뉴스] %s: 결과 없음", key)
    return results


def fetch_all_news(max_per_category: int = 8) -> dict[str, list[dict]]:
    result = {name: fetch_news(name, url, max_per_category) for name, url in RSS_FEEDS.items()}
    # 복합 이벤트 뉴스 병합 (키 충돌 없음 — 별도 키 사용)
    result.update(fetch_compound_news(max_items=5))
    return result
