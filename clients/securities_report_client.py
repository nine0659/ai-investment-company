"""
clients/securities_report_client.py
네이버 금융 증권사 리포트 수집 (서버사이드 렌더링 — 별도 인증 불필요)

수집 대상:
  - 종목분석 리포트: 목표주가·투자의견·실적 전망
  - 산업분석 리포트: 섹터 전망·업황 분석
  - 시장분석 리포트: 매크로·투자전략

출처: https://finance.naver.com/research/
"""
import logging
from datetime import datetime, date
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/research/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
_BASE = "https://finance.naver.com"
_TIMEOUT = 10

# 수집 대상 리포트 유형
REPORT_PAGES: dict[str, str] = {
    "종목분석": f"{_BASE}/research/company_list.naver",
    "산업분석": f"{_BASE}/research/industry_list.naver",
    "투자전략": f"{_BASE}/research/invest_list.naver",
    "채권분석": f"{_BASE}/research/debenture_list.naver",
}

_MAX_PER_TYPE = 15  # 유형별 최대 수집 건수
_MAX_DAYS_OLD = 2   # 최근 N일 이내 리포트만


def _parse_report_date(date_str: str) -> date | None:
    """'25.05.26' 또는 '2025.05.26' 형식 파싱."""
    for fmt in ("%y.%m.%d", "%Y.%m.%d", "%y-%m-%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _is_recent(date_str: str, max_days: int = _MAX_DAYS_OLD) -> bool:
    d = _parse_report_date(date_str)
    if d is None:
        return True  # 날짜 파싱 실패 시 포함
    return (date.today() - d).days <= max_days


def _fetch_report_page(label: str, url: str, max_items: int) -> list[dict]:
    """네이버 금융 리포트 목록 페이지 파싱."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")

        rows = soup.select("table.type_1 tr")
        reports: list[dict] = []

        for row in rows:
            cols = row.select("td")
            if len(cols) < 4:
                continue

            # 종목분석: 제목(0) | 종목명(1) | 증권사(2) | 날짜(3)
            # 산업/시장: 제목(0) | -(1)      | 증권사(2) | 날짜(3)
            title_tag = cols[0].select_one("a")
            if not title_tag:
                continue

            title     = title_tag.get_text(strip=True)
            stock     = cols[1].get_text(strip=True) if len(cols) > 1 else ""
            firm      = cols[2].get_text(strip=True) if len(cols) > 2 else ""
            date_str  = cols[3].get_text(strip=True) if len(cols) > 3 else ""

            if not title or not firm:
                continue
            if not _is_recent(date_str):
                continue

            href = title_tag.get("href", "")
            link = (_BASE + href) if href.startswith("/") else href

            reports.append({
                "type":   label,
                "title":  title,
                "stock":  stock,
                "firm":   firm,
                "date":   date_str,
                "link":   link,
            })

            if len(reports) >= max_items:
                break

        logger.debug("[증권사리포트] %s: %d건", label, len(reports))
        return reports

    except Exception as e:
        logger.warning("[증권사리포트] %s 수집 실패: %s", label, e)
        return []


def fetch_securities_reports(max_per_type: int = _MAX_PER_TYPE) -> list[dict]:
    """전체 리포트 유형 수집 후 통합 반환.

    반환: [{"type", "title", "stock", "firm", "date", "link"}, ...]
    """
    all_reports: list[dict] = []
    for label, url in REPORT_PAGES.items():
        reports = _fetch_report_page(label, url, max_per_type)
        all_reports.extend(reports)

    logger.info("[증권사리포트] 총 %d건 수집 (유형: %d개)", len(all_reports), len(REPORT_PAGES))
    return all_reports


def format_for_context(reports: list[dict]) -> str:
    """수집된 리포트를 LLM 컨텍스트용 텍스트로 포맷."""
    if not reports:
        return ""

    by_type: dict[str, list[dict]] = {}
    for r in reports:
        by_type.setdefault(r["type"], []).append(r)

    lines: list[str] = ["=== 증권사 리포트 (네이버 금융) ==="]
    for rtype, items in by_type.items():
        lines.append(f"\n[{rtype}]")
        for item in items:
            stock_part = f" | {item['stock']}" if item["stock"] else ""
            lines.append(
                f"  [{item['firm']}] {item['title']}{stock_part} ({item['date']})"
            )

    return "\n".join(lines)
