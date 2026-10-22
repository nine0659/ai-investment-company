"""한국 업종 온도계 — 전 종목 스크리닝 결과를 업종별로 모아 '어느 업종이 강하고, 어디서 바닥을 다지고, 어디가 약한가'를 코드로 계산한다.

기존 시장 분석은 지수·수급 한 줄 수준이었고 업종/테마 단위 근거가 없었다. 여기서는 종목 단위 지표(3개월 수익률, 50/200일선, 추정EPS,
외국인 매수)를 업종 중앙값/비율로 집계하고, 같은 숫자를 날마다 쌓아 *변화*(예: 200일선 위 종목 비율이 늘고 있나)를 본다.
분류 기준은 **시장 전체 대비**라 지수가 어디에 있든(전 종목이 고점 대비 -50%여도) 변별력을 유지한다. LLM 없음.
"""
import logging
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
MARKET_KEY = "ALL"                 # industry_daily에서 전체 시장 행을 가리키는 키
MIN_MEMBERS = 5                    # 우리 유니버스에서 이 수 미만인 업종은 중앙값이 불안정해 제외
# 시장 대비 분류 임계값(%p) — 초기 추정치. 성적이 쌓이면 조정한다.
_REL_RET = 5.0
_REL_BREADTH = 10.0


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _pct(flags):
    flags = list(flags)
    return sum(flags) / len(flags) * 100 if flags else None


def classify(ind: dict, mkt: dict) -> str:
    """시장 전체 대비 상대 기준으로 업종 국면 분류. 지표가 없으면 '중립'."""
    r3, b200, r1, b50 = ind["med_ret_3m"], ind["pct_above_ma200"], ind["med_ret_1m"], ind["pct_above_ma50"]
    if None in (r3, b200) or mkt["med_ret_3m"] is None or mkt["pct_above_ma200"] is None:
        return "중립"
    if r3 >= mkt["med_ret_3m"] + _REL_RET and b200 >= mkt["pct_above_ma200"] + _REL_BREADTH:
        return "강세"
    if r3 <= mkt["med_ret_3m"] - _REL_RET and b200 <= mkt["pct_above_ma200"] - _REL_BREADTH:
        return "약세"
    if r1 is not None and b50 is not None and r1 > 0 and b50 >= 50 and r3 < mkt["med_ret_3m"] + _REL_RET:
        return "회복초입"
    return "중립"


def _metrics(rows: list[dict]) -> dict:
    hs = [r["detail"]["hist"] for r in rows if (r.get("detail") or {}).get("hist")]
    ds = [r["detail"] for r in rows if r.get("detail")]
    growth = [(d["cns_eps"] / d["eps"] - 1) * 100 for d in ds if d.get("eps") and d["eps"] > 0 and d.get("cns_eps")]
    cp = [d["cns_per"] for d in ds if d.get("cns_per") and 0 < d["cns_per"] < 100]
    return {
        "med_ret_1m": _med(h.get("ret_1m") for h in hs), "med_ret_3m": _med(h.get("ret_3m") for h in hs),
        "pct_above_ma50": _pct(r["close"] > r["detail"]["hist"]["ma50"] for r in rows
                               if (r.get("detail") or {}).get("hist") and r["detail"]["hist"].get("ma50")),
        "pct_above_ma200": _pct(r["close"] > r["detail"]["hist"]["ma200"] for r in rows
                                if (r.get("detail") or {}).get("hist") and r["detail"]["hist"].get("ma200")),
        "med_cns_per": _med(cp), "med_growth": _med(max(-100.0, min(300.0, g)) for g in growth),
        "foreign_buy_pct": _pct(d["foreign_buy_days"] >= 2 for d in ds if d.get("flow_days", 0) >= 3),
    }


def build(rows: list[dict], groups: list[dict]) -> dict:
    """rows=상세·이력이 붙은 종목들, groups=네이버 업종 그룹 목록. {market:{...}, industries:[...]}."""
    names = {str(g["no"]): g for g in groups}
    by_ind: dict[str, list[dict]] = {}
    for r in rows:
        ind = (r.get("detail") or {}).get("industry")
        if ind:
            by_ind.setdefault(str(ind), []).append(r)
    mkt = _metrics(rows)
    mkt["n"] = len(rows)
    out = []
    for no, members in by_ind.items():
        if len(members) < MIN_MEMBERS:
            continue
        m = _metrics(members)
        g = names.get(no) or {}
        try:
            chg = float(g.get("changeRate")) if g.get("changeRate") is not None else None
        except (TypeError, ValueError):
            chg = None
        item = {"ind_no": no, "name": g.get("name") or f"업종{no}", "n": len(members), "change_rate": chg, **m}
        item["regime"] = classify(item, mkt)
        out.append(item)
    out.sort(key=lambda x: -(x["med_ret_3m"] if x["med_ret_3m"] is not None else -999))
    return {"market": mkt, "industries": out}


def save_daily(board: dict, date: str | None = None) -> int:
    from db.database import get_conn
    from sqlalchemy import text
    date = date or datetime.now(_KST).strftime("%Y-%m-%d")
    rows = [{"date": date, **{k: i[k] for k in ("ind_no", "name", "n", "change_rate", "med_ret_1m", "med_ret_3m",
                                                "pct_above_ma50", "pct_above_ma200", "med_cns_per", "med_growth",
                                                "foreign_buy_pct", "regime")}} for i in board["industries"]]
    m = board.get("market") or {}
    if m:                                                   # 전체 시장 폭 지표를 'ALL' 행으로 함께 쌓는다(시계열로 변화를 본다)
        rows.append({"date": date, "ind_no": MARKET_KEY, "name": "전체 시장", "n": m.get("n"), "change_rate": None,
                     "med_ret_1m": m.get("med_ret_1m"), "med_ret_3m": m.get("med_ret_3m"),
                     "pct_above_ma50": m.get("pct_above_ma50"), "pct_above_ma200": m.get("pct_above_ma200"),
                     "med_cns_per": m.get("med_cns_per"), "med_growth": m.get("med_growth"),
                     "foreign_buy_pct": m.get("foreign_buy_pct"), "regime": "-"})
    with get_conn() as conn:
        conn.execute(text("DELETE FROM industry_daily WHERE date=:d"), {"d": date})
        if rows:
            conn.execute(text(
                "INSERT INTO industry_daily (date, ind_no, name, n, change_rate, med_ret_1m, med_ret_3m, pct_above_ma50, "
                "pct_above_ma200, med_cns_per, med_growth, foreign_buy_pct, regime) VALUES (:date, :ind_no, :name, :n, "
                ":change_rate, :med_ret_1m, :med_ret_3m, :pct_above_ma50, :pct_above_ma200, :med_cns_per, :med_growth, "
                ":foreign_buy_pct, :regime)"), rows)
    return len(rows)


_KEYS = ("date", "ind_no", "name", "n", "change_rate", "med_ret_1m", "med_ret_3m", "pct_above_ma50", "pct_above_ma200",
         "med_cns_per", "med_growth", "foreign_buy_pct", "regime")


def load_snapshots(limit: int = 6) -> list[tuple[str, list[dict]]]:
    """최근 스냅샷 날짜들(최신순)과 업종 목록. 실패/없음 → []."""
    from db.database import get_conn
    from sqlalchemy import text
    try:
        with get_conn() as conn:
            dates = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT date FROM industry_daily ORDER BY date DESC LIMIT :n"), {"n": limit}).fetchall()]
            out = []
            for d in dates:
                rows = conn.execute(text(f"SELECT {', '.join(_KEYS)} FROM industry_daily WHERE date=:d"), {"d": d}).fetchall()
                out.append((d, [dict(zip(_KEYS, r)) for r in rows]))
        return out
    except Exception as e:
        logger.warning("[업종온도계] 스냅샷 조회 실패: %s", e)
        return []


def breadth_line(now: dict | None, prev: dict | None, prev_date: str | None = None) -> str:
    """시장 폭 한 줄 + 통상 해석(참고). 값이 없으면 ''. 해석은 확정 예측이 아니라 일반적으로 쓰이는 읽는 법이다."""
    if not now or now.get("pct_above_ma200") is None:
        return ""

    def with_prev(key, label):
        v = now.get(key)
        if v is None:
            return ""
        pv = prev.get(key) if prev else None
        return f"{label} {v:.0f}%" + (f"({prev_date[5:]} {pv:.0f}%)" if pv is not None and prev_date else "")
    parts = [x for x in (with_prev("pct_above_ma200", "200일선 위"), with_prev("pct_above_ma50", "50일선 위"),
                         with_prev("foreign_buy_pct", "외국인 매수 종목")) if x]
    line = f"📊 시장 폭 (전 종목 {now.get('n') or '?'}개 중): " + " · ".join(parts)
    b200, b50 = now["pct_above_ma200"], now.get("pct_above_ma50")
    p50 = prev.get("pct_above_ma50") if prev else None
    note = ""
    if b200 >= 75:
        note = "대부분 종목이 장기 추세 위 — 과열 구간일 수 있어 추격 매수 주의"
    elif b200 <= 25 and b50 is not None and p50 is not None and b50 - p50 >= 10:
        note = "장기 추세 아래 종목이 많은 가운데 단기 추세는 개선 중 — 바닥 다지기 초기일 수 있으나 확정 아님"
    elif b200 <= 25:
        note = "대부분 종목이 장기 추세 아래 — 단기 추세(50일선 위 비율) 개선이 확인되기 전엔 반등 신뢰도 낮음"
    return line + ("\n   ↳ 통상 해석(참고): " + note if note else "")


def _line(i: dict, prev: dict | None) -> str:
    bits = [f"{i['name']}({i['n']}종목)"]
    if i["med_ret_3m"] is not None:
        bits.append(f"3개월 중앙값 {i['med_ret_3m']:+.0f}%")
    if i["med_ret_1m"] is not None:
        bits.append(f"1개월 {i['med_ret_1m']:+.0f}%")
    if i["pct_above_ma200"] is not None:
        bits.append(f"200일선 위 {i['pct_above_ma200']:.0f}%")
    if i["med_growth"] is not None:
        bits.append(f"이익 증가 기대 {i['med_growth']:+.0f}%")
    if i["foreign_buy_pct"] is not None:
        bits.append(f"외국인 매수 종목 {i['foreign_buy_pct']:.0f}%")
    line = " · ".join(bits)
    if prev and prev.get("pct_above_ma50") is not None and i.get("pct_above_ma50") is not None:
        d50 = i["pct_above_ma50"] - prev["pct_above_ma50"]
        if abs(d50) >= 10:
            line += f" | 50일선 위 비율 {prev['pct_above_ma50']:.0f}%→{i['pct_above_ma50']:.0f}%({'개선' if d50 > 0 else '악화'})"
    return " · " + line


def format_board(snaps: list[tuple[str, list[dict]]] | None = None, top: int = 4) -> str:
    """업종 온도계 텍스트(≈800자). 오래된/빈 스냅샷이면 ''. 비교는 가장 오래된(최대 5거래일 전) 스냅샷 대비."""
    snaps = snaps if snaps is not None else load_snapshots()
    if not snaps or not snaps[0][1]:
        return ""
    date, cur_all = snaps[0]
    cur = [i for i in cur_all if i["ind_no"] != MARKET_KEY]
    mkt_now = next((i for i in cur_all if i["ind_no"] == MARKET_KEY), None)
    mkt_prev = next((i for i in snaps[-1][1] if i["ind_no"] == MARKET_KEY), None) if len(snaps) > 1 else None
    prev_map = {i["ind_no"]: i for i in snaps[-1][1]} if len(snaps) > 1 else {}
    cmp_note = f"{snaps[-1][0]} 대비 변화 표시" if len(snaps) > 1 else "비교할 과거 기록은 내일부터 쌓임"
    groups = {k: [i for i in cur if i["regime"] == k] for k in ("강세", "회복초입", "약세")}
    if not any(groups.values()) and not mkt_now:
        return ""
    lines = [f"🌡 업종 온도계 ({date} 기준, 시장 전체 대비 · 코드 계산 · {cmp_note})"]
    breadth = breadth_line(mkt_now, mkt_prev, snaps[-1][0] if len(snaps) > 1 else None)
    if breadth:
        lines.append(breadth)
    for label, key, rev in (("🔥 강세 업종", "강세", True), ("🌱 회복 초입 업종", "회복초입", True), ("🧊 약세 업종", "약세", False)):
        items = sorted(groups[key], key=lambda x: (x["med_ret_3m"] if x["med_ret_3m"] is not None else -999) if key != "회복초입"
                       else (x["med_ret_1m"] or -999), reverse=True if key != "약세" else False)[:top]
        if items:
            lines.append(label)
            lines += [_line(i, prev_map.get(i["ind_no"])) for i in items]
    return "\n".join(lines)
