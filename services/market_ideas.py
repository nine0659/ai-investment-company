"""시장 전체 아이디어 블록 — 한국·미국 전 종목 스크리닝 스냅샷과 DART 공시 사건을 브리핑/봇 컨텍스트용 텍스트로 조립.

보유 종목 여부와 무관한 '관심 후보'다. 모든 수치는 저장된 스냅샷·공시 본문에서 코드가 계산한 값이고,
LLM은 이 블록의 수치만 인용한다(없는 수치 생성 금지 — 절대원칙 2·3). 스냅샷이 오래됐으면 블록을 넣지 않는다.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_MAX_AGE_DAYS = 4
_VALIDATED_GOOD = {"자사주취득"}   # 소각·배당은 검증 표본 0건 — 미검증

_HEADER = ("[🔭 시장 전체 스크리닝 — 코드 계산. 보유 여부와 무관한 '관심 후보'이며 매수 권고가 아니다. "
           "이 블록의 수치만 인용하고 없는 수치는 만들지 마라. 신규 편입을 판단할 땐 이 후보 풀을 먼저 검토하고, "
           "목록 밖 종목을 편입하려면 근거를 따로 제시하라. 후보를 언급할 땐 ⚠경고와 반증 조건도 함께 적어라. "
           "✔ 표시 신호(한국 비금융 저PER·자사주 취득 공시)만 과거 검증을 통과했다(약 1.5~2년 한국 기준 — 시기 특유일 수 있고 저PER 근거는 약함, 금융주 랠리 제외 시). "
           "표시 없는 신호와 미국 신호는 시장 초과수익을 낸다는 검증은 아직 없다(가격 추세 신호는 효과가 없어 점수에서 제외) — "
           "후보는 '탐색 출발점'이지 예측이 아니므로 확신도를 높이는 근거로 쓰지 마라]")


def _fresh(date_str: str | None) -> bool:
    if not date_str:
        return False
    try:
        age = (datetime.now(_KST).date() - datetime.strptime(date_str, "%Y-%m-%d").date()).days
    except ValueError:
        return False
    return age <= _MAX_AGE_DAYS


def _query(sql: str, params: dict | None = None) -> list:
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        return conn.execute(text(sql), params or {}).fetchall()


def _pct(a, b) -> float | None:
    return (a / b - 1) * 100 if a and b else None


def kr_picks(exclude: set[str], n: int = 5) -> tuple[str | None, list[dict], set[str]]:
    """(스냅샷 날짜, 상위 후보, 직전 스냅샷에서도 3묶음 이상이었던 코드 집합)."""
    dates = [r[0] for r in _query("SELECT DISTINCT date FROM universe_screen_daily ORDER BY date DESC LIMIT 2")]
    if not dates or not _fresh(dates[0]):
        return None, [], set()
    rows = _query("SELECT code, name, market, close, chg_pct, mcap_eok, per, pbr, cns_per, eps, target, high52, score, signals "
                  "FROM universe_screen_daily WHERE date=:d AND score>=3 ORDER BY score DESC, mcap_eok DESC LIMIT 300",
                  {"d": dates[0]})
    keys = ("code", "name", "market", "close", "chg_pct", "mcap_eok", "per", "pbr", "cns_per", "eps", "target",
            "high52", "score", "signals")
    from services.universe_screener import validated_groups_in
    picks = [dict(zip(keys, r)) for r in rows if r[0] not in exclude]
    picks.sort(key=lambda x: (-validated_groups_in(x["signals"]), -x["score"], -x["mcap_eok"]))     # 검증된 신호가 많은 종목 먼저
    picks = picks[:n]
    prev: set[str] = set()
    if len(dates) > 1:
        prev = {r[0] for r in _query("SELECT code FROM universe_screen_daily WHERE date=:d AND score>=3", {"d": dates[1]})}
    return dates[0], picks, prev


def us_picks(exclude: set[str], n: int = 5) -> tuple[str | None, list[dict], set[str]]:
    dates = [r[0] for r in _query("SELECT DISTINCT date FROM us_screen_daily ORDER BY date DESC LIMIT 2")]
    if not dates or not _fresh(dates[0]):
        return None, [], set()
    rows = _query("SELECT code, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, high52, score, signals "
                  "FROM us_screen_daily WHERE date=:d AND score>=3 ORDER BY score DESC, mcap_usd DESC LIMIT 60",
                  {"d": dates[0]})
    keys = ("code", "name", "sector", "close", "chg_pct", "mcap_usd", "fwd_pe", "roe", "target", "high52", "score", "signals")
    picks = [dict(zip(keys, r)) for r in rows if r[0] not in exclude][:n]
    prev: set[str] = set()
    if len(dates) > 1:
        prev = {r[0] for r in _query("SELECT code FROM us_screen_daily WHERE date=:d AND score>=3", {"d": dates[1]})}
    return dates[0], picks, prev


def _kr_line(p: dict, events: list[dict], is_new: bool) -> str:
    up = _pct(p["target"], p["close"])
    dd = _pct(p["close"], p["high52"])
    bits = [f"{p['name']}({p['code']}) {p['close']:,.0f}원 · 시총 {p['mcap_eok']:,.0f}억 · 신호 {p['score']}묶음"
            + (" 🆕어제는 후보 아니었음" if is_new else "")]
    bits.append(f"신호: {p['signals']}")
    stats = []
    if p["cns_per"]:
        stats.append(f"추정PER {p['cns_per']:.1f}배")
    if p["pbr"]:
        stats.append(f"PBR {p['pbr']:.2f}배")
    if up is not None:
        stats.append(f"목표가 대비 {up:+.0f}%")
    if dd is not None:
        stats.append(f"52주 고점 대비 {dd:.0f}%")
    if stats:
        bits.append(" · ".join(stats))
    good = [e for e in events if e["polarity"] == "호재"][:1]
    bad = [e for e in events if e["polarity"] == "악재"][:2]
    for e in good:
        bits.append(f"최근 공시: {e['event_type']} {e['summary'] or ''} ({e['date']})".strip())
    for e in bad:
        bits.append(f"⚠최근 악재 공시: {e['event_type']} ({e['date']})")
    return "  · " + " | ".join(bits)


def _us_line(p: dict, is_new: bool) -> str:
    up = _pct(p["target"], p["close"])
    dd = _pct(p["close"], p["high52"])
    stats = []
    if p["fwd_pe"]:
        stats.append(f"선행PER {p['fwd_pe']:.1f}배")
    if p["roe"] is not None:
        stats.append(f"ROE {p['roe'] * 100:.0f}%")
    if up is not None:
        stats.append(f"목표가 대비 {up:+.0f}%")
    if dd is not None:
        stats.append(f"52주 고점 대비 {dd:.0f}%")
    s = (f"  · {p['name']}({p['code']}) ${p['close']:,.2f} · 시총 ${p['mcap_usd'] / 1e9:,.1f}B · {p['sector']} · "
         f"신호 {p['score']}묶음" + (" 🆕어제는 후보 아니었음" if is_new else "") + f" | 신호: {p['signals']}")
    return s + (" | " + " · ".join(stats) if stats else "")


def event_highlights(days: int = 2, min_mcap_eok: float = 3000, n: int = 8, skip_types: set[str] | None = None) -> list[str]:
    """최근 며칠 시장 전체 공시 중 시총 큰 종목의 의미 있는 호재·악재 (공급계약은 매출액대비 10%+, 실적은 호재/악재 판정분)."""
    from services.disclosure_events import recent_events
    since = (datetime.now(_KST).date() - timedelta(days=days)).strftime("%Y-%m-%d")
    evs = [e for e in recent_events(days=days + 1) if e["date"] >= since]
    if not evs:
        return []
    caps = {r[0]: r[1] for r in _query(
        "SELECT code, mcap_eok FROM universe_screen_daily WHERE date=(SELECT max(date) FROM universe_screen_daily)")}
    meaningful = []
    for e in evs:
        cap = caps.get(e["stock_code"])
        if not cap or cap < min_mcap_eok:
            continue
        t = e["event_type"]
        if skip_types and t in skip_types:
            continue
        if t == "잠정실적" and e["polarity"] != "악재":
            meaningful.append((cap, e))                       # 실적 발표는 호재로 단정하지 않고 '참고'로만 (이후 성과가 오히려 부진했다)
        elif e["polarity"] == "호재" and (t in ("자사주취득", "자사주소각", "배당결정")
                                         or (t == "공급계약" and (e["metric"] or 0) >= 10)):
            meaningful.append((cap, e))
        elif e["polarity"] == "악재" and t != "위험해소·절차":
            meaningful.append((cap, e))
    meaningful.sort(key=lambda x: -x[0])
    out = []
    for cap, e in meaningful[:n]:
        # 과거 검증(2024-10~2026-10 한국)을 통과한 사건(자사주 취득)은 따로 표시한다: 20일 뒤 중앙 +1.62%p, t=+3.2
        if e["polarity"] == "호재":
            tag = "✔호재(과거 검증)" if e["event_type"] in _VALIDATED_GOOD else "호재"
        elif e["polarity"] == "악재":
            tag = "⚠악재"
        else:
            tag = "실적 발표(참고)" if e["event_type"] == "잠정실적" else "참고"
        out.append(f"  · {e['corp_name']}({e['stock_code']}) 시총 {cap:,.0f}억 [{tag}] {e['event_type']}"
                   f"{(' — ' + e['summary']) if e['summary'] else ''} ({e['date']})")
    return out


def validated_events(days: int = 3, min_mcap_eok: float = 1000, n: int = 8) -> list[str]:
    """과거 검증을 통과한 호재 사건(자사주 취득 결정)을 스크리너 후보 여부와 무관하게 최근 며칠치 전부.
    같은 종목의 같은 유형은 한 번만, 시총 큰 순. (검증: 2024-10~2026-10 한국, 접수일 다음 거래일 이후 20일 중앙 초과수익 +1.62%p, t=+3.2)"""
    from services.disclosure_events import recent_events
    since = (datetime.now(_KST).date() - timedelta(days=days)).strftime("%Y-%m-%d")
    evs = [e for e in recent_events(days=days + 1) if e["date"] >= since and e["event_type"] in _VALIDATED_GOOD]
    if not evs:
        return []
    caps = {r[0]: r[1] for r in _query(
        "SELECT code, mcap_eok FROM universe_screen_daily WHERE date=(SELECT max(date) FROM universe_screen_daily)")}
    seen, rows = set(), []
    for e in evs:
        cap = caps.get(e["stock_code"])
        key = (e["stock_code"], e["event_type"])
        if not cap or cap < min_mcap_eok or key in seen:
            continue
        seen.add(key)
        rows.append((cap, e))
    rows.sort(key=lambda x: -x[0])
    return [f"  · {e['corp_name']}({e['stock_code']}) 시총 {cap:,.0f}억 — {e['event_type']} 결정 ({e['date']})" for cap, e in rows[:n]]


def build_block(exclude_codes: set[str] | None = None, for_user: bool = False) -> str:
    """브리핑·봇 컨텍스트용 텍스트. 소스별로 실패해도 나머지는 넣는다. 아무것도 없으면 ''."""
    exclude = set(exclude_codes or ())
    parts: list[str] = []

    try:
        d, picks, prev = kr_picks(exclude)
        if picks:
            from services.disclosure_events import by_code
            evs = by_code(days=7)
            parts.append(f"한국 전 종목 {d} 기준 상위 후보 (코스피·코스닥 약 1,280종목 스크리닝, 신호 3묶음 이상, ✔=과거 검증 통과 신호가 많은 순):")
            parts += [_kr_line(p, evs.get(p["code"], []), bool(prev) and p["code"] not in prev) for p in picks]
    except Exception as e:
        logger.warning("[시장아이디어] 한국 후보 실패 (건너뜀): %s", e)

    try:
        d, picks, prev = us_picks(exclude)
        if picks:
            parts.append(f"\n미국 전 종목 {d} 기준 상위 후보 (시총 $30억+ 약 1,600종목 스크리닝, 신호 3묶음 이상):")
            parts += [_us_line(p, bool(prev) and p["code"] not in prev) for p in picks]
    except Exception as e:
        logger.warning("[시장아이디어] 미국 후보 실패 (건너뜀): %s", e)

    try:
        from services.industry_board import format_board, load_snapshots
        snaps = load_snapshots()
        if snaps and _fresh(snaps[0][0]):
            board = format_board(snaps)
            if board:
                parts.append("\n" + board)
    except Exception as e:
        logger.warning("[시장아이디어] 업종 온도계 실패 (건너뜀): %s", e)

    try:
        hl = event_highlights()
        if hl:
            parts.append("\n최근 2일 한국 시장 공시 하이라이트 (시총 3,000억+, 본문 숫자는 코드가 추출):")
            parts += hl
    except Exception as e:
        logger.warning("[시장아이디어] 공시 하이라이트 실패 (건너뜀): %s", e)

    if not parts:
        return ""
    head = ("🔭 시장 전체 스크리닝 후보 — 코드 계산, 매수 권고가 아닌 '볼 만한 후보' 목록입니다. 종목별 이유는 /why 종목명"
            if for_user else _HEADER)
    return head + "\n" + "\n".join(parts)
