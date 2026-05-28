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

# 복합 지정학·기관수급·AI 이벤트 — 한국어 Google News 검색 RSS
# 단일 RSS 피드로 잡히지 않는 복합·긴급 이슈 커버
_COMPOUND_QUERIES = [
    # ── 기존 ──────────────────────────────────────────────
    ("트럼프_관세_반도체",   "트럼프 관세 반도체 AI"),
    ("미중협상_증시",        "미중 협상 증시 반도체"),
    ("엔비디아_한국",        "엔비디아 젠슨황 한국 증시"),
    ("연준_금리_코스피",     "연준 금리 코스피 외국인"),
    # ── 지정학 리스크 (이란·중동·러시아·북한) ────────────────
    ("지정학_중동_원유",     "이란 미군 중동 공격 원유 이스라엘"),
    ("지정학_러시아_우크라", "러시아 우크라이나 전쟁 증시 방산"),
    ("지정학_북한_한국",     "북한 도발 한국 코스피 방산주"),
    # ── 국내 기관 수급 (국민연금·연기금·외국인 대량 이동) ────
    ("국민연금_수급",        "국민연금 주식 비율 매도 코스피"),
    ("연기금_외국인_수급",   "연기금 외국인 기관 순매도 수급 코스피"),
    # ── 거시 충격 (금리·환율·원자재 급변) ──────────────────
    ("환율_원달러_급등",     "원달러 환율 급등 외환 코스피 충격"),
    ("원자재_충격_한국",     "원유 금 구리 천연가스 급등 한국 수출"),
]
_GNEWS_BASE    = "https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q="
_GNEWS_EN_BASE = "https://news.google.com/rss/search?hl=en&gl=US&ceid=US:en&q="

# 영문 Google News — 글로벌 지정학 이슈 조기 포착
_COMPOUND_QUERIES_EN = [
    ("geopolitical_risk",   "Iran attack US military Israel Middle East oil"),
    ("fed_market_shock",    "Federal Reserve rate hike shock stock market crash"),
    ("china_taiwan_risk",   "China Taiwan military tension stock market"),
]


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
    """복합 지정학·기관수급·AI 이벤트 Google News RSS 검색.
    단일 매체 RSS로는 잡히지 않는 이슈(지정학, 국민연금, 환율충격 등) 추가 수집.
    """
    results: dict[str, list[dict]] = {}
    # 한국어 검색
    for key, query in _COMPOUND_QUERIES:
        url = _GNEWS_BASE + urllib.parse.quote(query)
        items = fetch_news(key, url, max_items)
        if items:
            results[key] = items
            logger.debug("[복합뉴스] %s: %d건", key, len(items))
    # 영문 검색 (글로벌 지정학 이슈 조기 포착)
    for key, query in _COMPOUND_QUERIES_EN:
        url = _GNEWS_EN_BASE + urllib.parse.quote(query)
        items = fetch_news(f"[EN]{key}", url, max_items)
        if items:
            results[f"global_{key}"] = items
            logger.debug("[글로벌뉴스EN] %s: %d건", key, len(items))
    return results


def fetch_all_news(max_per_category: int = 8) -> dict[str, list[dict]]:
    result = {name: fetch_news(name, url, max_per_category) for name, url in RSS_FEEDS.items()}
    # 복합 이벤트 뉴스 병합 (키 충돌 없음 — 별도 키 사용)
    result.update(fetch_compound_news(max_items=5))
    return result
