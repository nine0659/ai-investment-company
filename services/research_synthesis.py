"""
services/research_synthesis.py

report_analysis에 쌓인 '검증된' 문서 분석을 모아 CEO·인텔리전스 팀이 읽을 컨텍스트로 만든다.
집계(증권사 수, 합의/엇갈림, 목표가 통계)는 전부 코드가 계산한다 — LLM은 숫자를 만들지 않는다.

합의가 곧 정답이 아니다: 증권사 의견이 한쪽으로 쏠리면 오히려 '쏠림 경고'로 표시한다.
"""
import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

MACRO_TYPES = ("경제분석", "투자전략", "산업분석")
CROWD_MIN_FIRMS = 3
CROWD_SHARE = 0.75


def _since(days: int) -> str:
    return (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")


def _rows(sql: str, params: dict) -> list:
    try:
        from db.database import get_conn
        with get_conn() as conn:
            return conn.execute(text(sql), params).fetchall()
    except Exception as e:
        logger.warning("[리서치종합] 조회 실패: %s", e)
        return []


# ── 순수 집계 ──────────────────────────────────────────────────────

def classify_consensus(stances: dict[str, int]) -> str:
    """{'강세':n,'중립':n,'약세':n} → 합의/쏠림/엇갈림 라벨."""
    total = sum(stances.values())
    if total == 0:
        return ""
    top, n_top = max(stances.items(), key=lambda kv: kv[1])
    if total >= CROWD_MIN_FIRMS and n_top / total >= CROWD_SHARE and top != "중립":
        return f"{top} 쏠림 — 다수가 같은 방향이면 반대 근거를 점검할 것"
    if stances.get("강세", 0) and stances.get("약세", 0):
        return "엇갈림"
    return "소수 의견" if total < CROWD_MIN_FIRMS else "중립 우세" if top == "중립" else "한쪽 우세(쏠림 기준 미달)"


_FORECAST_WORDS = ("전망", "예상", "목표", "추정", "컨센서스", "예측", "projection", "forecast", "target", "expect", "estimate", "foresee")


def is_forecast_figure(f: dict) -> bool:
    """전망치 여부는 LLM의 kind 라벨을 믿지 않고 라벨·인용에 전망 어휘가 있는지로 코드가 판정한다.
    (라벨 신뢰도가 낮아 '미사일 사거리'가 전망치로 분류된 사례가 있었다.)"""
    blob = f"{f.get('label', '')} {f.get('quote', '')}".lower()
    return f.get("kind") == "전망치" and any(w in blob for w in _FORECAST_WORDS)


def aggregate_macro(rows: list[dict]) -> dict:
    """rows: 분석 완료 문서(dict: firm, report_date, analysis). 테마별로 증권사당 1표.

    '전망' 성격의 claim만 투표에 쓴다(시황 사실 나열은 의견이 아님). 같은 증권사가 같은 테마에
    여러 번 말하면 가장 최근 문서의 다수 입장 1표.
    """
    votes: dict[tuple, tuple] = {}   # (테마, 증권사) → (날짜, 입장, claim 정보)
    for r in sorted(rows, key=lambda x: x.get("report_date") or ""):
        per_theme: dict[str, list] = defaultdict(list)
        for c in r["analysis"].get("claims") or []:
            if c.get("kind") == "전망" and c.get("stance") and c.get("theme") and c["theme"] != "기타":
                per_theme[c["theme"]].append(c)
        for th, cs in per_theme.items():
            tally = defaultdict(int)
            for c in cs:
                tally[c["stance"]] += 1
            st = max(tally.items(), key=lambda kv: kv[1])[0]
            votes[(th, r["firm"])] = (r.get("report_date") or "", st, cs[0])
    themes: dict[str, dict] = defaultdict(lambda: {"stances": defaultdict(int), "claims": []})
    for (th, firm), (_, st, c) in votes.items():
        themes[th]["stances"][st] += 1
        themes[th]["claims"].append((firm, c["claim"], c["quote"]))
    return {th: {"stances": dict(v["stances"]), "claims": v["claims"]} for th, v in themes.items()}


_OPINION_MAP = {
    "매수": ("buy", "strongbuy", "strong buy", "매수", "강력매수", "outperform", "overweight", "적극매수", "trading buy", "tradingbuy"),
    "중립": ("hold", "neutral", "중립", "marketperform", "market perform", "보유", "시장수익률", "marketweight"),
    "매도": ("sell", "매도", "underperform", "underweight", "reduce", "비중축소"),
}


def normalize_opinion(op: str | None) -> str:
    key = (op or "").strip().lower()
    for label, words in _OPINION_MAP.items():
        if key in words:
            return label
    return ""   # '없음'·NotRated 등은 의견 미표기로 취급


def target_price_stats(reports: list[dict]) -> dict:
    """종목 하나의 리포트들 → 증권사당 최신 1건 기준 투자의견·목표가 통계.

    목표가 변경은 '같은 증권사의 직전 리포트 목표가'와 비교해서만 센다
    (네이버 API의 prevGoalPrice는 이전 목표가가 아니라 작성일 주가라서 쓰면 안 된다).
    """
    by_firm_all: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(reports, key=lambda x: x.get("report_date") or ""):
        by_firm_all[r["firm"]].append(r)
    latest = [v[-1] for v in by_firm_all.values()]

    goals = [r["goal_price"] for r in latest if r.get("goal_price")]
    gaps = [(r["goal_price"] / r["price_at_write"] - 1) * 100 for r in latest
            if r.get("goal_price") and r.get("price_at_write")]
    ups = downs = same = 0
    for v in by_firm_all.values():
        prev = next((x["goal_price"] for x in reversed(v[:-1]) if x.get("goal_price")), None)
        cur = v[-1].get("goal_price")
        if prev and cur:
            if cur > prev:
                ups += 1
            elif cur < prev:
                downs += 1
            else:
                same += 1
    opinions: dict[str, int] = defaultdict(int)
    for r in latest:
        opinions[normalize_opinion(r.get("opinion")) or "의견 미표기"] += 1
    return {
        "n_firms": len(latest),
        "goal_median": int(statistics.median(goals)) if goals else None,
        "goal_min": min(goals) if goals else None,
        "goal_max": max(goals) if goals else None,
        "gap_median_pct": round(statistics.median(gaps), 1) if gaps else None,
        "ups": ups, "downs": downs, "same": same,
        "opinions": dict(opinions),
        "last_date": max((r.get("report_date") or "" for r in latest), default=""),
    }


# ── 컨텍스트 조립 ──────────────────────────────────────────────────

def _load_analyzed(days: int, types: tuple) -> list[dict]:
    marks = ",".join(f":t{i}" for i in range(len(types)))
    params = {f"t{i}": t for i, t in enumerate(types)}
    params["since"] = _since(days)
    rows = _rows(
        "SELECT firm, rtype, title, report_date, analysis_json FROM report_analysis "
        f"WHERE rtype IN ({marks}) AND report_date>=:since AND analysis_json IS NOT NULL", params)
    out = []
    for firm, rtype, title, d, aj in rows:
        try:
            out.append({"firm": firm, "rtype": rtype, "title": title, "report_date": d, "analysis": json.loads(aj)})
        except (TypeError, json.JSONDecodeError):
            continue
    return out


def holdings() -> list[tuple[str, str]]:
    rows = _rows("SELECT code, name FROM portfolio_positions WHERE status='holding' AND quantity > 0", {})
    return [(c, n) for c, n in rows]


def build_research_context(macro_days: int = 4, company_days: int = 45) -> str:
    docs = _load_analyzed(macro_days, MACRO_TYPES)
    official = _load_analyzed(30, ("공식문서",))
    lines: list[str] = []

    if docs:
        agg = aggregate_macro(docs)
        firms = {d["firm"] for d in docs}
        lines.append(f"=== 증권사 리포트 본문 분석 (최근 {macro_days}일 · {len(docs)}건 · {len(firms)}개 증권사 · 코드 검증) ===")
        lines.append("※ 각 주장·수치는 원문 대조를 통과한 것만 포함. 증권사 의견은 참고용이며 낙관 편향·후행성이 있다.")
        for th, v in sorted(agg.items(), key=lambda kv: -sum(kv[1]["stances"].values())):
            s = v["stances"]
            label = classify_consensus(s)
            tally = " ".join(f"{k} {s[k]}곳" for k in ("강세", "중립", "약세") if s.get(k))
            lines.append(f"\n[{th}] {tally} → {label}")
            for firm, claim, quote in v["claims"][:3]:
                lines.append(f"  · {firm}: [AI 요약] {claim}  (원문: \"{quote}\")")
        figs = [(d["firm"], f) for d in docs for f in d["analysis"].get("figures") or [] if is_forecast_figure(f)]
        if figs:
            lines.append("\n[증권사 전망·추정 수치 (원문 표기 그대로 — 사실이 아니라 각 사의 예상)]")
            for firm, f in figs[:10]:
                lines.append(f"  · {firm}: {f['label']} = {f['value']}")
        risks = [(d["firm"], r) for d in docs for r in d["analysis"].get("risks") or []]
        if risks:
            lines.append("\n[증권사들이 직접 언급한 위험 요인]")
            for firm, r in risks[:8]:
                lines.append(f"  · {firm}: {r}")

    if official:
        lines.append("\n=== 중앙은행·공식 문서 원문 분석 ===")
        for d in sorted(official, key=lambda x: x["report_date"] or "", reverse=True)[:5]:
            a = d["analysis"]
            tone = ""   # 매파/비둘기 성향은 실행마다 달라지는 주관 판단이라 표시하지 않는다(원문 인용만 신뢰)
            lines.append(f"\n[{d['firm']}] {d['title']} ({d['report_date']}){tone}")
            for c in (a.get("claims") or [])[:3]:
                lines.append(f"  · [AI 요약] {c['claim']}  (원문: \"{c['quote']}\")")
            for f in [f for f in (a.get("figures") or []) if is_forecast_figure(f)][:3]:
                lines.append(f"  · {f['label']} = {f['value']}")

    held = holdings()
    if held:
        rows = _rows(
            "SELECT code, firm, report_date, opinion, goal_price, price_at_write FROM report_analysis "
            "WHERE rtype='종목분석' AND report_date>=:s", {"s": _since(company_days)})
        by_code: dict[str, list[dict]] = defaultdict(list)
        for code, firm, d, op, g, pg in rows:
            by_code[code].append({"firm": firm, "report_date": d, "opinion": op, "goal_price": g, "price_at_write": pg})
        cov = []
        for code, name in held:
            reps = by_code.get(code)
            if not reps:
                cov.append(f"  · {name}({code}): 최근 {company_days}일 증권사 리포트 수집 없음 (커버리지 공백일 뿐 의견 부재가 아님)")
                continue
            st = target_price_stats(reps)
            op = " ".join(f"{k} {v}" for k, v in st["opinions"].items()) or "의견 미표기"
            goal = (f"목표가 중앙 {st['goal_median']:,}원 (최저 {st['goal_min']:,} ~ 최고 {st['goal_max']:,})"
                    if st["goal_median"] else "목표가 미표기")
            gap = f" · 작성일 주가 대비 목표 괴리 중앙 {st['gap_median_pct']:+.1f}%" if st["gap_median_pct"] is not None else ""
            chg = (f" · 같은 증권사 직전 대비 목표가 상향 {st['ups']}/하향 {st['downs']}/유지 {st['same']}"
                   if (st["ups"] or st["downs"] or st["same"]) else "")
            cov.append(f"  · {name}({code}): {st['n_firms']}개 증권사 · {op} · {goal}{gap}{chg} · 최신 {st['last_date']}")
        lines.append("\n=== 실보유 종목 증권사 커버리지 (코드 집계, 증권사별 최신 1건) ===")
        lines.extend(cov)

    return "\n".join(lines).strip()
