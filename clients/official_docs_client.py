"""
clients/official_docs_client.py
중앙은행·국제기구 1차 문서(원문) 수집 — Fed(FOMC 성명·의사록), 한국은행(금통위 의사록·통화정책 보도자료),
ECB(통화정책·연설). 언론 해석 없이 원문을 읽기 위한 것이다.

두 단계: list_official_documents()는 RSS 메타데이터만, fetch_official_text()는 선택된 문서의 본문만 받는다
(문서가 수만 자라 전부 받지 않는다).
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
_TIMEOUT = 20
_MAX_TEXT = 60000

# (표시 출처, RSS, 최근 N일, 제목 필터 정규식)
_SOURCES = [
    ("Fed", "https://www.federalreserve.gov/feeds/press_monetary.xml", 30,
     r"FOMC|Federal Open Market|Monetary Policy"),
    ("한국은행", "https://www.bok.or.kr/portal/bbs/P0000559/news.rss", 30,
     r"금융통화위원회|통화정책|경제전망|물가|금융안정"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html", 14,
     r"monetary policy|inflation|economic bulletin|projection|Lagarde|Lane|Schnabel"),
    # 연설·증언·연구노트 — 정책 방향의 단서 (2026-10-21 추가)
    ("Fed 연설", "https://www.federalreserve.gov/feeds/speeches.xml", 14, r"."),
    ("Fed 의회증언", "https://www.federalreserve.gov/feeds/testimony.xml", 45, r"."),
    ("뉴욕연준 연구", "https://libertystreeteconomics.newyorkfed.org/feed/", 10, r"."),
    ("샌프란시스코연준", "https://www.frbsf.org/feed/?post_type=economic-letter", 21, r"."),
    ("BIS 연설", "https://www.bis.org/doclist/cbspeeches.rss", 10, r"."),
    ("일본은행", "https://www.boj.or.jp/en/rss/whatsnew.xml", 14,
     r"outlook|monetary|policy|summary|opinions|Governor|Ueda"),
]

# 한국은행 상세 페이지는 menuNo가 비면 404 — 보도자료 게시판 메뉴 번호
_BOK_MENU_NO = "200690"


def _recent(entry, days: int) -> bool:
    pp = entry.get("published_parsed")
    if not pp:
        return True
    pub = datetime(*pp[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - pub <= timedelta(days=days)


def list_official_documents(max_per_source: int = 3) -> list[dict]:
    """공식 문서 메타데이터 목록 (본문 미포함). 소스별 실패는 건너뛰고 나머지를 돌려준다."""
    docs: list[dict] = []
    for name, url, days, pattern in _SOURCES:
        try:
            r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            r.raise_for_status()
            entries = feedparser.parse(r.content).entries
        except Exception as e:
            logger.warning("[공식문서] %s 목록 실패: %s", name, e)
            continue
        n = 0
        for en in entries:
            title = (en.get("title") or "").strip()
            if not title or not _recent(en, days) or not re.search(pattern, title, re.I):
                continue
            link = en.get("link") or ""
            if name == "한국은행":
                link = re.sub(r"menuNo=\s*$", f"menuNo={_BOK_MENU_NO}", link)
            pub = ""
            if en.get("published_parsed"):
                pub = datetime(*en["published_parsed"][:6]).strftime("%Y-%m-%d")
            docs.append({"type": "공식문서", "firm": name, "title": title, "stock": "",
                         "date": pub, "link": link, "summary": ""})
            n += 1
            if n >= max_per_source:
                break
    return docs


def _page_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for t in soup(["script", "style", "nav", "header", "footer", "form"]):
        t.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def fetch_official_text(doc: dict) -> str:
    """선택된 문서의 본문. 실패하면 빈 문자열."""
    link = doc.get("link") or ""
    if not link:
        return ""
    try:
        r = requests.get(link, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        firm = doc.get("firm")

        if firm == "Fed":
            # 보도자료 껍데기 → 의사록 본문 페이지로 따라간다 (성명서는 자체가 본문)
            m = re.search(r"/monetarypolicy/fomcminutes\d+\.htm", r.text)
            if m:
                r = requests.get(urljoin(link, m.group(0)), headers=_HEADERS, timeout=_TIMEOUT)
                r.raise_for_status()
            return _page_text(r.content)[:_MAX_TEXT]

        if firm == "한국은행":
            soup = BeautifulSoup(r.content, "html.parser")
            pdf = next((a["href"] for a in soup.select("a[href]")
                        if a["href"].lower().endswith(".pdf") and "/fileSrc/" in a["href"]), "")
            if pdf:
                from clients.securities_report_client import fetch_pdf_text
                t = fetch_pdf_text(urljoin(link, pdf), max_pages=30)
                if t:
                    return t[:_MAX_TEXT]
            return _page_text(r.content)[:_MAX_TEXT]

        return _page_text(r.content)[:_MAX_TEXT]
    except Exception as e:
        logger.debug("[공식문서] 본문 실패 %.80s: %s", link, e)
        return ""
