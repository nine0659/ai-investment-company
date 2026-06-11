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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
_DETAIL_TIMEOUT = 6  # 상세 페이지 fetch 타임아웃

# 종목분석 상세 페이지 요약 추출 셀렉터 (우선순위 순)
_DETAIL_SELECTORS = [
    "td.text",
    ".research_txt",
    ".view_txt",
    "#report_contents",
    "table.view_tb td",
    "div.report_view td",
    ".cont_area",
]

# 수집 대상 리포트 유형
REPORT_PAGES: dict[str, str] = {
    "종목분석": f"{_BASE}/research/company_list.naver",
    "산업분석": f"{_BASE}/research/industry_list.naver",
    "투자전략": f"{_BASE}/research/invest_list.naver",
    "채권분석": f"{_BASE}/research/debenture_list.naver",
}

_MAX_PER_TYPE = 15   # 유형별 최대 수집 건수
_MAX_DAYS_OLD = 2    # 최근 N일 이내 리포트만
_DETAIL_TOP_N = 7    # 종목분석 중 상세 본문을 fetch할 최대 건수
_DETAIL_MAX_CHARS = 350  # 상세 요약 최대 문자


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

            title    = title_tag.get_text(strip=True)
            stock    = cols[1].get_text(strip=True) if len(cols) > 1 else ""
            firm     = cols[2].get_text(strip=True) if len(cols) > 2 else ""
            date_str = cols[3].get_text(strip=True) if len(cols) > 3 else ""

            if not title or not firm:
                continue
            if not _is_recent(date_str):
                continue

            href = title_tag.get("href", "")
            link = (_BASE + href) if href.startswith("/") else href

            reports.append({
                "type":    label,
                "title":   title,
                "stock":   stock,
                "firm":    firm,
                "date":    date_str,
                "link":    link,
                "summary": "",  # 상세 fetch 후 채워짐
            })

            if len(reports) >= max_items:
                break

        logger.debug("[증권사리포트] %s: %d건", label, len(reports))
        return reports

    except Exception as e:
        logger.warning("[증권사리포트] %s 수집 실패: %s", label, e)
        return []


def _fetch_detail_summary(link: str) -> str:
    """네이버 금융 리포트 상세 페이지에서 요약 텍스트 추출.

    실패 시 빈 문자열 반환 (예외 없음).
    """
    if not link or "finance.naver.com" not in link:
        return ""
    try:
        r = requests.get(link, headers=_HEADERS, timeout=_DETAIL_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="euc-kr")

        # 스크립트·스타일 제거
        for tag in soup(["script", "style"]):
            tag.decompose()

        for sel in _DETAIL_SELECTORS:
            tag = soup.select_one(sel)
            if not tag:
                continue
            text = " ".join(tag.get_text(separator=" ", strip=True).split())
            if len(text) >= 40:
                return text[:_DETAIL_MAX_CHARS]

        return ""
    except Exception as e:
        logger.debug("[리포트상세] 실패 [%.60s]: %s", link, e)
        return ""


def _enrich_company_reports(reports: list[dict]) -> list[dict]:
    """종목분석 리포트 상위 N건에 상세 요약 추가 (병렬 fetch)."""
    targets = [r for r in reports if r["type"] == "종목분석"][:_DETAIL_TOP_N]
    if not targets:
        return reports

    summaries: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_map = {ex.submit(_fetch_detail_summary, r["link"]): r["link"] for r in targets}
        for future in as_completed(future_map):
            link = future_map[future]
            try:
                s = future.result()
                if s:
                    summaries[link] = s
            except Exception:
                pass

    result = []
    for r in reports:
        if r["link"] in summaries:
            result.append({**r, "summary": summaries[r["link"]]})
        else:
            result.append(r)

    fetched = len(summaries)
    if fetched:
        logger.info("[리포트상세] 종목분석 %d/%d건 요약 추가", fetched, len(targets))
    return result


def fetch_securities_reports(
    max_per_type: int = _MAX_PER_TYPE,
    enrich_details: bool = True,
) -> list[dict]:
    """전체 리포트 유형 수집 후 통합 반환.

    반환: [{"type", "title", "stock", "firm", "date", "link", "summary"}, ...]
    """
    all_reports: list[dict] = []
    for label, url in REPORT_PAGES.items():
        reports = _fetch_report_page(label, url, max_per_type)
        all_reports.extend(reports)

    if enrich_details and all_reports:
        all_reports = _enrich_company_reports(all_reports)

    logger.info("[증권사리포트] 총 %d건 수집 (유형: %d개)", len(all_reports), len(REPORT_PAGES))
    return all_reports


def format_for_context(reports: list[dict]) -> str:
    """수집된 리포트를 LLM 컨텍스트용 텍스트로 포맷.

    종목분석 리포트는 요약(summary)이 있으면 2번째 줄에 들여쓰기로 표시.
    """
    if not reports:
        return ""

    by_type: dict[str, list[dict]] = {}
    for r in reports:
        by_type.setdefault(r["type"], []).append(r)

    lines: list[str] = ["=== 증권사 리포트 (네이버 금융) ==="]
    for rtype, items in by_type.items():
        lines.append(f"\n[{rtype}]")
        for item in items:
            stock_part = f" | {item['stock']}" if item.get("stock") else ""
            lines.append(
                f"  [{item['firm']}] {item['title']}{stock_part} ({item['date']})"
            )
            if item.get("summary"):
                lines.append(f"    → {item['summary']}")

    return "\n".join(lines)
