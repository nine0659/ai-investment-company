"""
clients/news_client.py
뉴스 수집 클라이언트

[설계 원칙]
- 검색어는 절대 고정하지 않는다.
- 매 실행마다 현재 시황 + 초기 헤드라인을 LLM에 넘겨 "오늘 이슈가 될 검색어"를 동적 생성.
- LLM이 현재 지정학 갈등, 정책 변화, 수급 이슈 등을 스스로 판단해 검색어를 만든다.
"""
import json
import logging
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# ── 고정 RSS 피드 (국내 주요 경제지) ───────────────────────────────
RSS_FEEDS = {
    "한국경제":    "https://www.hankyung.com/rss/finance.xml",
    "매일경제":    "https://www.mk.co.kr/rss/40300001/",
    "이데일리":    "https://www.edaily.co.kr/rss/sub_rss.xml",
    "머니투데이":  "https://news.mt.co.kr/mtviewRss.html",
    "연합인포맥스": "https://news.einfomax.co.kr/rss/allArticle.xml",
}

_GNEWS_KO = "https://news.google.com/rss/search?hl=ko&gl=KR&ceid=KR:ko&q="
_GNEWS_EN = "https://news.google.com/rss/search?hl=en&gl=US&ceid=US:en&q="

# ── 항상 실행할 '기준선' 쿼리 (최소 보장) ───────────────────────────
# LLM 장애 시 폴백용. 최소 6개만 유지.
_FALLBACK_QUERIES: list[tuple[str, str, str]] = [
    ("KO", "코스피_수급",     "KOSPI 외국인 기관 수급 순매도 순매수"),
    ("KO", "미국_증시_여파",  "미국 증시 S&P500 나스닥 한국 영향"),
    ("KO", "지정학_위험",     "전쟁 분쟁 공격 지정학 리스크 증시"),
    ("KO", "국민연금_수급",   "국민연금 연기금 주식 비율 매도"),
    ("KO", "환율_달러",       "원달러 환율 급등 외환 증시"),
    ("EN", "global_risk",     "geopolitical risk stock market crash oil"),
]


# ── 기사 수집 ─────────────────────────────────────────────────────

def fetch_news(name: str, url: str, max_items: int = 10) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        return [
            {
                "source":    name,
                "title":     e.get("title", ""),
                "summary":   (e.get("summary") or e.get("description") or "")[:300],
                "link":      e.get("link", ""),
                "published": e.get("published", ""),
            }
            for e in feed.entries[:max_items]
        ]
    except Exception as e:
        logger.warning("뉴스 수집 실패 [%s]: %s", name, e)
        return []


def _fetch_gnews(lang: str, query: str, key: str, max_items: int = 5) -> list[dict]:
    base = _GNEWS_KO if lang == "KO" else _GNEWS_EN
    url  = base + urllib.parse.quote(query)
    return fetch_news(key, url, max_items)


# ── LLM 동적 검색어 생성 ─────────────────────────────────────────

_SCOUT_SYSTEM = """당신은 주식시장 뉴스 인텔리전스 전문가입니다.

[역할]
오늘 한국 주식시장(KOSPI/KOSDAQ)에 실질적인 영향을 줄 수 있는
이슈를 능동적으로 발굴하여 Google News 검색어를 생성합니다.

[검색어 생성 원칙]
1. 현재 뉴스 헤드라인에서 감지된 이슈를 더 깊이 파고드는 검색어
2. 헤드라인에 없지만 시장 움직임(지수·환율·원자재)에서 유추되는 숨은 원인
3. 지정학 리스크: 현재 진행 중인 분쟁·협상·제재의 최신 동향
4. 수급 이슈: 외국인·기관·국민연금·연기금의 최근 동향
5. 정책·금리: 각국 중앙은행 결정, 재정정책, 규제 변화
6. 섹터 이슈: 오늘 특히 강하거나 약한 섹터의 원인 추적

[출력 형식 — 반드시 이 JSON만 출력, 설명 없이]
{
  "ko": ["한국어 검색어1", "한국어 검색어2", ...(최대 8개)],
  "en": ["English query1", "English query2", ...(최대 4개)]
}

각 검색어는 Google News에 직접 입력할 수 있는 구체적인 문구여야 합니다.
일반적이고 막연한 검색어(예: "주식 시장") 금지.
현재 상황과 직결된 구체적 키워드 조합 필수."""


def generate_dynamic_queries(initial_news: dict, market_data: dict) -> list[tuple[str, str, str]]:
    """LLM이 현재 헤드라인 + 시황 기반으로 동적 검색어 생성.

    Returns: [(lang, key, query), ...]  lang: "KO" or "EN"
    """
    try:
        from clients.openai_client import chat

        today = datetime.now(_KST).strftime("%Y-%m-%d %H:%M KST")

        # 초기 헤드라인 수집
        headlines = []
        for source, items in initial_news.items():
            for item in items[:4]:
                if t := item.get("title"):
                    headlines.append(f"[{source}] {t}")
        headline_text = "\n".join(headlines[:35]) if headlines else "없음"

        # 시장 현황 요약
        market_lines = []
        for key, label in [
            ("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"), ("vix", "VIX"),
            ("usd_krw", "USD/KRW"), ("oil_wti", "WTI"), ("gold", "금"),
            ("us10y", "미국10년금리"), ("sp500_futures", "S&P선물"),
        ]:
            d = market_data.get(key, {})
            if isinstance(d, dict):
                chg = d.get("change_pct") or d.get("change") or 0
                val = d.get("close") or d.get("current") or 0
                if val:
                    market_lines.append(f"{label}: {val} ({chg:+.2f}%)")
        market_text = "\n".join(market_lines) if market_lines else "없음"

        user_prompt = f"""날짜: {today}

[현재 시장 현황]
{market_text}

[초기 뉴스 헤드라인 (RSS 수집)]
{headline_text}

위 정보를 분석하여 오늘 추가 탐색이 필요한 핵심 이슈 검색어를 JSON으로 생성하세요.
헤드라인에 이미 나온 이슈는 더 깊이 파고들고,
시장 데이터에서 원인을 알 수 없는 움직임이 있다면 그 원인을 추적하는 검색어를 포함하세요."""

        result = chat(_SCOUT_SYSTEM, user_prompt, max_tokens=400)

        # JSON 파싱
        match = re.search(r'\{[^{}]*"ko"[^{}]*\}', result, re.DOTALL)
        if not match:
            match = re.search(r'\{.*?\}', result, re.DOTALL)
        if not match:
            raise ValueError("JSON 파싱 실패")

        parsed = json.loads(match.group())
        queries: list[tuple[str, str, str]] = []

        for i, q in enumerate(parsed.get("ko", [])[:8]):
            if isinstance(q, str) and len(q.strip()) >= 4:
                queries.append(("KO", f"dynamic_ko_{i+1}", q.strip()))

        for i, q in enumerate(parsed.get("en", [])[:4]):
            if isinstance(q, str) and len(q.strip()) >= 4:
                queries.append(("EN", f"dynamic_en_{i+1}", q.strip()))

        logger.info("[뉴스스카우트] 동적 검색어 %d개 생성: %s",
                    len(queries), [q[2][:20] for q in queries[:5]])
        return queries

    except Exception as e:
        logger.warning("[뉴스스카우트] 동적 생성 실패 (%s) → 폴백 사용", e)
        return _FALLBACK_QUERIES


# ── 통합 뉴스 수집 ────────────────────────────────────────────────

def fetch_static_rss(max_per: int = 8) -> dict[str, list[dict]]:
    """고정 RSS 피드만 수집 (Google News 검색 없이)."""
    return {name: fetch_news(name, url, max_per) for name, url in RSS_FEEDS.items()}


def fetch_dynamic_compound_news(queries: list[tuple[str, str, str]], max_items: int = 5) -> dict:
    """동적으로 생성된 쿼리로 Google News 검색."""
    results: dict[str, list[dict]] = {}
    for lang, key, query in queries:
        items = _fetch_gnews(lang, query, key, max_items)
        if items:
            results[key] = items
            logger.debug("[뉴스스카우트] %s(%s): %d건", key, query[:20], len(items))
    return results


def fetch_all_news(max_per_category: int = 8, market_data: dict = None) -> dict[str, list[dict]]:
    """전체 뉴스 수집 파이프라인.

    1단계: 고정 RSS 수집
    2단계: RSS 헤드라인 + 시황 → LLM 검색어 동적 생성
    3단계: 동적 검색어로 Google News 추가 수집
    4단계: 전체 병합
    """
    # 1단계: 고정 RSS
    result = fetch_static_rss(max_per_category)
    logger.info("[뉴스수집] RSS 완료: %d개 소스", len(result))

    # 2단계: LLM 동적 검색어 생성
    dynamic_queries = generate_dynamic_queries(result, market_data or {})

    # 3단계: 동적 검색어로 Google News 수집
    dynamic_news = fetch_dynamic_compound_news(dynamic_queries, max_items=5)
    result.update(dynamic_news)
    logger.info("[뉴스수집] 동적검색 완료: %d개 쿼리, 총 %d개 소스",
                len(dynamic_queries), len(result))

    return result
