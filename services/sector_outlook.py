"""업종 전망 엔진 — 업종별 '시장 대비 우호/비우호' 시나리오·확률·틀리는 조건을 기록하고 20거래일 뒤 코드가 채점한다 (2026-10-23).

근거는 업종 온도계(industry_daily)에서 코드가 계산한 값뿐이다: 컨센서스 PER 할인, 이익 기대 성장, 외국인 매수 비율,
시장 대비 폭 변화. 한국 2년 검증에서 가격 모멘텀은 예측력이 없었으므로 모멘텀은 근거에서 *제외*하고 '현재 상태'로만 표시한다.
LLM(다른 계열 모델)은 주어진 숫자 위에서 판단·서술만 하고, 근거에 없는 숫자를 쓴 판단은 코드가 폐기한다.
채점: 업종 구성원 수익률 중앙값 - 전 종목 중앙값(같은 두 스냅샷 날짜) — screen_scorecard와 같은 기준선.
새 발송 경로 없음 — CEO 컨텍스트·봇 컨텍스트에만 주입되고, 표본이 30건 미만이면 '표본 부족'을 밝힌다.
"""
import json
import logging
import re
import statistics
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

HORIZON = 20                 # 채점까지 스냅샷(거래일) 수
HIT_BAND = 1.0               # %p — 이 이내의 상대수익률은 '우호/비우호' 어느 쪽도 증명하지 못한다(미적중 처리)
MIN_MEMBERS = 5
MIN_TRUSTED = 30             # 채점된 판단이 이보다 적으면 '표본 부족'
MAX_PER_SIDE = 4
_MAX_AGE_DAYS = 5

_SYSTEM = """당신은 한국 업종의 향후 20거래일 '시장 전체 대비' 성과를 판단하는 독립 분석가입니다.
아래는 코드가 계산한 업종별 근거입니다. 이 숫자만 근거로 삼으세요.

[규칙]
- 우호(시장보다 나을 것) 최대 4개, 비우호(시장보다 못할 것) 최대 4개만 고르세요. 확신 없는 업종은 고르지 마세요(비워둬도 됩니다).
- 가격 모멘텀(최근 수익률·이평선)은 예측력이 검증되지 않았으니 근거로 쓰지 마세요. 밸류에이션·이익 기대·외국인 수급 변화로만 판단하세요.
- 확률은 '그 판단이 맞을 확률'(50~75). 근거가 약하면 50~55. 표본이 작은 성적표가 있으면 과신하지 마세요.
- 근거 문장에는 입력에 있는 숫자만 쓰세요. 입력에 없는 수치·사건을 만들지 마세요.
- '틀리는조건'은 이 판단이 틀렸다고 볼 구체적 신호 한 문장(기준 숫자는 새로 정해도 됩니다).
JSON 객체만 출력(설명·코드블록 금지):
{"판단":[{"업종번호":"123","전망":"우호|비우호","확률":58,"근거":"...","틀리는조건":"..."}]}"""


# ── 근거 계산 (LLM 없음) ──────────────────────────────────────────────

def _f(v, fmt="{:+.1f}"):
    return fmt.format(v) if isinstance(v, (int, float)) else "N/A"


def evidence_rows(snaps: list[tuple[str, list[dict]]]) -> tuple[str, list[dict], str]:
    """(날짜, 업종별 근거 dict 목록, 근거 텍스트). 스냅샷이 없거나 시장 행이 없으면 ('', [], '')."""
    from services.industry_board import MARKET_KEY
    if not snaps or not snaps[0][1]:
        return "", [], ""
    date, cur_all = snaps[0]
    mkt = next((i for i in cur_all if i["ind_no"] == MARKET_KEY), None)
    if not mkt:
        return "", [], ""
    old_map = {i["ind_no"]: i for i in snaps[-1][1]} if len(snaps) > 1 else {}
    rows, lines = [], []
    for i in cur_all:
        if i["ind_no"] == MARKET_KEY or (i.get("n") or 0) < MIN_MEMBERS:
            continue
        per_disc = ((i["med_cns_per"] / mkt["med_cns_per"] - 1) * 100
                    if i.get("med_cns_per") and mkt.get("med_cns_per") else None)
        growth_rel = (i["med_growth"] - mkt["med_growth"]
                      if i.get("med_growth") is not None and mkt.get("med_growth") is not None else None)
        fb_rel = (i["foreign_buy_pct"] - mkt["foreign_buy_pct"]
                  if i.get("foreign_buy_pct") is not None and mkt.get("foreign_buy_pct") is not None else None)
        old = old_map.get(i["ind_no"])
        b200_chg = (i["pct_above_ma200"] - old["pct_above_ma200"]
                    if old and i.get("pct_above_ma200") is not None and old.get("pct_above_ma200") is not None else None)
        r = {"ind_no": i["ind_no"], "name": i["name"], "n": i["n"], "per_disc": per_disc, "growth_rel": growth_rel,
             "fb_rel": fb_rel, "b200_chg": b200_chg, "regime": i["regime"]}
        rows.append(r)
        lines.append(f"{i['ind_no']} {i['name']}(종목 {i['n']}) | 컨센서스PER 시장대비 {_f(per_disc)}% | 이익기대 시장대비 {_f(growth_rel)}%p"
                     f" | 외국인매수비율 시장대비 {_f(fb_rel)}%p | 200일선위 비율 변화 {_f(b200_chg)}%p | 현재 상태 {i['regime']}")
    head = (f"[업종 근거 {date} · 시장 중앙값: 컨센서스PER {_f(mkt.get('med_cns_per'), '{:.1f}')}배, "
            f"이익기대 {_f(mkt.get('med_growth'))}%]")
    return date, rows, head + "\n" + "\n".join(lines)


# ── LLM 호출 + 검증 ───────────────────────────────────────────────────

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers_ok(sentence: str, evidence: str) -> bool:
    """문장에 쓴 숫자가 모두 근거 텍스트에 있어야 한다(없는 수치 창작 차단)."""
    have = set(_NUM.findall(evidence))
    return all(n in have for n in _NUM.findall(sentence or ""))


def _load_items(raw: str) -> list:
    """응답에서 판단 객체 목록을 꺼낸다. 전체 JSON이 깨져도(출력이 길어 중간에 잘린 경우 등) 완결된 객체는 살린다."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("판단") or []
        return items if isinstance(items, list) else []
    except (ValueError, AttributeError):
        pass
    out = []
    for om in re.finditer(r"\{[^{}]*\}", raw or ""):
        try:
            d = json.loads(om.group(0))
        except ValueError:
            continue
        if isinstance(d, dict) and "업종번호" in d:
            out.append(d)
    return out


def parse_views(raw: str, rows: list[dict], evidence: str) -> list[dict]:
    """LLM JSON → 검증된 판단 목록. 형식 불량·미지의 업종·창작 숫자·중복은 개별 폐기. 변별 불가능한 전체 불량이면 []."""
    items = _load_items(raw)
    by_no = {r["ind_no"]: r for r in rows}
    out, seen, count = [], set(), {"우호": 0, "비우호": 0}
    for it in items if isinstance(items, list) else []:
        try:
            no, view, prob = str(it["업종번호"]).strip(), str(it["전망"]).strip(), float(it["확률"])
        except (KeyError, ValueError, TypeError):
            continue
        reason, wrong_if = str(it.get("근거", ""))[:200], str(it.get("틀리는조건", ""))[:200]
        if no not in by_no or no in seen or view not in count or not (50 <= prob <= 80) or count[view] >= MAX_PER_SIDE:
            continue
        # 숫자 대조는 '근거'에만 건다. '틀리는조건'의 기준값(예: PER 50% 이상 상승)은 모델이 새로 정하는 조건이지 사실 주장이 아니다
        # (2026-10-23 실모델 시험: 조건까지 대조했더니 유효 판단 0건 — 매일 빈 기록이 쌓일 뻔함).
        if not reason or not wrong_if or not _numbers_ok(reason, evidence):
            continue
        seen.add(no)
        count[view] += 1
        out.append({"ind_no": no, "name": by_no[no]["name"], "view": view, "prob": prob,
                    "reason": reason, "wrong_if": wrong_if})
    return out


def make_views(evidence: str, rows: list[dict], scorecard_line: str = "", chat_fn=None) -> list[dict]:
    if chat_fn is None:
        from clients.openai_client import chat_independent as chat_fn
    user = evidence + (f"\n\n[지난 성적] {scorecard_line}" if scorecard_line else "")
    return parse_views(chat_fn(_SYSTEM, user, max_tokens=2500), rows, evidence)


# ── 기록 ──────────────────────────────────────────────────────────────

def record_views(date: str, views: list[dict], evidence: str, model: str = "independent") -> int:
    from db.database import get_conn
    with get_conn() as conn:
        conn.execute(text("DELETE FROM sector_outlook WHERE date=:d"), {"d": date})
        for v in views:
            conn.execute(text(
                "INSERT INTO sector_outlook (date, ind_no, name, view, prob, horizon, reason, wrong_if, evidence, model) "
                "VALUES (:d,:no,:nm,:v,:p,:h,:re,:wi,:ev,:m)"),
                {"d": date, "no": v["ind_no"], "nm": v["name"], "v": v["view"], "p": v["prob"], "h": HORIZON,
                 "re": v["reason"], "wi": v["wrong_if"], "ev": evidence[:3000], "m": model})
    return len(views)


# ── 채점 (LLM 없음) ───────────────────────────────────────────────────

def score_hit(view: str, rel: float) -> int:
    """우호는 +HIT_BAND 초과, 비우호는 -HIT_BAND 미만일 때만 적중. 밴드 안은 미적중(증명 못 함)."""
    return int(rel > HIT_BAND) if view == "우호" else int(rel < -HIT_BAND)


def _relative_return(da: str, db_: str, ind_no: str) -> float | None:
    from db.database import get_conn
    with get_conn() as conn:
        rows = conn.execute(text(
            "SELECT a.industry, a.close, b.close FROM universe_screen_daily a JOIN universe_screen_daily b ON a.code=b.code "
            "WHERE a.date=:da AND b.date=:db AND a.close>0 AND b.close>0"), {"da": da, "db": db_}).fetchall()
    if len(rows) < 100:                                    # 비교 가능한 종목이 너무 적은 날(수집 이상)은 채점하지 않는다
        return None
    allr = [(c / a - 1) * 100 for _, a, c in rows]
    mem = [(c / a - 1) * 100 for ind, a, c in rows if ind == ind_no]
    if len(mem) < MIN_MEMBERS:
        return None
    return statistics.median(mem) - statistics.median(allr)


def score_pending(today: str | None = None) -> int:
    """HORIZON 거래일이 지난 미채점 판단을 채점한다. 채점 건수 반환."""
    from db.database import get_conn
    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(text("SELECT DISTINCT date FROM universe_screen_daily ORDER BY date ASC")).fetchall()]
        pend = conn.execute(text("SELECT id, date, ind_no, view, horizon FROM sector_outlook WHERE hit IS NULL")).fetchall()
    n = 0
    for pid, d, ind_no, view, hz in pend:
        if d not in dates:
            continue
        j = dates.index(d) + int(hz or HORIZON)
        if j >= len(dates):
            continue
        rel = _relative_return(d, dates[j], ind_no)
        if rel is None:
            continue
        with get_conn() as conn:
            conn.execute(text("UPDATE sector_outlook SET actual_rel=:r, hit=:h, scored_on=:s WHERE id=:i"),
                         {"r": rel, "h": score_hit(view, rel), "s": dates[j], "i": pid})
        n += 1
    return n


def scorecard() -> dict:
    from db.database import get_conn
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT view, prob, hit, date FROM sector_outlook WHERE hit IS NOT NULL")).fetchall()
    except Exception as e:
        logger.warning("[업종전망] 성적표 조회 실패: %s", e)
        return {"n": 0}
    n = len(rows)
    if not n:
        return {"n": 0}
    return {"n": n, "hit_rate": sum(r[2] for r in rows) / n * 100, "avg_prob": sum(r[1] for r in rows) / n,
            "n_dates": len({r[3] for r in rows}), "trusted": n >= MIN_TRUSTED}


def format_scorecard_line(sc: dict) -> str:
    if not sc.get("n"):
        return "업종 전망 채점 결과 아직 없음(첫 판단은 20거래일 뒤 채점)"
    tail = "" if sc["trusted"] else f" — 표본 {MIN_TRUSTED}건 미만이라 우연과 구분 불가(결론 금지)"
    return (f"업종 전망 {sc['n']}건 채점(서로 다른 {sc['n_dates']}일): 적중 {sc['hit_rate']:.0f}% "
            f"(평균 제시 확률 {sc['avg_prob']:.0f}%; 우연 기준 약 40~50%){tail}")


# ── 일일 실행 + 컨텍스트 ──────────────────────────────────────────────

def _has_industry(date: str) -> bool:
    from db.database import get_conn
    with get_conn() as conn:
        return bool(conn.execute(text(
            "SELECT COUNT(*) FROM universe_screen_daily WHERE date=:d AND industry IS NOT NULL AND industry<>''"),
            {"d": date}).scalar())


def run_daily(snaps=None, chat_fn=None) -> str:
    """universe_screen 잡 끝에서 호출. 채점 + (오늘 판단이 없으면) 새 판단 기록. 잡 detail에 붙일 한 줄을 반환(실패는 예외로)."""
    from services.industry_board import load_snapshots
    scored = score_pending()
    snaps = snaps if snaps is not None else load_snapshots(limit=6)
    date, rows, evidence = evidence_rows(snaps)
    if not rows:
        return f"업종전망: 근거 없음(채점 {scored})"
    if not _has_industry(date):
        return f"업종전망: 업종 코드 없는 스냅샷이라 판단 건너뜀(채점 {scored})"
    views = make_views(evidence, rows, format_scorecard_line(scorecard()), chat_fn=chat_fn)
    n = record_views(date, views, evidence)
    return f"업종전망 {n}건 기록 · 채점 {scored}건" + ("" if n else " ⚠유효 판단 0건")


def context_block(max_age_days: int = _MAX_AGE_DAYS) -> str:
    """CEO·봇 컨텍스트용 블록. 최근 판단이 없으면 ''."""
    from db.database import get_conn
    try:
        with get_conn() as conn:
            d = conn.execute(text("SELECT MAX(date) FROM sector_outlook")).scalar()
            if not d or (datetime.now(_KST).date() - datetime.strptime(d, "%Y-%m-%d").date()).days > max_age_days:
                return ""
            rows = conn.execute(text("SELECT name, view, prob, reason, wrong_if FROM sector_outlook WHERE date=:d "
                                     "ORDER BY view, prob DESC"), {"d": d}).fetchall()
    except Exception as e:
        logger.warning("[업종전망] 컨텍스트 조회 실패: %s", e)
        return ""
    if not rows:
        return ""
    lines = [f"[🧭 업종 전망 가설 ({d}, 향후 {HORIZON}거래일 시장 대비) — 독립 예측가(다른 계열 모델)의 가설이며 기록·채점 중. "
             f"{format_scorecard_line(scorecard())}. 확신도를 높이는 근거로 쓰지 말고 참고·반박 대상으로만 다뤄라. 아래 숫자만 인용]"]
    for name, view, prob, reason, wrong_if in rows:
        lines.append(f"- {name}: {view} {prob:.0f}% — {reason} / 틀리는 조건: {wrong_if}")
    return "\n".join(lines)
