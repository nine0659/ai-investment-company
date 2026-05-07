import io
import logging
import zipfile
import xml.etree.ElementTree as ET
import requests
from config.settings import DART_API_KEY

logger = logging.getLogger(__name__)

_BASE = "https://opendart.fss.or.kr/api"
_corp_map: dict[str, str] = {}   # stock_code(6자리) → corp_code(8자리)

# 재무 항목 → 가능한 계정명 목록 (회사마다 표기 다름)
_ACCOUNT_MAP = {
    "매출액":    ["매출액", "영업수익", "수익(매출액)"],
    "영업이익":  ["영업이익", "영업이익(손실)"],
    "당기순이익": ["당기순이익", "당기순이익(손실)", "분기순이익"],
    "자본총계":  ["자본총계"],
    "자산총계":  ["자산총계"],
    "부채총계":  ["부채총계"],
}


# ── 기업 코드 ──────────────────────────────────────────────────

def _load_corp_map() -> dict[str, str]:
    global _corp_map
    if _corp_map:
        return _corp_map
    try:
        r = requests.get(f"{_BASE}/corpCode.xml", params={"crtfc_key": DART_API_KEY}, timeout=30)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            with z.open("CORPCODE.xml") as f:
                root = ET.parse(f).getroot()
        mapping = {}
        for item in root.findall("list"):
            sc = (item.findtext("stock_code") or "").strip()
            cc = (item.findtext("corp_code") or "").strip()
            if sc and cc:
                mapping[sc.zfill(6)] = cc
        _corp_map = mapping
        logger.info("DART 기업코드 로드: %d개", len(mapping))
    except Exception as e:
        logger.error("DART 기업코드 로드 실패: %s", e)
    return _corp_map


def get_corp_code(stock_code: str) -> str | None:
    return _load_corp_map().get(stock_code.zfill(6))


# ── 재무제표 조회 ──────────────────────────────────────────────

def get_financial_statements(corp_code: str, year: int,
                              reprt_code: str = "11011") -> list[dict]:
    """
    단일회사 전체 재무제표 조회
    reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기
    """
    for fs_div in ("CFS", "OFS"):   # 연결 → 개별 순서로 시도
        try:
            r = requests.get(
                f"{_BASE}/fnlttSinglAcnt.json",
                params={"crtfc_key": DART_API_KEY, "corp_code": corp_code,
                        "bsns_year": str(year), "reprt_code": reprt_code,
                        "fs_div": fs_div},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                return data["list"]
        except Exception as e:
            logger.warning("DART 재무제표 조회 실패 (%s/%s): %s", corp_code, fs_div, e)
    return []


def extract_financials(statements: list[dict]) -> dict[str, int]:
    """재무제표 리스트에서 주요 항목 금액 추출 (단위: 원)"""
    result: dict[str, int] = {}
    for label, names in _ACCOUNT_MAP.items():
        for item in statements:
            if any(n in (item.get("account_nm") or "") for n in names):
                raw = (item.get("thstrm_amount") or "0").replace(",", "").strip()
                try:
                    result[label] = int(raw)
                    break
                except ValueError:
                    pass
    return result


def get_multi_year_financials(stock_code: str, years: int = 3) -> list[dict]:
    """최근 N년치 연간 재무 데이터 수집"""
    from datetime import datetime
    corp_code = get_corp_code(stock_code)
    if not corp_code:
        logger.warning("기업코드 없음: %s", stock_code)
        return []

    current_year = datetime.now().year
    result = []
    for y in range(current_year - 1, current_year - 1 - years, -1):
        stmts = get_financial_statements(corp_code, y)
        if stmts:
            fin = extract_financials(stmts)
            fin["year"] = y
            result.append(fin)
    return result
