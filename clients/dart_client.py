import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
import requests
from config.settings import DART_API_KEY

logger = logging.getLogger(__name__)

_BASE = "https://opendart.fss.or.kr/api"
_corp_map: dict[str, str] = {}

# 계정명 → 가능한 명칭 목록
_NAME_MAP = {
    "매출액": [
        "매출액", "영업수익", "수익(매출액)", "매출", "순매출액",
        "영업수익합계", "수익합계", "매출총액",
    ],
    "영업이익": [
        "영업이익", "영업이익(손실)", "영업손익", "영업이익(영업손실)",
    ],
    "당기순이익": [
        "당기순이익", "당기순이익(손실)", "분기순이익", "당기순손익",
        "당기순이익(당기순손실)", "연결당기순이익",
        "지배기업의 소유주에게 귀속되는 당기순이익",
    ],
    "자본총계": ["자본총계", "자본합계", "총자본"],
    "자산총계": ["자산총계", "자산합계", "총자산"],
    "부채총계": ["부채총계", "부채합계", "총부채"],
}

# 계정이 속한 재무제표 구분 (BS/IS/CIS)
_SJ_HINT = {
    "매출액":   ("IS", "CIS"),
    "영업이익": ("IS", "CIS"),
    "당기순이익": ("IS", "CIS"),
    "자본총계": ("BS",),
    "자산총계": ("BS",),
    "부채총계": ("BS",),
}


# ── 기업 코드 ──────────────────────────────────────────────────

def _load_corp_map() -> dict[str, str]:
    global _corp_map
    if _corp_map:
        return _corp_map
    try:
        r = requests.get(
            f"{_BASE}/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=30,
        )
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
    단일회사 주요계정 재무제표 조회
    reprt_code: 11011=사업보고서, 11012=반기, 11013=1분기, 11014=3분기
    """
    for fs_div in ("CFS", "OFS"):
        try:
            r = requests.get(
                f"{_BASE}/fnlttSinglAcnt.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "corp_code": corp_code,
                    "bsns_year": str(year),
                    "reprt_code": reprt_code,
                    "fs_div": fs_div,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                return data["list"]
        except Exception as e:
            logger.warning("DART 재무제표 조회 실패 (%s/%s/%s): %s",
                           corp_code, year, fs_div, e)
    return []


def extract_financials(statements: list[dict]) -> dict[str, int]:
    """재무제표 리스트에서 주요 항목 금액 추출 (단위: 원)"""
    result: dict[str, int] = {}

    for label, names in _NAME_MAP.items():
        sj_hints = _SJ_HINT.get(label, ())

        for item in statements:
            sj_div = item.get("sj_div", "")
            if sj_hints and sj_div not in sj_hints:
                continue

            account_nm = item.get("account_nm") or ""
            if not any(n in account_nm for n in names):
                continue

            # thstrm_amount 우선, 없으면 thstrm_add_amount(누계)
            raw = (item.get("thstrm_amount") or
                   item.get("thstrm_add_amount") or "0")
            raw = raw.replace(",", "").replace(" ", "").strip()
            if raw in ("", "-", "N/A"):
                continue
            try:
                result[label] = int(raw)
                break
            except ValueError:
                pass

    return result


def get_multi_year_financials(stock_code: str, years: int = 3) -> list[dict]:
    """최근 N년치 연간 재무 데이터 수집 + 당해 최신 분기 포함"""
    corp_code = get_corp_code(stock_code)
    if not corp_code:
        logger.warning("기업코드 없음: %s", stock_code)
        return []

    now = datetime.now()
    current_year = now.year
    current_month = now.month

    result = []

    # ── 당해 최신 분기 먼저 시도 ───────────────────────────
    quarterly_candidates: list[tuple[int, str]] = []
    if current_month >= 11:
        quarterly_candidates = [(current_year, "11014"), (current_year, "11012")]
    elif current_month >= 8:
        quarterly_candidates = [(current_year, "11012"), (current_year, "11013")]
    elif current_month >= 5:
        quarterly_candidates = [(current_year, "11013")]

    for y, reprt in quarterly_candidates:
        stmts = get_financial_statements(corp_code, y, reprt)
        if stmts:
            fin = extract_financials(stmts)
            if fin:
                fin["year"] = y
                fin["period"] = _reprt_label(reprt)
                result.append(fin)
                break

    # ── 직전 N년 사업보고서 ───────────────────────────────
    base = current_year - 1   # 전년도부터 시작
    for y in range(base, base - years, -1):
        stmts = get_financial_statements(corp_code, y)
        if stmts:
            fin = extract_financials(stmts)
            if fin:
                fin["year"] = y
                fin["period"] = "연간"
                result.append(fin)

    return result


def _reprt_label(code: str) -> str:
    return {"11011": "연간", "11012": "반기", "11013": "1분기", "11014": "3분기"}.get(code, code)
