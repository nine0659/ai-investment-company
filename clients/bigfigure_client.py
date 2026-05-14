"""
글로벌 빅피겨 발언/행보 뉴스 수집 (Google News RSS + feedparser)

커버리지 기준:
  - 미국 연준·재무부: 금리·환율·채권 직결
  - 글로벌 중앙은행: ECB·일본은행 — 엔/원 환율, 달러 인덱스 영향
  - 미국 대통령·무역정책: 관세·반도체 수출규제
  - AI·반도체 CEO: 삼성·SK·한화 등 국내 공급망 직결
  - 전기차·배터리 CEO: 국내 배터리 대형주 직결
  - 글로벌 자금흐름: 대형 자산운용사 수장
  - 중국·아시아 정책: 홍콩EQ, 중국 소비 관련 국내주
"""
import logging
import feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 빅피겨 뉴스 최신성 기준: 72시간 이내 기사만 포함
_NEWS_MAX_AGE_HOURS = 72

BIG_FIGURES: dict[str, dict] = {
    # ── 미국 통화·재정 정책 ────────────────────────────────────────
    "제롬파월": {
        "name_en":  "Jerome Powell",
        "org":      "연준(Fed)",
        "sector":   "금리/통화정책",
        "query_ko": "제롬파월 연준 금리",
        "query_en": "Jerome Powell Federal Reserve rate",
    },
    "스콧베센트": {
        "name_en":  "Scott Bessent",
        "org":      "미국 재무부",
        "sector":   "재정/환율/관세",
        "query_ko": "스콧베센트 재무부",
        "query_en": "Scott Bessent Treasury tariff",
    },
    "도널드트럼프": {
        "name_en":  "Donald Trump",
        "org":      "미국 대통령",
        "sector":   "관세/반도체규제/경제정책",
        "query_ko": "트럼프 관세 반도체",
        "query_en": "Trump tariff semiconductor trade",
    },
    # ── 글로벌 중앙은행 ────────────────────────────────────────────
    "크리스틴라가르드": {
        "name_en":  "Christine Lagarde",
        "org":      "ECB(유럽중앙은행)",
        "sector":   "유럽금리/달러인덱스",
        "query_ko": "라가르드 ECB 금리",
        "query_en": "Lagarde ECB rate policy",
    },
    "우에다가즈오": {
        "name_en":  "Kazuo Ueda",
        "org":      "일본은행(BOJ)",
        "sector":   "엔화/금리/외국인수급",
        "query_ko": "우에다 일본은행 금리",
        "query_en": "Ueda Bank of Japan rate yen",
    },
    # ── AI / 반도체 CEO ───────────────────────────────────────────
    "젠슨황": {
        "name_en":  "Jensen Huang",
        "org":      "엔비디아",
        "sector":   "AI/반도체/HBM",
        "query_ko": "젠슨황 엔비디아 AI",
        "query_en": "Jensen Huang Nvidia AI chip",
    },
    "리사수": {
        "name_en":  "Lisa Su",
        "org":      "AMD",
        "sector":   "AI/반도체",
        "query_ko": "리사수 AMD 반도체",
        "query_en": "Lisa Su AMD AI accelerator",
    },
    "팻겔싱어": {
        "name_en":  "Pat Gelsinger",
        "org":      "인텔",
        "sector":   "반도체/파운드리",
        "query_ko": "팻겔싱어 인텔",
        "query_en": "Pat Gelsinger Intel foundry",
    },
    "르네하스": {
        "name_en":  "Rene Haas",
        "org":      "Arm",
        "sector":   "반도체IP/모바일칩",
        "query_ko": "Arm 반도체 라이선스",
        "query_en": "Rene Haas Arm chip license",
    },
    # ── 빅테크 AI / 클라우드 CEO ─────────────────────────────────
    "샘알트만": {
        "name_en":  "Sam Altman",
        "org":      "OpenAI",
        "sector":   "AI",
        "query_ko": "샘알트만 OpenAI",
        "query_en": "Sam Altman OpenAI GPT",
    },
    "사티아나델라": {
        "name_en":  "Satya Nadella",
        "org":      "마이크로소프트",
        "sector":   "AI/클라우드/기업SW",
        "query_ko": "사티아나델라 마이크로소프트 AI",
        "query_en": "Satya Nadella Microsoft AI cloud",
    },
    "순다르피차이": {
        "name_en":  "Sundar Pichai",
        "org":      "구글(알파벳)",
        "sector":   "AI/광고/검색",
        "query_ko": "순다르피차이 구글 AI",
        "query_en": "Sundar Pichai Google AI Gemini",
    },
    "마크저커버그": {
        "name_en":  "Mark Zuckerberg",
        "org":      "메타",
        "sector":   "AI/소셜미디어/VR",
        "query_ko": "저커버그 메타 AI",
        "query_en": "Mark Zuckerberg Meta AI Llama",
    },
    "앤디재시": {
        "name_en":  "Andy Jassy",
        "org":      "아마존",
        "sector":   "클라우드(AWS)/물류",
        "query_ko": "앤디재시 아마존 AWS",
        "query_en": "Andy Jassy Amazon AWS cloud",
    },
    # ── 전기차 / 배터리 ───────────────────────────────────────────
    "일론머스크": {
        "name_en":  "Elon Musk",
        "org":      "테슬라/xAI",
        "sector":   "전기차/배터리/AI로봇",
        "query_ko": "일론머스크 테슬라 전기차",
        "query_en": "Elon Musk Tesla EV robot",
    },
    "팀쿡": {
        "name_en":  "Tim Cook",
        "org":      "애플",
        "sector":   "스마트폰/부품/AI",
        "query_ko": "팀쿡 애플 아이폰",
        "query_en": "Tim Cook Apple iPhone AI",
    },
    # ── 글로벌 투자자 / 자금흐름 ──────────────────────────────────
    "워런버핏": {
        "name_en":  "Warren Buffett",
        "org":      "버크셔해서웨이",
        "sector":   "시장전망/포트폴리오",
        "query_ko": "워런버핏 버크셔 투자",
        "query_en": "Warren Buffett Berkshire investment",
    },
    "래리핑크": {
        "name_en":  "Larry Fink",
        "org":      "블랙록",
        "sector":   "글로벌자금흐름/ETF",
        "query_ko": "래리핑크 블랙록 시장",
        "query_en": "Larry Fink BlackRock market outlook",
    },
    "손정의": {
        "name_en":  "Masayoshi Son",
        "org":      "소프트뱅크",
        "sector":   "AI/스타트업투자",
        "query_ko": "손정의 소프트뱅크 AI 투자",
        "query_en": "Masayoshi Son SoftBank AI investment",
    },
    # ── 중국·아시아 정책 ──────────────────────────────────────────
    "리창": {
        "name_en":  "Li Qiang",
        "org":      "중국 국무원",
        "sector":   "중국경제/관세/반도체규제",
        "query_ko": "리창 중국 경제 정책",
        "query_en": "Li Qiang China economy policy",
    },
    "이창용": {
        "name_en":  "Rhee Chang-yong",
        "org":      "한국은행(BOK)",
        "sector":   "원화금리/환율",
        "query_ko": "이창용 한국은행 금리",
        "query_en": "Rhee Chang-yong Bank of Korea rate",
    },
}


def _google_news_url(query: str, lang: str = "ko") -> str:
    encoded = quote(query)
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"


def _is_fresh(entry) -> bool:
    """published_parsed 기준 72시간 이내 뉴스인지 확인. 날짜 파싱 실패 시 포함."""
    try:
        pp = entry.get("published_parsed")
        if not pp:
            return True  # 날짜 없으면 최신으로 간주
        pub_dt = datetime(*pp[:6], tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - pub_dt
        return age <= timedelta(hours=_NEWS_MAX_AGE_HOURS)
    except Exception:
        return True


def _parse_rss(url: str, max_items: int = 3) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries:
            if not _is_fresh(entry):
                continue
            items.append({
                "title":     entry.get("title", "").strip(),
                "summary":   (entry.get("summary") or "")[:250].strip(),
                "link":      entry.get("link", ""),
                "published": entry.get("published", ""),
            })
            if len(items) >= max_items:
                break
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
