"""SEC 내부자 거래(Form 3/4/5) 분기 데이터셋 클라이언트 — 미국 종목의 '내부자가 자기 돈으로 샀는가' 신호용.

출처: https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets (무료, 공식).
함정: 폴더 이름이 분기마다 바뀐다(structureddata → datastandardsinnovation) → 고정 URL이 아니라 페이지에서 링크를 찾는다.
한계: 분기 단위로 공개되어 가장 최근 약 3주는 빠져 있다 → 반환값에 data_through(데이터가 닿는 마지막 접수일)를 넣는다.
모든 함수는 실패 시 빈 값을 반환하고 예외를 올리지 않는다(호출부는 이 신호 없이 진행).
"""
import csv
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
_SEC = {"User-Agent": "ai-investment-research nine0659@gmail.com", "Accept-Encoding": "gzip, deflate"}
_LINK = re.compile(r'href="(/files/[^"]*?/(\d{4})q([1-4])_form345\.zip)"')
_CACHE: dict = {"ts": 0.0, "key": None, "val": None}
_CACHE_TTL = 6 * 3600


def _d(s: str):
    try:
        return datetime.strptime((s or "").strip().upper(), "%d-%b-%Y").date()
    except ValueError:
        return None


def latest_zip_urls(n: int = 2) -> list[str]:
    """가장 최근 분기 n개의 zip 주소(최신순). 실패 시 []."""
    try:
        import requests
        r = requests.get(_PAGE, headers=_SEC, timeout=30)
        r.raise_for_status()
        found = {(int(y), int(q)): "https://www.sec.gov" + path for path, y, q in _LINK.findall(r.text)}
        return [found[k] for k in sorted(found, reverse=True)[:n]]
    except Exception as e:
        logger.warning("[내부자거래] 데이터셋 목록 조회 실패: %s", e)
        return []


def _read(z: zipfile.ZipFile, name: str):
    """TSV를 한 줄씩 스트리밍으로 읽는다. (이전: 파일 전체를 decode→StringIO로 복사해 분기당 순간 +150MB — 2026-10-22 실측,
    Render 무료 플랜 512MB에서 위험해 스트리밍으로 교체)"""
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline=""), delimiter="	")


def parse_zip(content: bytes) -> list[dict]:
    """zip → 공개시장 매수(P) 거래 목록 [{cik, symbol, owner, role, date, filed, value, shares, price, plan}].

    심볼은 종목이 바뀌면 충돌하므로(티커 재사용) 발행사 CIK로 집계한다.
    메모리: 거래 파일을 먼저 훑어 '공개시장 매수'의 접수번호만 모으고(수천 건), 제출·보고자 파일은 그 접수번호만 보관한다.
    """
    z = zipfile.ZipFile(io.BytesIO(content))
    trans = []
    for t in _read(z, "NONDERIV_TRANS.tsv"):
        if t.get("TRANS_CODE") != "P" or t.get("TRANS_ACQUIRED_DISP_CD") != "A":
            continue
        try:
            shares, price = float(t["TRANS_SHARES"]), float(t["TRANS_PRICEPERSHARE"])
        except (TypeError, ValueError):
            continue
        if shares > 0 and price > 0:
            trans.append((t["ACCESSION_NUMBER"], t.get("TRANS_DATE"), shares, price))
    wanted = {x[0] for x in trans}
    subs = {r["ACCESSION_NUMBER"]: {k: r.get(k) for k in ("DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL", "FILING_DATE", "AFF10B5ONE")}
            for r in _read(z, "SUBMISSION.tsv") if r["ACCESSION_NUMBER"] in wanted}
    owners: dict[str, list[tuple[str, str, str]]] = {}
    for r in _read(z, "REPORTINGOWNER.tsv"):
        if r["ACCESSION_NUMBER"] in wanted:
            owners.setdefault(r["ACCESSION_NUMBER"], []).append(
                (r["RPTOWNERCIK"], r["RPTOWNERNAME"], (r["RPTOWNER_RELATIONSHIP"] or "") + "|" + (r["RPTOWNER_TITLE"] or "")))
    out = []
    for acc, tdate, shares, price in trans:
        sub = subs.get(acc)
        if not sub or sub.get("DOCUMENT_TYPE") not in ("4", "4/A"):
            continue
        try:
            cik = int(sub.get("ISSUERCIK") or 0)
        except ValueError:
            cik = 0
        if not cik:
            continue                                           # 발행사를 특정할 수 없으면 쓰지 않는다
        for ocik, name, role in owners.get(acc, [("", "", "")])[:1]:   # 거래는 한 번만 센다
            out.append({"cik": cik, "symbol": (sub.get("ISSUERTRADINGSYMBOL") or "").strip().upper(),
                        "owner": ocik or name, "role": role, "date": _d(tdate),
                        "filed": _d(sub.get("FILING_DATE")), "shares": shares, "price": price,
                        "value": shares * price, "plan": sub.get("AFF10B5ONE") == "1"})
    return out


def fetch_recent_purchases(days: int = 90, today=None) -> dict:
    """{'by_cik': {발행사CIK: {buyers, value, last, holder_value}}, 'data_through': date|None, 'ok': bool}.

    buyers=서로 다른 임원·이사 매수자 수, value=그들의 매수 금액 합($), last=마지막 매수일,
    holder_value=10% 대주주·기타 법인의 매수 금액($, 신호에는 쓰지 않고 참고만 — 대형 지분거래가 임원 매수처럼 보이는 걸 막는다).
    """
    now = time.time()
    if _CACHE["val"] is not None and now - _CACHE["ts"] < _CACHE_TTL and _CACHE["key"] == days:
        return _CACHE["val"]
    urls = latest_zip_urls(2)
    if not urls:
        return {"by_cik": {}, "data_through": None, "ok": False}
    import requests
    rows: list[dict] = []
    for u in urls:
        try:
            r = requests.get(u, headers=_SEC, timeout=180)
            r.raise_for_status()
            rows += parse_zip(r.content)
        except Exception as e:
            logger.warning("[내부자거래] %s 수신/해석 실패: %s", u.rsplit("/", 1)[-1], e)
    if not rows:
        return {"by_cik": {}, "data_through": None, "ok": False}
    through = max((r["filed"] for r in rows if r["filed"]), default=None)
    today = today or datetime.now().date()
    since = today - timedelta(days=days)
    agg: dict[int, dict] = {}
    for r in rows:
        if not r["date"] or r["date"] < since or r["plan"]:
            continue
        a = agg.setdefault(r["cik"], {"owners": set(), "value": 0.0, "last": None, "holder_value": 0.0})
        if re.search(r"Officer|Director", r["role"], re.IGNORECASE):
            a["owners"].add(r["owner"])
            a["value"] += r["value"]
            a["last"] = max(a["last"], r["date"]) if a["last"] else r["date"]
        else:
            a["holder_value"] += r["value"]
    by_cik = {s: {"buyers": len(a["owners"]), "value": a["value"], "last": a["last"],
                     "holder_value": a["holder_value"]} for s, a in agg.items() if a["owners"] or a["holder_value"]}
    val = {"by_cik": by_cik, "data_through": through, "ok": True}
    _CACHE.update(ts=now, key=days, val=val)
    return val
