import logging
import feedparser

logger = logging.getLogger(__name__)

RSS_FEEDS = {
    "한국경제":    "https://www.hankyung.com/rss/finance.xml",
    "매일경제":    "https://www.mk.co.kr/rss/40300001/",
    "이데일리":    "https://www.edaily.co.kr/rss/sub_rss.xml",
    "머니투데이":  "https://news.mt.co.kr/mtviewRss.html",
    "연합인포맥스": "https://news.einfomax.co.kr/rss/allArticle.xml",
}


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


def fetch_all_news(max_per_category: int = 8) -> dict[str, list[dict]]:
    return {name: fetch_news(name, url, max_per_category) for name, url in RSS_FEEDS.items()}
