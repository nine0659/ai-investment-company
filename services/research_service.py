"""
기업 리서치 서비스
종목코드 또는 회사명으로 종합 투자 분석 리포트를 생성한다.
데이터 수집: KIS(가격/밸류에이션) + DART(재무제표) + yfinance(기술적) + 뉴스
"""
import logging
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from config.settings import DART_API_KEY

logger = logging.getLogger(__name__)

_DART_BASE = "https://opendart.fss.or.kr/api"
_corp_name_map: dict[str, dict] = {}   # stock_code → {corp_code, name}


# ── 기업코드·이름 검색 ──────────────────────────────────────────

def _load_corp_name_map() -> dict[str, dict]:
    """stock_code → {corp_code, name} 매핑. 이름 검색에도 사용."""
    global _corp_name_map
    if _corp_name_map:
        return _corp_name_map
    try:
        r = requests.get(
            f"{_DART_BASE}/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=30,
        )
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open("CORPCODE.xml") as f:
                root = ET.parse(f).getroot()
        mapping: dict[str, dict] = {}
        for item in root.findall("list"):
            sc   = (item.findtext("stock_code") or "").strip()
            cc   = (item.findtext("corp_code") or "").strip()
            name = (item.findtext("corp_name") or "").strip()
            if sc and cc:
                mapping[sc.zfill(6)] = {"corp_code": cc, "name": name}
        _corp_name_map = mapping
        logger.info("DART 기업코드+이름 로드: %d개", len(mapping))
    except Exception as e:
        logger.warning("DART 기업코드 로드 실패: %s", e)
    return _corp_name_map


def resolve_code(code_or_name: str) -> tuple[str, str]:
    """입력값(코드 또는 회사명) → (stock_code, corp_name).
    못 찾으면 ('', '') 반환.
    """
    query = code_or_name.strip()

    # 6자리 숫자 → 코드로 직접 처리
    if query.isdigit() and len(query) <= 6:
        code = query.zfill(6)
        corp_map = _load_corp_name_map()
        name = corp_map.get(code, {}).get("name", code)
        return code, name

    # 이름 검색 — 정확 일치 우선, 그 다음 부분 일치
    corp_map = _load_corp_name_map()
    candidates = []
    for sc, info in corp_map.items():
        corp_name = info.get("name", "")
        if corp_name == query:
            return sc, corp_name          # 정확 일치
        if query in corp_name or corp_name in query:
            candidates.append((sc, corp_name))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        # 가장 짧은(=더 정확한) 이름 반환
        candidates.sort(key=lambda x: len(x[1]))
        return candidates[0]

    return "", ""


def _detect_market(code: str) -> str:
    """코드로 KOSPI/KOSDAQ 추정 (KIS 조회 기반)."""
    from clients.kis_client import KISClient
    kis = KISClient()
    data = kis.get_stock_price(code, market="J")
    if data.get("price"):
        return "KOSPI"
    data = kis.get_stock_price(code, market="Q")
    if data.get("price"):
        return "KOSDAQ"
    return "KOSPI"  # 기본값


def gather_company_data(code: str, name: str = "") -> dict:
    """주어진 종목코드에 대한 모든 데이터를 수집·통합한다."""
    from clients.kis_client import KISClient
    from clients.market_data_client import fetch_kr_stock_technicals
    from clients.dart_client import get_multi_year_financials
    from clients.news_client import fetch_all_news

    result: dict = {"code": code, "name": name}

    # ── 1. 가격·밸류에이션 (KIS) ──────────────────────────────
    try:
        kis = KISClient()
        price_data = kis.get_stock_price(code, market=None)
        result["price"] = price_data

        # 배당 정보
        try:
            result["dividend"] = kis.get_dividend_info(code)
        except Exception:
            result["dividend"] = {}

        # 시장 구분
        result["market"] = _detect_market(code)
    except Exception as e:
        logger.warning("[리서치] 가격 조회 실패 (%s): %s", code, e)
        result["price"] = {}
        result["market"] = "KOSPI"

    # ── 2. 재무제표 (DART) ───────────────────────────────────
    try:
        fins = get_multi_year_financials(code, years=3)
        result["financials"] = fins
    except Exception as e:
        logger.warning("[리서치] DART 재무 조회 실패 (%s): %s", code, e)
        result["financials"] = []

    # ── 3. 기술적 지표 (yfinance) ───────────────────────────
    try:
        market_sfx = "KS" if result["market"] == "KOSPI" else "KQ"
        yfin_sym = f"{code}.{market_sfx}"
        from clients.market_data_client import fetch_kr_stock_technicals
        tech = fetch_kr_stock_technicals(yfin_sym)
        result["technicals"] = tech or {}
    except Exception as e:
        logger.warning("[리서치] 기술적 지표 실패 (%s): %s", code, e)
        result["technicals"] = {}

    # ── 4. 최근 뉴스 ─────────────────────────────────────────
    try:
        all_news = fetch_all_news(max_per_category=5)
        # 뉴스에서 회사명 포함 기사 필터링
        company_news = []
        search_name = name or code
        for cat_news in all_news.values() if isinstance(all_news, dict) else []:
            for article in (cat_news if isinstance(cat_news, list) else []):
                title = article.get("title", "")
                if search_name and search_name[:2] in title:
                    company_news.append(article)
        result["news"] = company_news[:10]
    except Exception as e:
        logger.warning("[리서치] 뉴스 조회 실패: %s", e)
        result["news"] = []

    return result


def research_company(code_or_name: str) -> str:
    """종목코드 또는 회사명으로 종합 투자 분석 리포트 생성.

    반환: 텔레그램 발송 가능한 마크다운 텍스트
    """
    from agents.research_agent import analyze

    # 1. 코드 해석
    code, name = resolve_code(code_or_name)
    if not code:
        return (
            f"❌ '{code_or_name}' 종목을 찾지 못했습니다.\n"
            "6자리 종목코드로 다시 시도해 주세요. 예: 005930"
        )

    logger.info("[리서치서비스] 시작: %s(%s)", name, code)

    # 2. 데이터 수집
    data = gather_company_data(code, name)

    # 3. AI 분석 생성
    report = analyze(data)

    logger.info("[리서치서비스] 완료: %s(%s)", name, code)
    return report


def search_companies(query: str) -> list[dict]:
    """회사명 일부로 검색 — 여러 후보 반환 (최대 5개).
    각 항목: {code, name}
    """
    corp_map = _load_corp_name_map()
    results = []
    for sc, info in corp_map.items():
        corp_name = info.get("name", "")
        if query in corp_name:
            results.append({"code": sc, "name": corp_name})
        if len(results) >= 20:
            break
    # 이름 길이 순 정렬 (더 정확한 매치 우선)
    results.sort(key=lambda x: len(x["name"]))
    return results[:5]
