"""
글로벌 빅피겨 발언/행보 뉴스 수집 (Google News RSS + feedparser)
"""
import logging
import feedparser
from urllib.parse import quote

logger = logging.getLogger(__name__)

BIG_FIGURES: dict[str, dict] = {
    "젠슨황": {
        "name_en":  "Jensen Huang",
        "org":      "엔비디아",
        "sector":   "AI/반도체",
        "query_ko": "젠슨황 엔비디아",
        "query_en": "Jensen Huang Nvidia",
    },
    "일론머스크": {
        "name_en":  "Elon Musk",
        "org":      "테슬라/SpaceX/xAI",
        "sector":   "전기차/로봇/AI",
        "query_ko": "일론머스크 테슬라",
        "query_en": "Elon Musk Tesla robot",
    },
    "손정의": {
        "name_en":  "Masayoshi Son",
        "org":      "소프트뱅크",
        "sector":   "AI/스타트업투자",
        "query_ko": "손정의 소프트뱅크 투자",
        "query_en": "Masayoshi Son SoftBank AI investment",
    },
    "팀쿡": {
        "name_en":  "Tim Cook",
        "org":      "애플",
        "sector":   "스마트폰/부품",
        "query_ko": "팀쿡 애플",
        "query_en": "Tim Cook Apple",
    },
    "샘알트만": {
        "name_en":  "Sam Altman",
        "org":      "OpenAI",
        "sector":   "AI",
        "query_ko": "샘알트만 OpenAI",
        "query_en": "Sam Altman OpenAI",
    },
    "제롬파월": {
        "name_en":  "Jerome Powell",
        "org":      "연준(Fed)",
        "sector":   "금리/통화정책",
        "query_ko": "제롬파월 연준 금리",
        "query_en": "Jerome Powell Federal Reserve interest rate",
    },
    "워런버핏": {
        "name_en":  "Warren Buffett",
        "org":      "버크셔해서웨이",
        "sector":   "시장전망",
        "query_ko": "워런버핏 버크셔",
        "query_en": "Warren Buffett Berkshire investment",
    },
    "래리핑크": {
        "name_en":  "Larry Fink",
        "org":      "블랙록",
        "sector":   "글로벌자금흐름",
        "query_ko": "래리핑크 블랙록",
        "query_en": "Larry Fink BlackRock",
    },
}


def _google_news_url(query: str, lang: str = "ko") -> str:
    encoded = quote(query)
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def _parse_rss(url: str, max_items: int = 3) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:max_items]:
            items.append({
                "title":     entry.get("title", "").strip(),
                "summary":   (entry.get("summary") or "")[:250].strip(),
                "link":      entry.get("link", ""),
                "published": entry.get("published", ""),
            })
        return items
    except Exception as e:
        logger.debug("RSS 파싱 실패 (%s): %s", url[:60], e)
        return []


def fetch_bigfigure_news(max_per_figure: int = 3) -> list[dict]:
    """각 빅피겨의 최신 뉴스 수집.
    한국어 뉴스 우선, 부족하면 영어 뉴스 보완.
    """
    results: list[dict] = []
    for key, info in BIG_FIGURES.items():
        # 한국어 뉴스 먼저 시도
        ko_items = _parse_rss(_google_news_url(info["query_ko"], "ko"), max_per_figure)
        if len(ko_items) < 2:
            # 한국어 뉴스 부족 시 영어로 보완
            en_items = _parse_rss(_google_news_url(info["query_en"], "en"), max_per_figure)
            news_items = (ko_items + en_items)[:max_per_figure]
        else:
            news_items = ko_items[:max_per_figure]

        if news_items:
            results.append({
                "figure_key": key,
                "name_ko":    key,
                "name_en":    info["name_en"],
                "org":        info["org"],
                "sector":     info["sector"],
                "news_items": news_items,
            })

    logger.info("[빅피겨] %d명 뉴스 수집 완료", len(results))
    return results
