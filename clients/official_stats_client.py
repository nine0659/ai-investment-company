"""
clients/official_stats_client.py
키 없이 쓸 수 있는 공식 통계 수집 — 미국 재무부(일간 국채수익률), 뉴욕연준(SOFR), BIS(각국 정책금리),
OECD(경기선행지수), 미국 노동통계국 BLS(CPI·실업률·비농업고용).

각 함수는 {"as_of": "YYYY-MM-DD" 또는 "YYYY-MM", ...} 형태의 원자료를 돌려주고, 실패하면 빈 dict/list(예외 없음).
계산(변화폭·YoY·스프레드)과 신선도 판정은 services/macro_dashboard.py가 한다 — 여기선 값만 가져온다.

신선도 주의: ECB HICP(ICP) 데이터셋은 2025-11에서 멈춘 폐기 시리즈였다(2026-02 방법론 변경). 그래서 쓰지 않는다.
시리즈를 추가할 땐 as_of가 실제로 최근인지 먼저 확인할 것.
"""
import csv
import io
import logging
import re
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
_T = 25

_TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
             "?data=daily_treasury_yield_curve&field_tdr_date_value_month={ym}")
_TREASURY_FIELDS = {"3M": "BC_3MONTH", "2Y": "BC_2YEAR", "5Y": "BC_5YEAR", "10Y": "BC_10YEAR", "30Y": "BC_30YEAR"}


def _get(url: str, timeout: int = _T, **kw):
    r = requests.get(url, headers=_H, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def fetch_treasury_curve(months: int = 2) -> list[dict]:
    """최근 영업일부터 과거 순서가 아닌 '오래된→최신' 순 일간 수익률 [{date, 3M, 2Y, 5Y, 10Y, 30Y}] (단위 %)."""
    rows: dict[str, dict] = {}
    now = datetime.now()
    for i in range(months):
        d = (now.replace(day=1) - timedelta(days=30 * i))
        ym = d.strftime("%Y%m")
        try:
            xml = _get(_TREASURY.format(ym=ym)).text
        except Exception as e:
            logger.warning("[공식통계] 재무부 %s 실패: %s", ym, e)
            continue
        for block in re.findall(r"<m:properties>(.*?)</m:properties>", xml, re.S):
            dm = re.search(r"<d:NEW_DATE[^>]*>(\d{4}-\d{2}-\d{2})", block)
            if not dm:
                continue
            rec = {"date": dm.group(1)}
            for label, field in _TREASURY_FIELDS.items():
                m = re.search(rf"<d:{field}[^>]*>([\d.]+)<", block)
                rec[label] = float(m.group(1)) if m else None
            rows[rec["date"]] = rec
    return [rows[k] for k in sorted(rows)]


def fetch_sofr() -> dict:
    try:
        j = _get("https://markets.newyorkfed.org/api/rates/secured/sofr/last/2.json").json()
        ref = (j.get("refRates") or [])
        if not ref:
            return {}
        cur = ref[0]
        return {"as_of": cur.get("effectiveDate"), "rate": cur.get("percentRate"),
                "prev": ref[1].get("percentRate") if len(ref) > 1 else None}
    except Exception as e:
        logger.warning("[공식통계] SOFR 실패: %s", e)
        return {}


def fetch_bis_policy_rates(areas=("KR", "US", "JP", "XM", "CN", "GB"), n: int = 5) -> dict:
    """BIS 중앙은행 정책금리(월별). {지역: [(YYYY-MM, 값), ... 오래된→최신]}"""
    try:
        txt = _get(f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/M.{'+'.join(areas)}"
                   f"?lastNObservations={n}&format=csv").text
        out: dict[str, list] = {}
        for row in csv.DictReader(io.StringIO(txt)):
            try:
                out.setdefault(row["REF_AREA"], []).append((row["TIME_PERIOD"], float(row["OBS_VALUE"])))
            except (KeyError, ValueError):
                continue
        return {k: sorted(v) for k, v in out.items()}
    except Exception as e:
        logger.warning("[공식통계] BIS 정책금리 실패: %s", e)
        return {}


def fetch_oecd_cli(areas=("KOR", "USA", "CHN", "JPN"), n: int = 3) -> dict:
    """OECD 경기선행지수(CLI, 100=장기추세). {국가: [(YYYY-MM, 값), ...]}"""
    try:
        txt = _get("https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/"
                   f"{'+'.join(areas)}.M.LI...AA...H?lastNObservations={n}&format=csvfilewithlabels").text
        out: dict[str, list] = {}
        for row in csv.DictReader(io.StringIO(txt)):
            try:
                out.setdefault(row["REF_AREA"], []).append((row["TIME_PERIOD"], float(row["OBS_VALUE"])))
            except (KeyError, ValueError):
                continue
        return {k: sorted(v) for k, v in out.items()}
    except Exception as e:
        logger.warning("[공식통계] OECD CLI 실패: %s", e)
        return {}


def _key(name: str) -> str:
    """API 키는 환경변수(.env는 db.database가 자체 로드). 없으면 빈 문자열 → 해당 수집기는 건너뛴다."""
    import os
    return (os.getenv(name) or "").strip()


def _ecos_rows(stat: str, cycle: str, start: str, end: str, item: str) -> list[tuple[str, float]]:
    k = _key("ECOS_API_KEY")
    url = f"https://ecos.bok.or.kr/api/StatisticSearch/{k}/json/kr/1/400/{stat}/{cycle}/{start}/{end}/{item}"
    rows = _get(url).json().get("StatisticSearch", {}).get("row", [])
    out = []
    for x in rows:
        t = x.get("TIME", "")
        t = f"{t[:4]}-{t[4:6]}" + (f"-{t[6:8]}" if len(t) == 8 else "")
        try:
            out.append((t, float(x["DATA_VALUE"])))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out)


def fetch_ecos() -> dict:
    """한국은행 ECOS — 기준금리·소비자물가지수·경상수지·수출금액·원/달러. {이름: [(기준일, 값), ... 오래된→최신]}.
    키가 없으면 {}. 한 통계가 실패해도 나머지는 돌려준다."""
    if not _key("ECOS_API_KEY"):
        return {}
    now = datetime.now()
    m_end = now.strftime("%Y%m")
    m_start = f"{now.year - 2}{now.month:02d}"
    specs = {
        "base_rate": ("722Y001", "M", m_start, m_end, "0101000"),
        "cpi": ("901Y009", "M", m_start, m_end, "0"),                 # 소비자물가지수 총지수(2020=100)
        "current_account": ("301Y013", "M", m_start, m_end, "000000"),  # 백만달러
        "exports": ("901Y118", "M", m_start, m_end, "T002"),           # 수출금액 천불
        "usdkrw": ("731Y001", "D", (now - timedelta(days=21)).strftime("%Y%m%d"), now.strftime("%Y%m%d"), "0000001"),
    }
    out = {}
    for name, args in specs.items():
        try:
            pts = _ecos_rows(*args)
            if pts:
                out[name] = pts
        except Exception as e:
            logger.warning("[공식통계] ECOS %s 실패: %s", name, str(e).replace(_key("ECOS_API_KEY"), "***")[:120])
    return out


_FRED_SERIES = {"VIXCLS": "vix", "BAMLH0A0HYM2": "hy_spread", "DFF": "fed_funds", "DTWEXBGS": "dollar_index",
                "ICSA": "jobless_claims", "UMCSENT": "sentiment"}


def fetch_fred(n: int = 6) -> dict:
    """FRED — VIX, 하이일드 스프레드, 연방기금 실효금리, 달러지수, 신규실업수당청구, 미시간 소비자심리.
    {이름: [(기준일, 값), ... 오래된→최신]}. 결측('.')은 제외. 키가 없으면 {}."""
    k = _key("FRED_API_KEY")
    if not k:
        return {}
    out = {}
    for sid, name in _FRED_SERIES.items():
        try:
            j = _get("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": sid, "api_key": k, "file_type": "json", "limit": n + 4,
                             "sort_order": "desc"}).json()
            pts = []
            for o in j.get("observations", []):
                try:
                    pts.append((o["date"], float(o["value"])))
                except (KeyError, ValueError):
                    continue
            if pts:
                out[name] = sorted(pts[:n])
        except Exception as e:
            logger.warning("[공식통계] FRED %s 실패: %s", sid, str(e).replace(k, "***")[:120])
    return out


def fetch_eia() -> dict:
    """미국 에너지정보청 — WTI·브렌트 현물가(일간), 미국 원유 재고(주간, 천배럴).
    {이름: [(기준일, 값), ...]}. 응답이 느릴 수 있어 3건을 병렬로 받는다. 키가 없으면 {}."""
    k = _key("EIA_API_KEY")
    if not k:
        return {}
    specs = {"wti": ("petroleum/pri/spt", "RWTC", "daily"), "brent": ("petroleum/pri/spt", "RBRTE", "daily"),
             "crude_stocks": ("petroleum/stoc/wstk", "WCESTUS1", "weekly")}
    def one(item):
        name, (route, sid, freq) = item
        try:
            p = {"api_key": k, "frequency": freq, "data[0]": "value", "facets[series][]": sid,
                 "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 6}
            data = _get(f"https://api.eia.gov/v2/{route}/data/", params=p, timeout=45).json()["response"]["data"]
            pts = []
            for x in data:
                try:
                    pts.append((x["period"], float(x["value"])))
                except (KeyError, ValueError, TypeError):
                    continue
            return name, sorted(pts)
        except Exception as e:
            logger.warning("[공식통계] EIA %s 실패: %s", sid, str(e).replace(k, "***")[:120])
            return name, []

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        return {n: pts for n, pts in ex.map(one, specs.items()) if pts}


_BLS_SERIES = {"CUUR0000SA0": "cpi_index", "LNS14000000": "unemployment", "CES0000000001": "payrolls"}


def fetch_bls() -> dict:
    """BLS v1(키 없음, 일 25회 제한 — 한 번에 3시리즈). {이름: [(YYYY-MM, 값), ... 오래된→최신]}"""
    try:
        y = datetime.now().year
        r = requests.post("https://api.bls.gov/publicAPI/v1/timeseries/data/", headers=_H, timeout=_T,
                          json={"seriesid": list(_BLS_SERIES), "startyear": str(y - 2), "endyear": str(y)})
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "REQUEST_SUCCEEDED":
            logger.warning("[공식통계] BLS 응답: %s", str(j.get("message"))[:150])
            return {}
        out = {}
        for s in (j.get("Results") or {}).get("series", []):
            name = _BLS_SERIES.get(s.get("seriesID"))
            pts = []
            for d in s.get("data", []):
                if d.get("period", "").startswith("M") and d["period"] != "M13":
                    try:
                        pts.append((f"{d['year']}-{d['period'][1:]}", float(d["value"])))
                    except ValueError:
                        continue
            if name:
                out[name] = sorted(pts)
        return out
    except Exception as e:
        logger.warning("[공식통계] BLS 실패: %s", e)
        return {}
