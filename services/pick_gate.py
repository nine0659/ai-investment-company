"""주간 추천 관문 — 같은 종목 반복·섹터 쏠림·과열/급락장 추격 추천을 코드가 구조적으로 막는다.

2026-10-14 성적표: 7건 추천 중 시장을 이긴 건 1건, 독립 표본은 4종목·3추천일뿐이었다. 원인 가설(데이터로 확인):
 ① 후보 풀이 KOSPI 시총 30개 고정이라 같은 대형 경기민감주(반도체·자동차)가 매번 뽑혔다
 ② 이미 계좌의 84%가 반도체인데 같은 섹터를 또 추천했다
 ③ 시장이 7,000선 고점권일 때 3종목 전부를 한 번에 추천했다
프롬프트로 "분산하라"고 부탁하는 대신, LLM에 넘기기 전에 후보를 걸러내고 LLM이 낸 뒤에 개수·섹터 중복을 잘라낸다.
"""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

COOLDOWN_DAYS = 28            # 최근 이 기간 내 추천했던 종목은 후보에서 제외
MIN_CANDIDATES = 8            # 걸러낸 뒤 이보다 적으면 섹터 제외를 풀어 후보 고갈을 막는다
CRASH_5D_PCT = -8.0
CRASH_1D_PCT = -3.0
HEAT_20D_PCT = 10.0
HEAT_MA60_RATIO = 1.10

# KOSPI_TOP30(agents/midterm_agent.py) 업종 분류 — 섹터 분산 판단용
SECTOR = {
    "005930": "반도체", "000660": "반도체", "207940": "바이오", "068270": "바이오",
    "373220": "2차전지", "006400": "2차전지", "003670": "2차전지", "247540": "2차전지",
    "005380": "자동차", "000270": "자동차", "012330": "자동차", "005490": "철강소재", "051910": "화학",
    "035420": "인터넷", "035720": "인터넷", "028260": "지주·유통", "034730": "지주·유통", "003550": "지주·유통",
    "105560": "금융", "055550": "금융", "086790": "금융", "316140": "금융",
    "032830": "보험", "000810": "보험", "066570": "전자", "017670": "통신",
    "012450": "방산", "033780": "필수소비", "034020": "에너지설비", "096770": "에너지설비",
}


def sector_of(code: str, fallback: str = "기타") -> str:
    return SECTOR.get(code, fallback)


def recent_rec_codes(days: int = COOLDOWN_DAYS, today: str | None = None) -> set[str]:
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT DISTINCT code FROM stock_recommendations WHERE date >= :c"),
                                {"c": cutoff}).fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        logger.debug("[추천관문] 최근 추천 조회 실패: %s", e)
        return set()


def held_summary(positions: list[dict]) -> tuple[set[str], dict[str, float]]:
    """(보유 종목코드, 업종별 계좌 비중 %). positions: portfolio_risk.load_positions 결과."""
    total = sum(p["value"] for p in positions) or 1.0
    by: dict[str, float] = {}
    for p in positions:
        g = SECTOR.get(p["code"]) or p.get("sector") or "기타"
        by[g] = by.get(g, 0.0) + p["value"] / total * 100
    return {p["code"] for p in positions}, by


def filter_candidates(valid: list[dict], recent: set[str], held: set[str], sector_w: dict[str, float],
                      sector_cap: float = 60.0) -> tuple[list[dict], list[tuple[str, str]]]:
    """valid: get_stock_valuation 결과 목록. 반환: (남은 후보, [(종목명, 제외 사유)])."""
    def run(use_sector: bool) -> tuple[list[dict], list[tuple[str, str]]]:
        kept, out = [], []
        for d in valid:
            code, name = d["code"], d.get("name", d["code"])
            if code in held:
                out.append((name, "이미 보유 중(신규 추천 대상 아님)"))
            elif code in recent:
                out.append((name, f"최근 {COOLDOWN_DAYS}일 내 추천한 종목(같은 판단 반복 방지)"))
            elif use_sector and sector_w.get(SECTOR.get(code, ""), 0) >= sector_cap:
                out.append((name, f"내 계좌의 {SECTOR[code]} 비중이 이미 {sector_w[SECTOR[code]]:.0f}% — 같은 업종 추가 추천 금지"))
            else:
                kept.append(d)
        return kept, out
    kept, out = run(True)
    if len(kept) < MIN_CANDIDATES:               # 후보 고갈 방지: 섹터 제외를 풀고 다시
        kept, out = run(False)
    return kept, out


def market_gate(closes: dict[str, float]) -> dict:
    """KOSPI 상태로 이번 주 추천 개수 상한을 정한다. 반환: {mode, max_picks, reason, note}."""
    days = sorted(closes)
    if len(days) < 25:
        return {"mode": "unknown", "max_picks": 3, "reason": "지수 데이터 부족", "note": ""}
    c = [closes[d] for d in days]
    r1 = (c[-1] / c[-2] - 1) * 100
    r5 = (c[-1] / c[-6] - 1) * 100 if len(c) > 6 else 0.0
    r20 = (c[-1] / c[-21] - 1) * 100 if len(c) > 21 else 0.0
    ma60 = sum(c[-60:]) / len(c[-60:]) if len(c) >= 60 else None
    heat = (c[-1] / ma60) if ma60 else None
    if r5 <= CRASH_5D_PCT or r1 <= CRASH_1D_PCT:
        return {"mode": "crash", "max_picks": 2,
                "reason": f"급락 직후(5일 {r5:+.1f}%, 1일 {r1:+.1f}%)",
                "note": "한 번에 전액 진입 금지 — 1/3씩 3회 분할, 반등 확인 전 첫 1/3만"}
    if r20 >= HEAT_20D_PCT or (heat and heat >= HEAT_MA60_RATIO):
        return {"mode": "heated", "max_picks": 1,
                "reason": f"단기 과열(20일 {r20:+.1f}%" + (f", 60일선 대비 {heat*100-100:+.1f}%)" if heat else ")"),
                "note": "고점 추격 금지 — 1종목만, 분할 진입"}
    return {"mode": "normal", "max_picks": 3, "reason": f"특이 과열/급락 없음(5일 {r5:+.1f}%, 20일 {r20:+.1f}%)", "note": ""}


def constraint_block(gate: dict, excluded: list[tuple[str, str]], sector_w: dict[str, float]) -> str:
    lines = ["[이번 주 추천 제약 — 코드 확정. 어기지 말 것]",
             f"- 추천 종목은 최대 {gate['max_picks']}개 ({gate['reason']})",
             "- 서로 다른 업종에서만 고를 것(같은 업종 2종목 금지)"]
    if gate.get("note"):
        lines.append(f"- {gate['note']}")
    if sector_w:
        top = ", ".join(f"{k} {v:.0f}%" for k, v in sorted(sector_w.items(), key=lambda x: -x[1])[:3])
        lines.append(f"- 내 계좌 업종 비중: {top} — 이미 많은 업종보다 비중이 낮은 업종을 우선 검토")
    if excluded:
        lines.append("- 후보에서 제외됨(추천하지 말 것): " + "; ".join(f"{n}({why})" for n, why in excluded[:12]))
    lines.append("- 추천이 없는 편이 나으면 '이번 주는 추천 없음'이라고 쓰는 것도 허용된다(억지로 채우지 말 것)")
    return "\n".join(lines)


def enforce(recs: list[dict], max_picks: int) -> list[dict]:
    """LLM이 낸 추천을 잘라낸다: 업종 중복 제거 → 개수 상한. 앞 순서(=LLM 우선순위) 유지."""
    out, seen = [], set()
    for r in recs:
        g = SECTOR.get(r["code"], r["code"])
        if g in seen:
            continue
        seen.add(g)
        out.append(r)
        if len(out) >= max_picks:
            break
    return out


_ITEM = re.compile(r"^\s*(\d)\.\s", re.M)


def truncate_report(report: str, max_picks: int) -> str:
    """리포트 본문에서 max_picks 번째 초과 번호 항목을 잘라낸다('📌' 마무리 줄은 보존)."""
    items = list(_ITEM.finditer(report))
    if len(items) <= max_picks:
        return report
    cut = items[max_picks].start()
    tail = report[cut:]
    foot = re.search(r"^📌.*$", tail, re.M)
    return report[:cut].rstrip() + ("\n\n" + foot.group(0) if foot else "")
