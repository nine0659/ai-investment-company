"""
services/macro_dashboard.py

공식 통계(clients/official_stats_client)의 원자료로 변화폭·YoY·스프레드를 **코드가 계산**하고,
각 수치에 기준일(as_of)과 신선도를 붙여 CEO 컨텍스트 텍스트로 만든다. LLM은 숫자를 만들지 않는다.

신선도: 월간 통계는 발표가 늦어 기준월이 한참 전일 수 있다 — 항상 기준일을 병기하고, 기대 주기보다
오래된 시리즈는 '오래됨'으로 표시한다(폐기 시리즈가 최신처럼 보이는 사고 방지).
"""
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

_CACHE: dict = {"t": 0.0, "data": None}
_CACHE_TTL = 6 * 3600   # BLS 무키 한도(일 25회)와 응답 속도 때문에 프로세스 내 캐시

_AREA_KO = {"KR": "한국", "US": "미국", "JP": "일본", "XM": "유로존", "CN": "중국", "GB": "영국",
            "KOR": "한국", "USA": "미국", "CHN": "중국", "JPN": "일본"}


# ── 순수 계산 ──────────────────────────────────────────────────────

def bp_change(series: list[float | None], back: int) -> float | None:
    """series 끝에서 back번째 전 대비 변화(bp). 값이 없으면 None."""
    if len(series) <= back or series[-1] is None or series[-1 - back] is None:
        return None
    return round((series[-1] - series[-1 - back]) * 100, 1)


def yoy_pct(points: list[tuple[str, float]]) -> tuple[str, float] | None:
    """월별 지수 → 최신월의 전년동월 대비 %. 12개월 전 같은 달이 없으면 None."""
    if not points:
        return None
    ym, v = points[-1]
    y, m = ym.split("-")
    prev_key = f"{int(y) - 1}-{m}"
    prev = dict(points).get(prev_key)
    if not prev:
        return None
    return ym, round((v / prev - 1) * 100, 2)


def age_days(as_of: str) -> int | None:
    try:
        if len(as_of) == 7:   # YYYY-MM → 그 달 말일 기준으로 보수적 계산
            y, m = int(as_of[:4]), int(as_of[5:7])
            end = datetime(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
        else:
            end = datetime.strptime(as_of[:10], "%Y-%m-%d")
        return (datetime.now() - end).days
    except ValueError:
        return None


def stale_tag(as_of: str, max_age: int) -> str:
    a = age_days(as_of)
    return f" ⚠오래됨({a}일 전)" if a is not None and a > max_age else ""


# ── 조립 ───────────────────────────────────────────────────────────

def _collect() -> dict:
    """키 없이 되는 5묶음 + 키가 설정된 묶음(ecos/fred/eia)을 병렬 수집. 키가 없는 묶음은 dict에 넣지 않는다
    (그래야 '키 미설정'이 수집 실패로 집계되지 않는다)."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    from clients import official_stats_client as osc
    jobs = {"curve": osc.fetch_treasury_curve, "sofr": osc.fetch_sofr, "bis": osc.fetch_bis_policy_rates,
            "cli": osc.fetch_oecd_cli, "bls": osc.fetch_bls}
    for name, env, fn in (("ecos", "ECOS_API_KEY", osc.fetch_ecos), ("fred", "FRED_API_KEY", osc.fetch_fred),
                          ("eia", "EIA_API_KEY", osc.fetch_eia)):
        if (os.getenv(env) or "").strip():
            jobs[name] = fn
    out = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result(timeout=90)
            except Exception as e:
                logger.warning("[매크로대시보드] %s 수집 실패: %s", k, e)
                out[k] = [] if k == "curve" else {}
    return out


# ── 키 필요 소스(ECOS·FRED·EIA) 포맷 ───────────────────────────────

def _fmt_ecos(e: dict) -> list[str]:
    items = []
    br = e.get("base_rate")
    if br:
        prev = f"(전월 {br[-2][1]:.2f}%)" if len(br) > 1 and br[-2][1] != br[-1][1] else ""
        items.append(f"기준금리 {br[-1][1]:.2f}%{prev}[{br[-1][0]}]")
    y = yoy_pct(e.get("cpi") or [])
    if y:
        items.append(f"소비자물가 전년비 {y[1]:+.2f}%[{y[0]}]")
    ca = e.get("current_account")
    if ca:
        items.append(f"경상수지 {ca[-1][1] / 100:+,.0f}억달러[{ca[-1][0]}]")
    ex = yoy_pct(e.get("exports") or [])
    if ex:
        items.append(f"수출 전년비 {ex[1]:+.1f}%[{ex[0]}]")
    fx = e.get("usdkrw")
    if fx:
        ch = f"({fx[-1][1] - fx[-2][1]:+.1f}원)" if len(fx) > 1 else ""
        items.append(f"원/달러 {fx[-1][1]:,.1f}{ch}[{fx[-1][0]}]")
    if not items:
        return []
    monthly = [x[-1][0] for x in (br, e.get("cpi"), ca, e.get("exports")) if x]
    oldest = max(monthly, key=lambda a: age_days(a) or 0) if monthly else ""
    return ["[한국 거시 — 한국은행 ECOS (월간은 발표 지연, 대괄호=기준월)]" + (stale_tag(oldest, 75) if oldest else ""),
            "  " + " | ".join(items)]


def _fmt_fred(f: dict) -> list[str]:
    spec = (("vix", "VIX", "{:.2f}", 6), ("hy_spread", "하이일드 스프레드", "{:.2f}%p", 6),
            ("fed_funds", "연방기금 실효금리", "{:.2f}%", 6), ("dollar_index", "달러지수(광의)", "{:.1f}", 6),
            ("jobless_claims", "신규실업수당청구", "{:,.0f}건", 14), ("sentiment", "미시간 소비자심리", "{:.1f}", 75))
    items = []
    for key, label, fmt, max_age in spec:
        pts = f.get(key)
        if not pts:
            continue
        d, v = pts[-1]
        if key == "sentiment":
            d = d[:7]          # 월간 시리즈는 매월 1일로 찍혀 나온다 — 월 단위로 신선도 판정
        ch = ""
        if len(pts) > 1:
            diff = v - pts[-2][1]
            ch = f", 직전 대비 {diff:+,.0f}" if key == "jobless_claims" else f", 직전 대비 {diff:+.2f}"
        items.append(f"{label} {fmt.format(v)}({d}{ch}){stale_tag(d, max_age)}")
    return ["[미국 시장·심리 지표 — FRED(세인트루이스 연준)]", "  " + " | ".join(items)] if items else []


def _fmt_eia(e: dict) -> list[str]:
    items = []
    for key, label in (("wti", "WTI"), ("brent", "브렌트")):
        pts = e.get(key)
        if pts:
            d, v = pts[-1]
            ch = f", 직전 대비 {v - pts[-2][1]:+.2f}" if len(pts) > 1 else ""
            items.append(f"{label} ${v:.2f}({d}{ch}){stale_tag(d, 10)}")
    cs = e.get("crude_stocks")
    if cs:
        d, v = cs[-1]
        ch = f", 전주 대비 {(v - cs[-2][1]) / 1000:+.1f}백만배럴" if len(cs) > 1 else ""
        items.append(f"미국 상업용 원유재고 {v / 1000:,.1f}백만배럴({d}{ch}){stale_tag(d, 14)}")
    return ["[에너지 — 미국 EIA (현물가는 주 단위로 갱신되어 며칠 지연)]", "  " + " | ".join(items)] if items else []




def format_dashboard(d: dict) -> tuple[str, int, int]:
    """(텍스트, 성공한 묶음 수, 전체 묶음 수). 순수 함수."""
    lines: list[str] = []
    ok = 0

    curve = d.get("curve") or []
    if curve:
        ok += 1
        last = curve[-1]
        def ser(k): return [r.get(k) for r in curve]
        parts = []
        for k in ("3M", "2Y", "10Y", "30Y"):
            if last.get(k) is None:
                continue
            ch = [f"{lbl} {bp_change(ser(k), n):+.0f}bp" for lbl, n in (("1일", 1), ("5일", 5), ("20일", 20))
                  if bp_change(ser(k), n) is not None]
            parts.append(f"{k} {last[k]:.2f}%" + (f" ({', '.join(ch)})" if ch else ""))
        sp = ""
        if last.get("10Y") is not None and last.get("2Y") is not None:
            s = round((last["10Y"] - last["2Y"]) * 100)
            sp += f" · 10Y-2Y {s:+d}bp" + (" 역전" if s < 0 else "")
        if last.get("10Y") is not None and last.get("3M") is not None:
            s3 = round((last["10Y"] - last["3M"]) * 100)
            sp += f" · 10Y-3M {s3:+d}bp" + (" 역전" if s3 < 0 else "")
        lines.append(f"[미국 국채수익률 — 재무부 {last['date']}]{stale_tag(last['date'], 6)}")
        lines.append("  " + " | ".join(parts) + sp)

    sofr = d.get("sofr") or {}
    if sofr.get("rate") is not None:
        ok += 1
        lines.append(f"[SOFR — 뉴욕연준 {sofr['as_of']}] {sofr['rate']}%{stale_tag(sofr['as_of'], 6)}")

    bis = d.get("bis") or {}
    if bis:
        ok += 1
        items = []
        asof = ""
        for a in ("KR", "US", "XM", "JP", "CN", "GB"):
            pts = bis.get(a)
            if not pts:
                continue
            asof = max(asof, pts[-1][0])
            chg = round(pts[-1][1] - pts[0][1], 2)
            items.append(f"{_AREA_KO[a]} {pts[-1][1]:.2f}%" + (f"({len(pts)-1}개월 {chg:+.2f}%p)" if chg else "(변동 없음)"))
        lines.append(f"[주요국 정책금리 — BIS 월별, 기준 {asof}]{stale_tag(asof, 75)}")
        lines.append("  " + " | ".join(items))

    cli = d.get("cli") or {}
    if cli:
        ok += 1
        items = []
        asof = ""
        for a in ("KOR", "USA", "CHN", "JPN"):
            pts = cli.get(a)
            if not pts:
                continue
            asof = max(asof, pts[-1][0])
            arrow = "↑" if len(pts) > 1 and pts[-1][1] > pts[-2][1] else "↓" if len(pts) > 1 and pts[-1][1] < pts[-2][1] else "→"
            items.append(f"{_AREA_KO[a]} {pts[-1][1]:.1f}{arrow}")
        lines.append(f"[OECD 경기선행지수 (100=장기추세) — 기준 {asof}]{stale_tag(asof, 100)}")
        lines.append("  " + " | ".join(items))

    bls = d.get("bls") or {}
    if bls:
        ok += 1
        items = []
        asof = ""
        y = yoy_pct(bls.get("cpi_index") or [])
        if y:
            asof = max(asof, y[0])
            items.append(f"CPI 전년비 {y[1]:+.2f}%({y[0]})")
        u = bls.get("unemployment")
        if u:
            asof = max(asof, u[-1][0])
            items.append(f"실업률 {u[-1][1]:.1f}%" + (f"(전월 {u[-2][1]:.1f}%)" if len(u) > 1 else ""))
        p = bls.get("payrolls")
        if p and len(p) > 1:
            asof = max(asof, p[-1][0])
            items.append(f"비농업고용 전월비 {p[-1][1] - p[-2][1]:+,.0f}천명({p[-1][0]})")
        if items:
            lines.append(f"[미국 물가·고용 — 노동통계국 BLS]{stale_tag(asof, 60)}")
            lines.append("  " + " | ".join(items))

    total = 5
    for key, fmt in (("ecos", _fmt_ecos), ("fred", _fmt_fred), ("eia", _fmt_eia)):
        if key not in d:          # 키 미설정 → 집계 대상 아님
            continue
        total += 1
        got = fmt(d.get(key) or {})
        if got:
            ok += 1
            lines.extend(got)

    return "\n".join(lines), ok, total


def build_macro_dashboard(force: bool = False) -> str:
    """공식 통계 대시보드 텍스트. 수집 실패 묶음은 빠지고, 절반 미만 성공이면 job_ledger에 fail 기록."""
    try:
        if not force and _CACHE["data"] is not None and time.time() - _CACHE["t"] < _CACHE_TTL:
            data = _CACHE["data"]
        else:
            data = _collect()
            _CACHE.update(t=time.time(), data=data)
        text, ok, total = format_dashboard(data)
        try:
            from services.job_ledger import record_job
            record_job("branch:macro_data", "success" if ok * 2 >= total else "fail", f"공식통계 {ok}/{total}묶음 수집")
        except Exception:
            pass
        if not text:
            return ""
        return ("=== 공식 통계 현황 (원자료는 각 기관, 변화폭·YoY·스프레드는 코드 계산) ===\n"
                "※ 월간 통계는 발표가 늦어 기준일을 병기함. 기준일이 오래된 항목은 '오래됨'으로 표시.\n" + text)
    except Exception as e:
        logger.error("[매크로대시보드] 실패: %s", e)
        return ""
