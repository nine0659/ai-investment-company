"""
clients/article_body_client.py
뉴스 기사 / 리서치 리포트 본문 추출 — 도메인별 CSS 셀렉터 우선, 범용 폴백
병렬 fetch로 속도 최소화.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://www.google.com/",
}
_TIMEOUT = 5
_MIN_BODY_LEN = 60  # 이 길이 미만은 본문으로 인정 안 함

# 도메인별 기사 본문 CSS 셀렉터 (우선순위 순)
_DOMAIN_SELECTORS: dict[str, list[str]] = {
    "hankyung.com":    ["#articlebody", ".article-body", ".article_body", "#article-body"],
    "mk.co.kr":        ["#article_body", ".view_text", ".news_view_content", ".art_txt"],
    "edaily.co.kr":    ["#newsContentDiv", ".article_txt", "#newsContent", ".news_text"],
    "mt.co.kr":        ["#textBody", ".news_cnt_detail_wrap", ".news_txt"],
    "einfomax.co.kr":  [".view_body", "#txtBody", ".article-body"],
    "yna.co.kr":       [".story-news.article", "#articleBody", ".article-body"],
    "newspim.com":     [".article_txt", "#articleBody"],
    "heraldcorp.com":  ["#article-view-content-div", ".article-view-content"],
    "sedaily.com":     ["#articleBody", ".article_view"],
    "finance.naver.com": [
        "td.text",          # 리서치 상세 페이지 본문 셀
        ".research_txt",
        ".view_txt",
        "#report_contents",
        "div.report_view",
        "td.view",
    ],
}

_GENERIC_SELECTORS = [
    "article",
    "[class*='article-body']",
    "[class*='article_body']",
    "[class*='news_body']",
    "[id*='articleBody']",
    "[id*='article_body']",
    "[id*='newsContent']",
    "main p",
]


def fetch_body(url: str, max_chars: int = 500, timeout: int = _TIMEOUT) -> str:
    """URL에서 기사·리포트 본문 추출. 실패 시 빈 문자열 반환 (예외 없음)."""
    if not url or not url.startswith("http"):
        return ""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()

        # EUC-KR 대응
        encoding = r.encoding or ""
        if encoding.lower() in ("euc-kr", "cp949", "euc_kr"):
            html = r.content.decode("euc-kr", errors="replace")
        else:
            html = r.text

        soup = BeautifulSoup(html, "html.parser")

        # 불필요 태그 제거
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        # 도메인별 셀렉터 시도
        domain = urlparse(url).netloc.lower()
        domain_key = next((k for k in _DOMAIN_SELECTORS if k in domain), None)
        selectors = (
            (_DOMAIN_SELECTORS[domain_key] if domain_key else []) + _GENERIC_SELECTORS
        )

        for sel in selectors:
            tag = soup.select_one(sel)
            if not tag:
                continue
            text = tag.get_text(separator=" ", strip=True)
            # 공백 정리
            text = " ".join(text.split())
            if len(text) >= _MIN_BODY_LEN:
                return text[:max_chars]

        return ""
    except Exception as e:
        logger.debug("[기사본문] 실패 [%.60s]: %s", url, e)
        return ""


def enrich_with_body(
    items: list[dict],
    max_items: int = 10,
    max_chars: int = 400,
    workers: int = 6,
) -> list[dict]:
    """상위 N개 뉴스 아이템에 본문 텍스트 추가 (병렬 fetch).

    Args:
        items: [{"link": ..., "title": ..., "summary": ...}, ...]
        max_items: 본문을 fetch할 최대 아이템 수
        max_chars: 본문 최대 문자 수
        workers: 동시 요청 수

    Returns:
        "body" 필드가 추가된 새 리스트 (실패한 아이템은 원본 유지)
    """
    candidates = [(i, it) for i, it in enumerate(items) if it.get("link") and i < max_items]
    if not candidates:
        return items

    body_by_idx: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(fetch_body, it["link"], max_chars): idx
            for idx, it in candidates
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                body = future.result()
                if body:
                    body_by_idx[idx] = body
            except Exception:
                pass

    if not body_by_idx:
        return items

    result = list(items)
    for idx, body in body_by_idx.items():
        result[idx] = {**result[idx], "body": body}

    fetched = len(body_by_idx)
    logger.info("[기사본문] %d/%d건 본문 추출 성공", fetched, len(candidates))
    return result
