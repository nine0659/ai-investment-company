"""
services/report_digest.py

증권사 리포트를 '시장·매크로 분석의 참고 재료'로 가공한다 (LLM 없음, 전부 코드 계산).

원칙
- 리포트는 결론이 아니라 참고다. 증권사 의견은 낙관 편향이 있고 목표가는 주가를 뒤따른다.
  그래서 '무슨 말을 했나'보다 '몇 곳이 어떤 주제를 어떤 톤으로 다루나'를 집계해 넘기고,
  LLM에는 "여러 곳이 같은 말을 하면 오히려 쏠림일 수 있다"는 반론 관점을 함께 준다.
- 제목 키워드 집계는 거친 신호다. 본문을 읽은 게 아니라는 한계를 컨텍스트에 명시한다.
- 같은 증권사가 같은 주제를 여러 건 써도 1곳으로 센다 (중복으로 부풀리지 않음).
- 수집이 0건이면 조용히 넘어가지 않고 `branch:report_sources` fail로 기록(daily_health 경보).
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 시장·매크로 성격의 유형 (종목분석은 개별 종목이라 매크로 집계에서 제외)
MACRO_TYPES = ("경제분석", "투자전략", "산업분석")

THEMES: dict[str, tuple[str, ...]] = {
    "금리·통화정책": ("금리", "FOMC", "연준", "Fed", "통화정책", "금통위", "인하", "인상", "기준금리", "국채"),
    "환율·달러":    ("환율", "원/달러", "달러", "원화", "엔화", "위안"),
    "물가·성장":    ("물가", "CPI", "PCE", "인플레", "GDP", "성장률", "고용", "소비", "경기"),
    "반도체·AI":    ("반도체", "AI", "HBM", "메모리", "엔비디아", "파운드리", "D램", "DRAM", "낸드"),
    "미국·글로벌":  ("미국", "S&P", "나스닥", "월가", "글로벌", "트럼프", "관세"),
    "중국":         ("중국", "위안화", "홍콩"),
    "에너지·원자재": ("유가", "원유", "WTI", "구리", "금값", "원자재", "OPEC"),
    "실적·밸류":    ("실적", "어닝", "밸류에이션", "PER", "이익", "컨센서스", "목표"),
    "수급·심리":    ("외국인", "기관", "수급", "공매도", "변동성", "VIX", "급락", "급등"),
}

_CAUTIOUS = ("급락", "하락", "둔화", "우려", "리스크", "위험", "하향", "조정", "약세", "부진", "경계", "충격", "불안", "후퇴", "쇼크")
_POSITIVE = ("상승", "반등", "회복", "확대", "호조", "상향", "개선", "강세", "돌파", "랠리", "기대", "서프라이즈", "낙관")


def classify_themes(title: str) -> list[str]:
    t = title or ""
    return [name for name, kws in THEMES.items() if any(k.lower() in t.lower() for k in kws)]


def classify_tone(title: str) -> str:
    """제목 키워드 기반 거친 톤. 둘 다 있으면 '혼재', 둘 다 없으면 '중립'."""
    t = title or ""
    c = any(k in t for k in _CAUTIOUS)
    p = any(k in t for k in _POSITIVE)
    if c and p:
        return "혼재"
    if c:
        return "경계"
    if p:
        return "긍정"
    return "중립"


def enrich(reports: list[dict]) -> list[dict]:
    """각 리포트에 themes/tone 부여 (원본 불변)."""
    out = []
    for r in reports or []:
        out.append({**r, "themes": classify_themes(r.get("title", "")), "tone": classify_tone(r.get("title", ""))})
    return out


def summarize_today(reports: list[dict]) -> dict:
    """오늘 수집분의 주제별 '증권사 수'와 톤 분포. 순수 함수."""
    enriched = enrich([r for r in reports if r.get("type") in MACRO_TYPES])
    theme_firms: dict[str, set] = defaultdict(set)
    theme_tone: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in enriched:
        for th in r["themes"]:
            theme_firms[th].add(r["firm"])
            theme_tone[th][r["tone"]].add(r["firm"])
    firms = {r["firm"] for r in enriched}
    return {
        "n_macro": len(enriched),
        "n_firms": len(firms),
        "theme_firms": {k: len(v) for k, v in theme_firms.items()},
        "theme_tone": {k: {t: len(f) for t, f in v.items()} for k, v in theme_tone.items()},
    }


def assess_collection(reports: list[dict]) -> tuple[bool, str]:
    """수집 상태 판정 → (정상 여부, 사유). 2026-10 네이버 페이지 이전으로 0건이 된 걸 아무도 몰랐던 사고 대응."""
    if not reports:
        return False, "증권사 리포트 0건 — 수집처 구조 변경/차단 의심"
    macro = [r for r in reports if r.get("type") in MACRO_TYPES]
    if not macro:
        return False, f"시장·매크로 리포트 0건 (전체 {len(reports)}건은 종목분석뿐)"
    return True, f"리포트 {len(reports)}건 (매크로 {len(macro)}건)"


def record_collection_health(reports: list[dict]) -> None:
    """수집 상태를 branch:report_sources로 기록 (fail이면 daily_health가 경보). 기록 실패는 삼킨다."""
    try:
        ok, detail = assess_collection(reports)
        from services.job_ledger import record_job
        record_job("branch:report_sources", "success" if ok else "fail", detail)
    except Exception as e:
        logger.warning("[리포트다이제스트] 수집상태 기록 실패: %s", e)


def store_reports(reports: list[dict], source: str = "naver") -> int:
    """리포트를 DB에 보관(중복은 건너뜀). 저장된 신규 건수 반환. 실패해도 예외 없음."""
    if not reports:
        return 0
    try:
        from sqlalchemy import text
        from db.database import get_conn
        n = 0
        with get_conn() as conn:
            for r in enrich(reports):
                if not r.get("link"):
                    continue
                res = conn.execute(
                    text(
                        "INSERT INTO broker_reports (source, link, rtype, firm, title, stock, report_date, themes, tone) "
                        "VALUES (:s,:l,:t,:f,:ti,:st,:d,:th,:to) ON CONFLICT (source, link) DO NOTHING"
                    ),
                    {"s": source, "l": r["link"], "t": r.get("type"), "f": r.get("firm"),
                     "ti": r.get("title"), "st": r.get("stock"), "d": r.get("date"),
                     "th": ",".join(r["themes"]), "to": r["tone"]},
                )
                n += res.rowcount or 0
        return n
    except Exception as e:
        logger.warning("[리포트다이제스트] 저장 실패: %s", e)
        return 0


def _past_theme_counts(days: int = 7) -> tuple[dict[str, int], int]:
    """최근 days일(오늘 제외) 보관분의 테마별 '증권사 수' 합계와 보관된 일수. 보관 없으면 ({}, 0)."""
    try:
        from sqlalchemy import text
        from db.database import get_conn
        since = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT report_date, firm, themes FROM broker_reports "
                     "WHERE rtype IN ('경제분석','투자전략','산업분석') AND report_date>=:s AND report_date<:t"),
                {"s": since, "t": today},
            ).fetchall()
        per_day_theme_firms: dict[tuple, set] = defaultdict(set)
        days_seen = set()
        for d, firm, themes in rows:
            days_seen.add(d)
            for th in (themes or "").split(","):
                if th:
                    per_day_theme_firms[(d, th)].add(firm)
        totals: dict[str, int] = defaultdict(int)
        for (d, th), f in per_day_theme_firms.items():
            totals[th] += len(f)
        return dict(totals), len(days_seen)
    except Exception as e:
        logger.debug("[리포트다이제스트] 과거 집계 실패: %s", e)
        return {}, 0


def build_digest(reports: list[dict]) -> str:
    """LLM 컨텍스트용 다이제스트. 코드가 센 숫자만 담고, 참고용·편향 한계를 명시한다."""
    s = summarize_today(reports)
    if not s["n_macro"]:
        return ""
    past, past_days = _past_theme_counts()

    lines = [
        "=== 증권사 리포트 집계 (코드 계산 · 참고용) ===",
        f"시장·매크로 리포트 {s['n_macro']}건 / 증권사 {s['n_firms']}곳 (제목 키워드 기반 거친 분류 — 본문을 읽은 것이 아님)",
    ]
    ranked = sorted(s["theme_firms"].items(), key=lambda kv: kv[1], reverse=True)
    for th, nf in ranked:
        tones = s["theme_tone"].get(th, {})
        tone_txt = " ".join(f"{t} {n}곳" for t, n in sorted(tones.items(), key=lambda kv: -kv[1]))
        trend = ""
        if past_days >= 3:
            avg = past.get(th, 0) / past_days
            if avg >= 1 and nf >= avg * 1.5:
                trend = f"  ↑ 최근 {past_days}일 일평균 {avg:.1f}곳보다 언급 늘어남"
            elif avg >= 2 and nf <= avg * 0.5:
                trend = f"  ↓ 최근 {past_days}일 일평균 {avg:.1f}곳보다 언급 줄어듦"
        lines.append(f"  - {th}: {nf}곳 언급 ({tone_txt}){trend}")

    lines.append(
        "해석 주의: 증권사 의견은 낙관 편향이 있고 컨센서스는 시장을 뒤따르는 경향이 있다. "
        "다수가 같은 방향이면 '쏠림'일 수 있으니 결론이 아닌 반박·교차검증 재료로만 쓸 것. "
        "위에 없는 수치·증권사 의견을 만들어내지 말 것."
    )
    return "\n".join(lines)
