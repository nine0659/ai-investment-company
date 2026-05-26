"""
clients/blog_scraper_client.py
등록된 네이버 블로그에서 최신 포스트를 수집·분석용 텍스트로 반환.

전략:
  - 블로그 주소만 있는 경우: RSS(rss.blog.naver.com/{id}.xml)로 최신글 목록 → 모바일 URL로 본문 수집
  - 특정 포스트 URL: 모바일 URL로 직접 본문 수집

출처: 사용자 선정 국내 투자 블로거 17명
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.blog.naver.com/",
}
_TIMEOUT = 12
_MAX_POSTS_PER_BLOG = 3    # 블로그당 최대 수집 포스트 수
_MAX_DAYS_OLD = 3           # 최근 N일 이내 포스트만
_MAX_CONTENT_CHARS = 1200  # 포스트 본문 최대 문자 수


BLOG_URLS: list[str] = [
    "https://blog.naver.com/centum_tiger",
    "https://blog.naver.com/doctordk",
    "https://blog.naver.com/luy1978",
    "https://blog.naver.com/keumssoa",
    "https://blog.naver.com/tmdejr1267",
    "https://blog.naver.com/tosoha1",
    "https://blog.naver.com/ranto28",
    "https://blog.naver.com/dkanchup",
    "https://blog.naver.com/jhlimidea",
    "https://blog.naver.com/hhhhnk/224292926350",
    "https://blog.naver.com/khiro38",
    "https://blog.naver.com/gmyhhj",
    "https://blog.naver.com/tama2020",
    "https://blog.naver.com/somewhaterror",
    "https://blog.naver.com/bambooinvesting",
    "https://blog.naver.com/sunsetfrappuccino",
    "https://blog.naver.com/shinook430",
]


def _parse_blog_url(url: str) -> tuple[str, str | None]:
    """URL에서 (blog_id, post_no) 추출. post_no는 없으면 None."""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    blog_id = parts[0] if parts else ""
    post_no = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
    return blog_id, post_no


def _rss_posts(blog_id: str) -> list[dict]:
    """RSS로 최신 포스트 목록 반환. [{title, post_no, date_str}, ...]"""
    rss_url = f"https://rss.blog.naver.com/{blog_id}.xml"
    cutoff = datetime.now(timezone.utc) - timedelta(days=_MAX_DAYS_OLD)
    try:
        feed = feedparser.parse(rss_url)
        posts: list[dict] = []
        for entry in feed.entries:
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if published and published < cutoff:
                continue

            link = entry.get("link", "")
            _, post_no = _parse_blog_url(link)
            if not post_no:
                m = re.search(r"logNo=(\d+)", link)
                post_no = m.group(1) if m else None
            if post_no:
                posts.append({
                    "title": entry.get("title", ""),
                    "post_no": post_no,
                    "date_str": entry.get("published", ""),
                })
            if len(posts) >= _MAX_POSTS_PER_BLOG:
                break
        return posts
    except Exception as e:
        logger.warning("[블로그스크래퍼] RSS 실패 %s: %s", blog_id, e)
        return []


def _fetch_post_content(blog_id: str, post_no: str) -> str:
    """모바일 URL로 포스트 본문 텍스트 추출."""
    url = f"https://m.blog.naver.com/{blog_id}/{post_no}"
    try:
        r = requests.get(url, headers=_MOBILE_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # SE 에디터 (신형) → 구형 순서로 시도
        content_div = (
            soup.select_one(".se-main-container")
            or soup.select_one("#postViewArea")
            or soup.select_one(".post_ct")
            or soup.select_one(".se_doc_viewer")
            or soup.select_one("div[class*='post']")
        )
        if content_div:
            text = content_div.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:_MAX_CONTENT_CHARS]
        return ""
    except Exception as e:
        logger.warning("[블로그스크래퍼] 본문 수집 실패 %s/%s: %s", blog_id, post_no, e)
        return ""


def fetch_blog_posts(blog_urls: list[str] | None = None) -> list[dict]:
    """등록된 블로그들에서 최신 포스트 수집.

    반환: [{"blog_id", "title", "post_no", "content", "url", "date_str"}, ...]
    """
    urls = blog_urls or BLOG_URLS
    results: list[dict] = []

    for raw_url in urls:
        blog_id, post_no = _parse_blog_url(raw_url)
        if not blog_id:
            continue

        if post_no:
            content = _fetch_post_content(blog_id, post_no)
            if content:
                results.append({
                    "blog_id": blog_id,
                    "title": f"{blog_id} 포스트",
                    "post_no": post_no,
                    "content": content,
                    "url": f"https://m.blog.naver.com/{blog_id}/{post_no}",
                    "date_str": "",
                })
        else:
            posts = _rss_posts(blog_id)
            for p in posts:
                content = _fetch_post_content(blog_id, p["post_no"])
                if content:
                    results.append({
                        "blog_id": blog_id,
                        "title": p["title"],
                        "post_no": p["post_no"],
                        "content": content,
                        "url": f"https://m.blog.naver.com/{blog_id}/{p['post_no']}",
                        "date_str": p["date_str"],
                    })

    logger.info("[블로그스크래퍼] %d개 블로그 -> %d건 수집", len(urls), len(results))
    return results


def format_for_context(posts: list[dict]) -> str:
    """수집된 블로그 포스트를 LLM 컨텍스트용 텍스트로 포맷."""
    if not posts:
        return ""
    lines = ["=== 국내 투자 블로그 포스트 (직접 수집) ==="]
    for p in posts:
        date_part = f" ({p['date_str']})" if p["date_str"] else ""
        lines.append(f"\n[{p['blog_id']}] {p['title']}{date_part}")
        lines.append(p["content"][:800])
        lines.append("---")
    return "\n".join(lines)
