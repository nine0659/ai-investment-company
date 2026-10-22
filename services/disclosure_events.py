"""시장 전체 DART 공시 사건 수집·분류 — 코스피·코스닥 전 종목의 주요사항·거래소공시를 쌓고, 본문의 숫자는 코드가 뽑는다.

기존 DART 연동(agents/dart_alert_agent.py)은 브리핑 때 '오늘 대형주(시총 5,000억+) 주요사항'만 한 번 훑고 끝났다 →
쌓이지 않고, 중소형주 사건은 못 보고, 스크리너·발굴과 연결되지 않았다. 여기서는
  ① 최근 N일 B(주요사항보고)·I(거래소공시) 전체 목록 → ② 제목 규칙으로 분류(호재/악재/중립, 무시할 것 제외)
  ③ 공급계약·잠정실적은 본문에서 금액·매출액대비·영업이익 증감률을 코드가 추출 → ④ dart_events에 upsert.
LLM 없음, 발송 없음. 해석은 쓰는 쪽(스크리너 신호·브리핑)의 몫이고, 수치는 전부 본문에서 온 것만 쓴다(절대원칙 2·3).
"""
import io
import logging
import re
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_BASE = "https://opendart.fss.or.kr/api"

# (정규식, 사건유형, 극성). 위에서부터 먼저 맞는 규칙을 쓴다. 정정공시는 원공시가 있으므로 제외한다.
_RULES: list[tuple[str, str, str]] = [
    # 위험 '해소' 공시가 아래 악재 규칙에 걸리지 않게 먼저 거른다 (실측: 거래정지해제·파산신청기각·상장유지결정이 악재로 잡혔다)
    (r"거래정지해제|파산신청기각|상장유지|이의신청", "위험해소·절차", "중립"),
    (r"상장폐지|관리종목지정|상장적격성실질심사대상|상장적격성\s*실질심사\s*대상|회생절차|파산신청|부도발생|영업정지", "상폐·부도위험", "악재"),
    (r"횡령|배임|회계처리기준위반|검찰고발", "횡령·배임·회계위반", "악재"),
    (r"불성실공시법인지정", "불성실공시", "악재"),
    (r"유상증자결정", "유상증자", "악재"),
    (r"전환사채권발행결정|신주인수권부사채권발행결정|교환사채권발행결정", "메자닌발행", "악재"),
    (r"감자결정", "감자", "악재"),
    (r"자기주식취득결정|자기주식취득신탁계약체결", "자사주취득", "호재"),
    (r"주식소각결정", "자사주소각", "호재"),
    (r"현금ㆍ현물배당결정|현금배당결정", "배당결정", "호재"),
    (r"단일판매ㆍ공급계약체결", "공급계약", "호재"),
    (r"영업\(잠정\)실적", "잠정실적", "중립"),
    (r"최대주주변경|최대주주등소유주식변동", "최대주주변경", "중립"),
    (r"회사합병결정|회사분할결정|분할합병|주식교환|영업양수|영업양도", "합병·분할·양수도", "중립"),
    (r"타법인주식및출자증권취득결정|타법인주식및출자증권처분결정", "타법인지분거래", "중립"),
    (r"소송등의제기|소송등의판결", "소송", "중립"),
]
_COMPILED = [(re.compile(p), t, pol) for p, t, pol in _RULES]
_EXCLUDE_TITLE = re.compile(r"정정|취소|철회")           # [기재정정]·[첨부정정]은 원공시와 중복


def classify(report_nm: str) -> tuple[str, str] | None:
    """제목 → (사건유형, 극성). 관심 없는 공시(IR 개최, 주총 소집, 전환가액 조정 등)와 정정공시는 None."""
    nm = (report_nm or "").strip()
    if not nm or _EXCLUDE_TITLE.search(nm):
        return None
    for rx, typ, pol in _COMPILED:
        if rx.search(nm):
            return typ, pol
    return None


# ── 본문 파싱 ──────────────────────────────────────────────────────────────

def _num(tok: str) -> float | None:
    if tok is None:
        return None
    t = tok.strip().replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    return float(t)


def doc_tokens(xml_text: str) -> list[str]:
    """DART 본문 XML → 표 셀 단위 토큰 목록."""
    no_style = re.sub(r"<style.*?</style>", " ", xml_text, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", "|", no_style)
    return [t.strip() for t in re.sub(r"\s+", " ", txt).split("|") if t.strip()]


def parse_supply_contract(tokens: list[str]) -> dict:
    """{amount(원), recent_sales(원), sales_pct, counterparty}. 못 찾은 값은 키가 없다."""
    out: dict = {}
    for i, t in enumerate(tokens[:-1]):
        nxt = _num(tokens[i + 1])
        if t.startswith("계약금액") and nxt is not None and "amount" not in out:
            out["amount"] = nxt
        elif t.startswith("최근매출액") and nxt is not None and "recent_sales" not in out:
            out["recent_sales"] = nxt
        elif t.startswith("매출액대비") and nxt is not None and "sales_pct" not in out:
            out["sales_pct"] = nxt
        elif re.match(r"(\d+\.\s*)?계약상대", t) and "counterparty" not in out and tokens[i + 1] not in ("-", ""):
            out["counterparty"] = tokens[i + 1]
    return out


def parse_earnings(tokens: list[str]) -> dict:
    """잠정실적 → {unit, period, rows:{매출액|영업이익|당기순이익: {cur, prev, qoq, base, yoy, qoq_flag, yoy_flag}}}."""
    out: dict = {"rows": {}}
    for t in tokens:
        m = re.match(r"단위\s*:\s*(\S+?)[,\s]", t + " ")
        if m:
            out["unit"] = m.group(1)
            break
    for i, t in enumerate(tokens):
        if t in ("매출액", "영업이익", "당기순이익") and t not in out["rows"] and tokens[i + 1:i + 2] == ["당해실적"]:
            v = tokens[i + 2:i + 9]
            if len(v) == 7:
                out["rows"][t] = {"cur": _num(v[0]), "prev": _num(v[1]), "qoq": _num(v[2]), "qoq_flag": v[3],
                                  "base": _num(v[4]), "yoy": _num(v[5]), "yoy_flag": v[6]}
    for i, t in enumerate(tokens):
        if t == "당기실적" and i + 3 < len(tokens) and tokens[i + 2] == "~":
            out["period"] = f"{tokens[i + 1]}~{tokens[i + 3]}"
            break
    return out


def summarize_supply(corp: str, d: dict) -> tuple[str, float | None]:
    if "amount" not in d:
        return "계약금액 본문에서 확인 못 함", None
    s = f"계약금액 {d['amount'] / 1e8:,.0f}억원"
    if "sales_pct" in d:
        s += f" · 최근매출액 대비 {d['sales_pct']:.1f}%"
    if d.get("counterparty"):
        s += f" · 상대 {d['counterparty'][:20]}"
    return s, d.get("sales_pct")


def summarize_earnings(d: dict) -> tuple[str, float | None]:
    op = d["rows"].get("영업이익")
    if not op or op.get("cur") is None:
        return "영업이익 본문에서 확인 못 함", None
    # 단위를 못 읽은 공시는 금액을 빼고 증감률만 쓴다 (단위 없는 숫자는 억/백만원을 혼동시킨다 — 2026-10-22 LS마린솔루션)
    parts = [f"영업이익 {op['cur']:,.0f}{d['unit']}"] if d.get("unit") else ["영업이익"]
    if op.get("yoy") is not None:
        parts.append(f"전년동기 대비 {op['yoy']:+.1f}%")
    elif op.get("yoy_flag") not in (None, "-"):
        parts.append(op["yoy_flag"])
    if op.get("qoq") is not None:
        parts.append(f"전분기 대비 {op['qoq']:+.1f}%")
    flags = [f for f in (op.get("yoy_flag"), op.get("qoq_flag")) if f and f != "-"]
    s = " · ".join(parts)
    if flags and any(("전환" in f or "지속" in f) for f in flags):
        s += f" [{'/'.join(sorted(set(flags)))}]"
    sales = d["rows"].get("매출액")
    if sales and sales.get("yoy") is not None:
        s += f" | 매출액 전년동기 대비 {sales['yoy']:+.1f}%"
    return s, op.get("yoy")


def earnings_polarity(ed: dict, yoy: float | None) -> str:
    """영업이익 기준: 흑자전환·전년동기 +30%↑=호재, 적자전환/지속·-30%↓=악재, 그 외 중립."""
    op = ed.get("rows", {}).get("영업이익") or {}
    flags = " ".join(str(f) for f in (op.get("yoy_flag"), op.get("qoq_flag")) if f)
    if "적자" in flags or (op.get("cur") is not None and op["cur"] < 0 and "전환" in flags):
        return "악재"
    if "흑자전환" in flags:
        return "호재"
    if yoy is not None:
        return "호재" if yoy >= 30 else "악재" if yoy <= -30 else "중립"
    return "중립"


# ── 수집 ───────────────────────────────────────────────────────────────────

def _list_page(key: str, bgn: str, end: str, ty: str, cls: str, page: int) -> dict:
    import requests
    r = requests.get(f"{_BASE}/list.json", params={
        "crtfc_key": key, "bgn_de": bgn, "end_de": end, "pblntf_ty": ty, "corp_cls": cls,
        "page_no": page, "page_count": 100}, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_disclosures(days: int = 3, today=None) -> dict:
    """{items:[list.json 항목], ok, errors}. B·I 유형 × 코스피·코스닥 전 페이지."""
    from config.settings import DART_API_KEY
    if not DART_API_KEY:
        return {"items": [], "ok": False, "errors": ["DART_API_KEY 미설정"]}
    today = today or datetime.now(_KST).date()
    bgn, end = (today - timedelta(days=days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
    items, errors = [], []
    for ty in ("B", "I"):
        for cls in ("Y", "K"):
            try:
                d = _list_page(DART_API_KEY, bgn, end, ty, cls, 1)
                if d.get("status") not in ("000", "013"):          # 013 = 조회된 데이터 없음
                    errors.append(f"{ty}/{cls}: {d.get('status')} {d.get('message')}")
                    continue
                items += d.get("list") or []
                for p in range(2, int(d.get("total_page") or 1) + 1):
                    items += _list_page(DART_API_KEY, bgn, end, ty, cls, p).get("list") or []
            except Exception as e:
                errors.append(f"{ty}/{cls}: {str(e)[:80]}")
    return {"items": items, "ok": len(errors) < 4, "errors": errors}


def _fetch_doc_tokens(rcept_no: str) -> list[str]:
    from config.settings import DART_API_KEY
    import requests
    r = requests.get(f"{_BASE}/document.xml", params={"crtfc_key": DART_API_KEY, "rcept_no": rcept_no}, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    return doc_tokens(z.read(z.namelist()[0]).decode("utf-8", "ignore"))


def build_event(item: dict, tokens: list[str] | None = None) -> dict | None:
    """list.json 항목 → 저장용 사건 dict (관심 밖이면 None). 상세는 tokens가 있을 때만."""
    cls = classify(item.get("report_nm"))
    code = (item.get("stock_code") or "").strip()
    if not cls or not code:
        return None
    typ, pol = cls
    summary, metric = "", None
    if tokens is not None:
        try:
            if typ == "공급계약":
                summary, metric = summarize_supply(item.get("corp_name", ""), parse_supply_contract(tokens))
                if metric is not None and metric < 5:
                    pol = "중립"                                     # 매출액 대비 5% 미만은 호재로 보지 않는다
            elif typ == "잠정실적":
                ed = parse_earnings(tokens)
                summary, metric = summarize_earnings(ed)
                pol = earnings_polarity(ed, metric)
        except Exception as e:
            logger.debug("[공시사건] 본문 해석 실패(%s): %s", item.get("rcept_no"), e)
    dt = item.get("rcept_dt") or ""
    return {"rcept_no": item["rcept_no"], "date": f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}", "stock_code": code,
            "corp_name": item.get("corp_name", ""), "market": "KOSPI" if item.get("corp_cls") == "Y" else "KOSDAQ",
            "report_nm": (item.get("report_nm") or "").strip(), "event_type": typ, "polarity": pol,
            "summary": summary, "metric": metric}


def _known_rcept_nos(nos: list[str]) -> set[str]:
    from db.database import get_conn
    from sqlalchemy import text
    if not nos:
        return set()
    with get_conn() as conn:
        rows = conn.execute(text("SELECT rcept_no FROM dart_events WHERE date>=:d"),
                            {"d": (datetime.now(_KST).date() - timedelta(days=14)).strftime("%Y-%m-%d")}).fetchall()
    return {r[0] for r in rows}


def save_events(events: list[dict]) -> int:
    from db.database import get_conn
    from sqlalchemy import text
    if not events:
        return 0
    with get_conn() as conn:
        for e in events:
            conn.execute(text(
                "INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, "
                "summary, metric) VALUES (:rcept_no, :date, :stock_code, :corp_name, :market, :report_nm, :event_type, "
                ":polarity, :summary, :metric) ON CONFLICT (rcept_no) DO NOTHING"), e)
    return len(events)


def collect(days: int = 3) -> dict:
    """공시 수집 → 분류 → (공급계약·잠정실적은 본문 해석) → 저장. 통계 dict 반환."""
    fetched = fetch_disclosures(days)
    classified = [(it, classify(it.get("report_nm"))) for it in fetched["items"] if it.get("stock_code")]
    relevant = [it for it, c in classified if c]
    known = _known_rcept_nos([it["rcept_no"] for it in relevant])
    fresh = [it for it in relevant if it["rcept_no"] not in known]
    events, doc_fail, doc_try = [], 0, 0
    for it in fresh:
        toks = None
        if classify(it["report_nm"])[0] in ("공급계약", "잠정실적"):
            doc_try += 1
            try:
                toks = _fetch_doc_tokens(it["rcept_no"])
            except Exception as e:
                doc_fail += 1
                logger.debug("[공시사건] 본문 수신 실패(%s): %s", it["rcept_no"], e)
        ev = build_event(it, toks)
        if ev:
            events.append(ev)
    saved = save_events(events)
    return {"ok": fetched["ok"], "errors": fetched["errors"], "listed": len(fetched["items"]),
            "relevant": len(relevant), "new": len(fresh), "saved": saved, "doc_try": doc_try, "doc_fail": doc_fail,
            "events": events}                                  # 이번에 처음 저장한 사건들 (보유종목 즉시 알림이 쓴다)


def run_daily_job() -> tuple[str, str]:
    """무발송 잡 본체. (status, detail) — 목록 실패=fail, 목록 0건=empty."""
    st = collect()
    if not st["ok"]:
        return "fail", f"DART 목록 조회 실패 {st['errors'][:2]}"
    if st["listed"] == 0:
        return "empty", "공시 목록 0건"
    if st["doc_try"] and st["doc_fail"] > st["doc_try"] * 0.5:
        return "fail", f"본문 수신 {st['doc_try'] - st['doc_fail']}/{st['doc_try']}만 성공"
    return "success", (f"목록 {st['listed']} · 관심 사건 {st['relevant']} · 신규 {st['new']} · "
                       f"본문 {st['doc_try'] - st['doc_fail']}/{st['doc_try']}")


# ── 조회 (스크리너·발굴·브리핑용) ──────────────────────────────────────────────

def recent_events(days: int = 7, polarity: str | None = None, codes: set[str] | None = None) -> list[dict]:
    from db.database import get_conn
    from sqlalchemy import text
    since = (datetime.now(_KST).date() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(text(
                "SELECT date, stock_code, corp_name, market, report_nm, event_type, polarity, summary, metric "
                "FROM dart_events WHERE date>=:s ORDER BY date DESC, id DESC"), {"s": since}).fetchall()
    except Exception as e:
        logger.warning("[공시사건] 조회 실패: %s", e)
        return []
    keys = ("date", "stock_code", "corp_name", "market", "report_nm", "event_type", "polarity", "summary", "metric")
    out = [dict(zip(keys, r)) for r in rows]
    if polarity:
        out = [e for e in out if e["polarity"] == polarity]
    if codes is not None:
        out = [e for e in out if e["stock_code"] in codes]
    return out


def by_code(days: int = 7) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in recent_events(days):
        out.setdefault(e["stock_code"], []).append(e)
    return out
