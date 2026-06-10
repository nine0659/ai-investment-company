"""
clients/ipo_calendar_client.py
대형 IPO / 메가 상장 이벤트 수집 — 시장 자금 이탈 리스크 사전 감지

SpaceX처럼 수십조 규모 IPO는 차익실현·자금 재배치로 나스닥 급락을 유발할 수 있음.
이 클라이언트는 RSS 뉴스에서 IPO 관련 기사를 수집하고 메가 이벤트를 분류합니다.
"""
import logging
from datetime import datetime, timezone, timedelta

import feedparser

logger = logging.getLogger(__name__)

_MAX_AGE_HOURS = 72  # 3일 이내 기사
_MAX_PER_FEED = 40

# IPO 관련 키워드 (영문/한글)
_IPO_KEYWORDS = [
    "ipo", "initial public offering", "going public", "debut", "listing",
    "공모", "상장", "기업공개", "상장 예정", "코스피 상장", "코스닥 상장",
]

# 시장에 자금 이탈 충격을 줄 수 있는 메가 기업 목록 (지속 업데이트)
_MEGA_COMPANIES = [
    "SpaceX", "스페이스X",
    "OpenAI", "오픈AI",
    "Stripe", "스트라이프",
    "Databricks",
    "ByteDance", "TikTok", "틱톡",
    "Shein", "쉬인",
    "Klarna",
    "Chime",
    "Revolut",
    "Anthropic", "앤트로픽",
    "Waymo",
    "Epic Games",
]

# 수집 대상 RSS 피드
_FEEDS = [
    {"url": "https://feeds.reuters.com/reuters/businessNews",           "label": "Reuters"},
    {"url": "https://feeds.marketwatch.com/marketwatch/topstories/",    "label": "MarketWatch"},
    {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "label": "CNBC"},
    {"url": "https://www.koreaherald.com/rss/020100000000.xml",         "label": "KoreaHerald"},
]


def _is_ipo_related(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(kw.lower() in text for kw in _IPO_KEYWORDS)


def _is_mega_ipo(title: str, summary: str) -> bool:
    text = f"{title} {summary}"
    return any(company.lower() in text.lower() for company in _MEGA_COMPANIES)


def fetch_ipo_events(hours_back: int = _MAX_AGE_HOURS) -> list[dict]:
    """IPO/상장 관련 뉴스 수집. 메가 IPO는 is_mega=True로 분류."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    results: list[dict] = []

    for feed_info in _FEEDS:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:_MAX_PER_FEED]:
                title   = entry.get("title", "")
                summary = entry.get("summary", "")

                if not _is_ipo_related(title, summary):
                    continue

                published_parsed = entry.get("published_parsed")
                if published_parsed:
                    pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue

                results.append({
                    "title":     title,
                    "summary":   summary[:250],
                    "source":    feed_info["label"],
                    "is_mega":   _is_mega_ipo(title, summary),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            logger.debug("[IPO캘린더] %s 수집 실패: %s", feed_info["label"], e)

    # 메가 IPO 먼저, 최신순 정렬
    results.sort(key=lambda x: (x["is_mega"], x["published"]), reverse=True)
    return results[:12]


def format_for_context(events: list[dict]) -> str:
    """이벤트 목록을 LLM 컨텍스트 텍스트로 변환."""
    if not events:
        return ""

    mega   = [e for e in events if e["is_mega"]]
    normal = [e for e in events if not e["is_mega"]]

    lines = ["=== 대형 IPO / 상장 이벤트 동향 (자금 이탈 리스크) ==="]

    if mega:
        lines.append("\n[🔴 메가 IPO — 나스닥/글로벌 자금 이탈 주의]")
        for e in mega:
            lines.append(f"  [{e['source']}] {e['title']}")
            if e["summary"]:
                lines.append(f"    → {e['summary'][:180]}")

    if normal:
        lines.append("\n[일반 IPO / 공모 동향]")
        for e in normal[:5]:
            lines.append(f"  [{e['source']}] {e['title']}")

    return "\n".join(lines)
