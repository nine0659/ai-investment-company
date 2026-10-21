"""
clients/securities_report_client.py
네이버 증권 리서치(증권사 리포트) 수집.

2026-10 이전엔 finance.naver.com/research HTML 표를 긁었으나, 네이버가 이 페이지를
stock.naver.com/research(Next.js)로 이전해 표가 사라졌고 수집이 조용히 0건이 됐다
(에러 없이 빈 목록 반환 → 알 수 있는 경로가 없었음). 지금은 모바일 증권이 쓰는
JSON 목록 API를 사용한다 — HTML 구조 변경에 덜 민감하고 필드가 구조화돼 있다.

수집 대상(시장·매크로 분석 우선):
  - 경제분석 / 투자전략 : 매크로·시장 전망 (가장 중요)
  - 산업분석           : 섹터 업황
  - 종목분석           : 개별 종목 (보조)

주의: 이 리포트들은 '참고 대상'이다. 증권사 의견은 낙관 편향이 있고 목표가는 주가를
뒤따르는 경향이 있어, 그대로 결론으로 쓰지 않고 서로 비교·반박하는 재료로만 쓴다.
"""
import html as _html
import logging
import re
from datetime import datetime, date, timedelta
import requests

logger = logging.getLogger(__name__)

_API = "https://m.stock.naver.com/front-api/research/list"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
_TIMEOUT = 10

# API category → 표시용 유형 (매크로·시장 유형을 앞에)
REPORT_CATEGORIES: dict[str, str] = {
    "economy":  "경제분석",
    "invest":   "투자전략",
    "industry": "산업분석",
    "company":  "종목분석",
}

_MAX_DAYS_OLD = 3          # 주말·휴일을 넘기려면 2일로는 부족
_PAGE_SIZE = 40
_LIMIT_PER_TYPE = {"경제분석": 15, "투자전략": 15, "산업분석": 15, "종목분석": 12}


def _parse_report_date(date_str: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%y.%m.%d", "%Y.%m.%d", "%y-%m-%d"):
        try:
            return datetime.strptime(str(date_str).strip(), fmt).date()
        except ValueError:
            continue
    return None


def _is_recent(date_str: str, max_days: int = _MAX_DAYS_OLD) -> bool:
    d = _parse_report_date(date_str)
    if d is None:
        return False  # 날짜를 모르는 리포트는 오래된 것일 수 있어 제외
    return (date.today() - d).days <= max_days


def _normalize(item: dict, label: str) -> dict | None:
    title = (item.get("title") or "").strip()
    firm = (item.get("brokerName") or "").strip()
    if not title or not firm:
        return None
    return {
        "type":    label,
        "title":   title,
        "stock":   (item.get("itemName") or "").strip(),
        "code":    (item.get("itemCode") or "").strip(),
        "id":      item.get("researchId"),
        "firm":    firm,
        "date":    str(item.get("writeDate") or "").strip(),
        "link":    item.get("endUrl") or "",
        "summary": "",
    }


def _fetch_category(category: str, label: str) -> list[dict]:
    """한 유형의 최근 리포트 목록. 실패하면 빈 목록(예외 없음) — 호출부가 0건을 감지한다."""
    try:
        r = requests.get(
            _API, headers=_HEADERS, timeout=_TIMEOUT,
            params={"category": category, "pageSize": _PAGE_SIZE},
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("isSuccess"):
            logger.warning("[증권사리포트] %s 응답 실패: %s", label, str(data)[:150])
            return []
        out: list[dict] = []
        limit = _LIMIT_PER_TYPE.get(label, 12)
        for item in data.get("result") or []:
            rep = _normalize(item, label)
            if rep and _is_recent(rep["date"]):
                out.append(rep)
            if len(out) >= limit:
                break
        logger.debug("[증권사리포트] %s: %d건", label, len(out))
        return out
    except Exception as e:
        logger.warning("[증권사리포트] %s 수집 실패: %s", label, e)
        return []


def fetch_securities_reports(max_per_type: int | None = None, enrich_details: bool = False) -> list[dict]:
    """전체 유형 수집 후 통합 반환.

    반환: [{"type", "title", "stock", "code", "firm", "date", "link", "summary"}, ...]
    max_per_type/enrich_details는 옛 시그니처 호환용(본문 상세 API가 없어 요약은 비어 있음).
    """
    all_reports: list[dict] = []
    for category, label in REPORT_CATEGORIES.items():
        reps = _fetch_category(category, label)
        if max_per_type:
            reps = reps[:max_per_type]
        all_reports.extend(reps)
    logger.info("[증권사리포트] 총 %d건 수집 (유형: %d개)", len(all_reports), len(REPORT_CATEGORIES))
    return all_reports


def format_for_context(reports: list[dict]) -> str:
    """수집된 리포트를 LLM 컨텍스트용 텍스트로 포맷."""
    if not reports:
        return ""
    by_type: dict[str, list[dict]] = {}
    for r in reports:
        by_type.setdefault(r["type"], []).append(r)

    lines: list[str] = ["=== 증권사 리포트 (네이버 증권 리서치) ==="]
    for rtype in REPORT_CATEGORIES.values():
        items = by_type.get(rtype)
        if not items:
            continue
        lines.append(f"\n[{rtype}]")
        for item in items:
            stock_part = f" | {item['stock']}" if item.get("stock") else ""
            lines.append(f"  [{item['firm']}] {item['title']}{stock_part} ({item['date']})")
            if item.get("summary"):
                lines.append(f"    → {item['summary']}")
    return "\n".join(lines)


# ── 본문·PDF 전문 (깊이 있는 분석용) ───────────────────────────────────

_DETAIL_API = "https://m.stock.naver.com/api/research/{category}/{rid}"
_CATEGORY_BY_LABEL = {v: k for k, v in REPORT_CATEGORIES.items()}
_PDF_MAX_BYTES = 12 * 1024 * 1024
_PDF_MAX_PAGES = 12


def _html_to_text(raw: str) -> str:
    if not raw:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def fetch_report_detail(rtype: str, report_id) -> dict:
    """리포트 상세: 본문(HTML→텍스트), PDF 주소, (종목분석) 투자의견·목표가. 실패 시 빈 dict."""
    category = _CATEGORY_BY_LABEL.get(rtype)
    if not category or not report_id:
        return {}
    try:
        r = requests.get(_DETAIL_API.format(category=category, rid=report_id),
                         headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        rc = (r.json() or {}).get("researchContent") or {}
    except Exception as e:
        logger.debug("[리포트상세] %s/%s 실패: %s", rtype, report_id, e)
        return {}

    def _num(v):
        try:
            return int(float(str(v).replace(",", "")))
        except (TypeError, ValueError):
            return None

    return {
        "body":            _html_to_text(rc.get("content") or ""),
        "pdf_url":         rc.get("attachUrl") or "",
        "opinion":         (rc.get("opinion") or "").strip(),
        "goal_price":      _num(rc.get("goalPrice")),
        "price_at_write":  _num(rc.get("priceAtWriteDate")),   # 주의: prevGoalPrice는 이전 목표가가 아니라 작성일 주가와 같은 값이다
    }


def fetch_pdf_text(url: str, max_pages: int = _PDF_MAX_PAGES) -> str:
    """PDF 전문 텍스트 추출(pymupdf). 라이브러리·네트워크·파싱 어느 쪽이든 실패하면 빈 문자열."""
    if not url or not url.lower().split("?")[0].endswith(".pdf"):
        return ""
    try:
        import pymupdf
    except ImportError:
        logger.warning("[리포트PDF] pymupdf 미설치 — PDF 본문 생략")
        return ""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        if len(r.content) > _PDF_MAX_BYTES:
            return ""
        doc = pymupdf.open(stream=r.content, filetype="pdf")
        parts = [doc[i].get_text() for i in range(min(len(doc), max_pages))]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    except Exception as e:
        logger.debug("[리포트PDF] 추출 실패 %.80s: %s", url, e)
        return ""


def fetch_company_reports_for(codes: list[str], pages: int = 8) -> list[dict]:
    """보유 종목 등 특정 종목코드의 최근 종목분석 리포트.

    목록 API가 종목 필터를 지원하지 않아 최근 목록(100건×pages)을 받아 코드로 거른다.
    """
    wanted = {c for c in codes if c}
    if not wanted:
        return []
    out: list[dict] = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(_API, headers=_HEADERS, timeout=_TIMEOUT,
                             params={"category": "company", "pageSize": 100, "page": page})
            r.raise_for_status()
            items = (r.json() or {}).get("result") or []
        except Exception as e:
            logger.warning("[증권사리포트] 종목분석 page%d 실패: %s", page, e)
            break
        if not items:
            break
        for it in items:
            if (it.get("itemCode") or "") in wanted:
                rep = _normalize(it, "종목분석")
                if rep:
                    out.append(rep)
    return out
