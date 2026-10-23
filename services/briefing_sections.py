"""브리핑에 코드가 직접 붙이는 '전망·발굴' 섹션 (2026-10-23).

배경: 목표는 '시장 분석 → 전망 → 종목 발굴 → 조언'인데, 장전 브리핑 템플릿에는 전망·발굴을 담을 칸이 없었다.
후보 풀(market_ideas)은 CEO 입력에만 들어갔고 CEO가 '신규 편입'을 결정하지 않으면 사용자 눈에는 아무것도 안 보였다
(10/23: 하이닉스 비중 축소 얘기뿐, 발굴·전망 없음 — "계산은 되는데 안 보임" 패턴의 네 번째 사례).
그래서 LLM 서술을 거치지 않고 저장된 스냅샷에서 코드가 줄을 만들어 브리핑 끝에 붙인다(research_pipeline과 같은 방식).
모든 수치는 스냅샷 값, 표시·고지는 고정 문구. 없는 항목은 말없이 생략하고, 전부 없으면 ''.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 신호 키 → 쉬운 말 (저장 문자열의 '✔'는 과거 검증 통과, '(참고)'는 점수 제외)
_PLAIN = {"저평가": "이익 대비 싼 편", "자산대비저평가": "장부가치 대비 쌈", "업종대비저평가": "같은 업종보다 싼 편",
          "업종대비저PBR": "같은 업종보다 장부가치 대비 쌈", "이익증가기대": "이익 증가 기대", "컨센서스여력": "증권사 목표가 여력",
          "고점대비낙폭": "고점 대비 많이 하락", "쌍끌이매수": "외국인·기관 동반 매수", "공시호재": "호재 공시",
          "수익성": "수익성 양호", "내부자매수": "임원 매수"}

_SKIP = {"수익성"}      # ROE 단독은 효과 없음(참고용) — 사용자에게 근거처럼 보이지 않게 뺀다

DISCLAIMER = "※ 후보는 '볼 만한 종목'이지 매수 권고가 아닙니다. ✔=과거 검증을 통과한 근거(그래도 확실하진 않음), 나머지는 검증 전입니다."


def plain_signals(signals: str | None, max_items: int = 4) -> str:
    """'저평가✔,공시호재✔,수익성(참고)' → '이익 대비 싼 편✔ · 호재 공시✔ · …'. 검증된 것을 앞에 둔다."""
    items = []
    for raw in (signals or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        key = raw.replace("✔", "").split("(")[0]
        if key in _SKIP:
            continue
        mark = "✔" if "✔" in raw else ""
        items.append((0 if mark else 1, f"{_PLAIN.get(key, key)}{mark}"))
    items.sort(key=lambda x: x[0])
    return " · ".join(t for _, t in items[:max_items])


def _kr_lines(exclude: set[str], n: int = 3) -> list[str]:
    from services import market_ideas as mi
    from services.disclosure_events import by_code
    d, picks, _ = mi.kr_picks(exclude, n=n)
    if not picks:
        return []
    evs = by_code(days=7)
    out = []
    for p in picks:
        stats = []
        if p.get("cns_per"):
            stats.append(f"예상PER {p['cns_per']:.1f}배")
        up = mi._pct(p.get("target"), p["close"])
        if up is not None:
            stats.append(f"목표가 대비 {up:+.0f}%")
        dd = mi._pct(p["close"], p.get("high52"))
        if dd is not None:
            stats.append(f"고점 대비 {dd:.0f}%")
        warn = [e for e in evs.get(p["code"], []) if e["polarity"] == "악재"][:1]
        line = f"· {p['name']}({p['code']}) {p['close']:,.0f}원 — {plain_signals(p['signals'])}" + \
               (f" | {' · '.join(stats)}" if stats else "")
        if warn:
            line += f" | ⚠최근 악재 공시: {warn[0]['event_type']}"
        out.append(line)
    return out


def _us_lines(exclude: set[str], n: int = 2) -> list[str]:
    from services import market_ideas as mi
    d, picks, _ = mi.us_picks(exclude, n=n)
    out = []
    for p in picks:
        stats = []
        if p.get("fwd_pe"):
            stats.append(f"예상PER {p['fwd_pe']:.1f}배")
        up = mi._pct(p.get("target"), p["close"])
        if up is not None:
            stats.append(f"목표가 대비 {up:+.0f}%")
        out.append(f"· {p['name']}({p['code']}) ${p['close']:,.2f} — {plain_signals(p['signals'])}"
                   + (f" | {' · '.join(stats)}" if stats else ""))
    return out


def _financial_codes() -> set[str]:
    """최근 스냅샷에서 금융업 종목 코드 집합. 업종 코드가 아직 안 쌓였으면 빈 집합(필터 불가 → 그대로 노출)."""
    from db.database import get_conn
    from sqlalchemy import text
    from services.universe_screener import FIN_KEYWORDS
    with get_conn() as conn:
        d = conn.execute(text("SELECT MAX(date) FROM universe_screen_daily WHERE industry IS NOT NULL AND industry<>''")).scalar()
        if not d:
            return set()
        names = dict(conn.execute(text("SELECT ind_no, name FROM industry_daily WHERE date=(SELECT MAX(date) FROM industry_daily)")).fetchall())
        rows = conn.execute(text("SELECT code, industry FROM universe_screen_daily WHERE date=:d"), {"d": d}).fetchall()
    fin_inds = {k for k, nm in names.items() if any(w in (nm or "") for w in FIN_KEYWORDS)}
    return {c for c, ind in rows if ind in fin_inds}


def _buyback_lines(n: int = 3) -> list[str]:
    """과거 검증은 비금융 기준이라 금융업은 뺀다(업종 정보가 있을 때)."""
    from services import market_ideas as mi
    rows = mi.validated_events(days=3, min_mcap_eok=1000, n=n * 3)
    try:
        fin = _financial_codes()
    except Exception as e:
        logger.warning("[브리핑섹션] 금융업 판별 실패 (필터 없이 진행): %s", e)
        fin = set()
    kept = [r for r in rows if not any(f"({c})" in r for c in fin)]
    return [r.replace("  · ", "· ", 1) for r in kept[:n]]


def _board_lines() -> list[str]:
    """업종 흐름 한 줄씩: 시장 대비 강한/약한 업종(현재 상태 설명 — 이후 성과 예측이 아니다)."""
    from services.industry_board import load_snapshots
    snaps = load_snapshots(limit=1)
    if not snaps or not snaps[0][1]:
        return []
    date, rows = snaps[0]
    if (datetime.now(_KST).date() - datetime.strptime(date, "%Y-%m-%d").date()).days > 5:
        return []
    inds = [r for r in rows if r["ind_no"] != "ALL" and r.get("med_ret_3m") is not None]
    strong = sorted([r for r in inds if r["regime"] == "강세"], key=lambda r: -r["med_ret_3m"])[:3]
    weak = sorted([r for r in inds if r["regime"] == "약세"], key=lambda r: r["med_ret_3m"])[:3]
    out = []
    if strong:
        out.append("· 강한 업종: " + ", ".join(f"{r['name']}({r['med_ret_3m']:+.0f}%)" for r in strong))
    if weak:
        out.append("· 약한 업종: " + ", ".join(f"{r['name']}({r['med_ret_3m']:+.0f}%)" for r in weak))
    if out:
        out.append("· 최근 3개월 종목 수익률 중앙값 기준의 현재 상태입니다. 앞으로 계속 강하다는 뜻은 아닙니다(과거 검증에서 추세는 예측력이 없었음).")
    return out


def _sector_lines() -> list[str]:
    """업종 전망 가설(기록·채점 중) — 우호/비우호 각 최대 2개. 채점 결과가 없으면 그렇다고 밝힌다."""
    from db.database import get_conn
    from sqlalchemy import text
    from services import sector_outlook as so
    with get_conn() as conn:
        d = conn.execute(text("SELECT MAX(date) FROM sector_outlook")).scalar()
        if not d or (datetime.now(_KST).date() - datetime.strptime(d, "%Y-%m-%d").date()).days > 5:
            return []
        rows = conn.execute(text("SELECT name, view, prob, reason FROM sector_outlook WHERE date=:d ORDER BY prob DESC"),
                            {"d": d}).fetchall()
    good = [r for r in rows if r[1] == "우호"][:2]
    bad = [r for r in rows if r[1] == "비우호"][:2]
    out = []
    for label, grp in (("시장보다 나을 것으로 본 업종", good), ("시장보다 못할 것으로 본 업종", bad)):
        if grp:
            out.append(f"· {label}: " + ", ".join(f"{r[0]}({r[2]:.0f}%)" for r in grp))
    if out:
        out.append(f"· {so.format_scorecard_line(so.scorecard())}")
    return out


def build(exclude_codes: set[str] | None = None) -> tuple[str, dict]:
    """(브리핑 끝에 붙일 텍스트, 항목별 줄 수). 소스별로 실패해도 나머지는 넣는다. 전부 비면 ('', counts)."""
    exclude = set(exclude_codes or ())
    counts: dict[str, int] = {}
    sections: list[str] = []

    def add(title: str, name: str, fn):
        try:
            lines = fn()
        except Exception as e:
            logger.warning("[브리핑섹션] %s 실패 (건너뜀): %s", name, e)
            counts[name] = -1
            return
        counts[name] = len(lines)
        if lines:
            sections.append(title + "\n" + "\n".join(lines))

    add("🌡 업종 흐름 (한국 전 종목 집계)", "board", _board_lines)
    add("🔎 오늘의 발굴 후보 — 한국 (전 종목 스크리닝)", "kr", lambda: _kr_lines(exclude))
    add("🔎 미국 후보", "us", lambda: _us_lines(exclude))
    add("✔ 자사주 취득 결정 (최근 3일, 과거 검증 통과 공시)", "buyback", _buyback_lines)
    add("🧭 업종 전망 (향후 20거래일, 시장 대비 · 기록·채점 중인 가설)", "sector", _sector_lines)
    if not sections:
        return "", counts
    return "\n\n".join(sections) + "\n" + DISCLAIMER, counts
