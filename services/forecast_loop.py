"""구조화 예측 → 채점 → 즉시 반성 → 교훈 효과 검증 → 순환 리포트 (2026-10-13).

학습 고리의 빠진 조각들을 채운다:
- 기록: 기존 market_predictions의 '방향'은 CEO 스탠스 환산값이라 채점이 공정하지 않았다. 여기서는 독립 예측가(다른
  계열 모델)가 '방향·확률·예상 범위·틀리는 조건'을 채점 가능한 형태로 내고 forecast_log에 기록한다.
- 반성: 다음 날 코드가 실제 등락과 대조해 오차(방향·폭)를 분석한 한 줄 반성을 남긴다 (LLM 아님).
- 발전: 교훈이 생긴 뒤 적중률이 실제로 나아졌는지 전/후를 비교해 교훈을 확정·보류·폐기한다.
- 회고: 주간 순환 리포트 한 통(예측→결과→반성→교훈 상태→바뀌는 점)으로 묶는다.

원칙: 숫자·채점·판정은 코드, LLM은 예측 서술만. 표본이 작으면 결론을 내리지 않고 '보류'로 둔다.
"""
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

NEUTRAL_BAND = 0.3
MIN_AFTER_FOR_VERDICT = 8       # 교훈 효과를 판정하려면 생성 후 채점된 예측이 이만큼은 있어야 한다
CONFIRM_MARGIN = 5.0            # %p
RETIRE_MARGIN = 10.0            # %p

_FORECAST_SYSTEM = """당신은 KOSPI 당일 등락을 예측하는 독립 예측가입니다. 투자 자문 시스템의 CEO와 별개로 판단합니다.
아래 시장 수치와 '코드가 계산한 기저율·성적표·교훈'만 근거로, 오늘 KOSPI 종가의 전일 대비 등락을 예측하세요.

[규칙]
- 입력에 없는 수치·사건을 만들지 마세요. 근거는 입력의 숫자로 한 문장.
- 기저율을 먼저 보세요: 최근 횡보일이 드문 시장이면 '횡보'를 기본값으로 삼지 마세요.
- 확률은 '방향이 맞을 확률'(50~85). 근거가 약하면 50~60. 과신 금지 — 지난 성적표가 나쁘면 더 낮추세요.
- 예상범위는 종가 등락률(%)의 [하단, 상단]. 폭은 최근 평균 하루 변동폭을 참고해 현실적으로.
- '틀리는 조건'은 이 예측이 틀렸다고 판단할 구체적 신호 한 문장.
JSON 객체만 출력(설명·코드블록 금지):
{"방향":"상승|하락|횡보","확률":65,"하단":-2.5,"상단":0.5,"근거":"...","틀리는조건":"..."}"""


# ── 입력 요약 ─────────────────────────────────────────────────────────

def market_line(raw_mkt: dict) -> str:
    """raw_market_data에서 예측가용 핵심 수치 한 덩어리. 없는 값은 생략."""
    labels = [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"), ("sp500", "S&P500"), ("nasdaq", "나스닥"),
              ("sox", "SOX"), ("usd_krw", "USD/KRW"), ("vix", "VIX"), ("us10y", "미10Y"),
              ("sp500_futures", "S&P선물"), ("nasdaq_futures", "나스닥선물"), ("ewy", "EWY")]
    parts = []
    for key, label in labels:
        d = (raw_mkt or {}).get(key) or {}
        v = d.get("close") or d.get("price")
        c = d.get("change_pct") if d.get("change_pct") is not None else d.get("realtime_pct")
        if v is None:
            continue
        parts.append(f"{label} {v}" + (f"({c:+.2f}%)" if isinstance(c, (int, float)) else ""))
    return " | ".join(parts)


def parse_forecast(raw: str) -> dict | None:
    """LLM JSON → 검증된 예측 dict. 형식·논리 위반이면 None (조용히 쓰지 않고 호출부가 기록)."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        direction = str(d["방향"]).strip()
        prob = float(d["확률"])
        lo, hi = float(d["하단"]), float(d["상단"])
    except (KeyError, ValueError, TypeError):
        return None
    if direction not in ("상승", "하락", "횡보") or not (50 <= prob <= 99) or lo >= hi or hi - lo > 12:
        return None
    # 방향과 범위가 모순이면 폐기 (예: 상승인데 범위 전체가 음수)
    if direction == "상승" and hi <= 0 or direction == "하락" and lo >= 0:
        return None
    return {"direction": direction, "prob": prob, "low": lo, "high": hi,
            "reason": str(d.get("근거", ""))[:200], "wrong_if": str(d.get("틀리는조건", ""))[:200]}


def make_forecast(raw_mkt: dict, learning_ctx: str, chat_fn=None) -> dict | None:
    if chat_fn is None:
        from clients.openai_client import chat_independent as chat_fn
    ctx = f"[시장 수치] {market_line(raw_mkt)}\n\n{learning_ctx}".strip()
    fc = parse_forecast(chat_fn(_FORECAST_SYSTEM, ctx, max_tokens=400))
    if fc:
        fc["model"] = "independent"
    return fc


# ── 기록 ──────────────────────────────────────────────────────────────

def record_forecast(date: str, run_type: str, fc: dict, prev_chg: float | None, lesson_ids: list[int]) -> bool:
    params = {"d": date, "r": run_type, "dir": fc["direction"], "p": fc["prob"], "lo": fc["low"], "hi": fc["high"],
              "re": fc["reason"], "wi": fc["wrong_if"], "m": fc.get("model", ""), "pc": prev_chg,
              "lu": ",".join(str(i) for i in lesson_ids)}
    try:
        with get_conn() as conn:
            ex = conn.execute(text("SELECT id FROM forecast_log WHERE date=:d AND run_type=:r"), params).fetchone()
            if ex:
                conn.execute(text("UPDATE forecast_log SET direction=:dir, prob=:p, low_pct=:lo, high_pct=:hi, reason=:re, "
                                  "wrong_if=:wi, model=:m, prev_chg=:pc, lessons_used=:lu WHERE id=:i"),
                             {**params, "i": ex[0]})
            else:
                conn.execute(text("INSERT INTO forecast_log (date, run_type, direction, prob, low_pct, high_pct, reason, "
                                  "wrong_if, model, prev_chg, lessons_used) VALUES (:d,:r,:dir,:p,:lo,:hi,:re,:wi,:m,:pc,:lu)"),
                             params)
        return True
    except Exception as e:
        logger.warning("[예측일지] 기록 실패: %s", e)
        return False


def forecast_for_briefing(date: str, run_type: str, raw_mkt: dict, learning_ctx: str) -> str:
    """CEO 입력용 '독립 예측가 의견' 블록을 만들고 예측을 일지에 기록. 실패 시 빈 문자열(브리핑을 막지 않는다)."""
    try:
        fc = make_forecast(raw_mkt, learning_ctx)
        if not fc:
            logger.warning("[예측일지] 예측 형식 불량 — 이번 회차 건너뜀")
            return ""
        prev = None
        try:
            from services.market_regime import fetch_closes, regime_stats
            st = regime_stats(fetch_closes())
            prev = st.get("last_chg") if st else None
        except Exception:
            pass
        from services.learning_loop import active_lessons
        ids = [x["id"] for x in active_lessons() if x.get("id") is not None]
        record_forecast(date, run_type, fc, prev, ids)
        return (f"[독립 예측가 의견 — 다른 계열 모델이 별도로 낸 오늘 KOSPI 예측. 기록·채점되며, CEO 판단과 다르면 이유를 한 줄로 밝혀라]\n"
                f"{fc['direction']} {fc['prob']:.0f}% · 예상 등락 {fc['low']:+.1f}%~{fc['high']:+.1f}%\n"
                f"근거: {fc['reason']}\n틀리는 조건: {fc['wrong_if']}")
    except Exception as e:
        logger.warning("[예측일지] 예측 생성 실패 (무시): %s", e)
        return ""


# ── 채점 + 즉시 반성 (LLM 없음) ─────────────────────────────────────────

def _dir_of(chg: float) -> str:
    return "상승" if chg >= NEUTRAL_BAND else ("하락" if chg <= -NEUTRAL_BAND else "횡보")


def score_one(fc: dict, actual: float) -> dict:
    """예측 1건 채점 + 코드가 쓰는 반성 한 줄."""
    adir = _dir_of(actual)
    dir_hit = 1 if fc["direction"] == adir else 0
    range_hit = 1 if fc["low_pct"] <= actual <= fc["high_pct"] else 0
    p = fc["prob"] / 100
    brier = round((p - dir_hit) ** 2, 4)
    width = fc["high_pct"] - fc["low_pct"]
    if range_hit:
        amp = "범위 안"
    elif abs(actual) > max(abs(fc["low_pct"]), abs(fc["high_pct"])):
        amp = f"변동폭 과소평가(예상 최대 {max(abs(fc['low_pct']), abs(fc['high_pct'])):.1f}% vs 실제 {abs(actual):.1f}%)"
    else:
        amp = "범위 이탈(방향 편향)"
    sure = "과신" if (not dir_hit and fc["prob"] >= 65) else ("적절" if dir_hit else "약한 확신 실패")
    refl = (f"예측 {fc['direction']} {fc['prob']:.0f}% [{fc['low_pct']:+.1f}~{fc['high_pct']:+.1f}] → 실제 {actual:+.2f}%({adir}): "
            f"방향 {'적중' if dir_hit else '실패'}, {amp}, 확신 {sure}")
    return {"actual_chg": actual, "dir_hit": dir_hit, "range_hit": range_hit, "brier": brier, "reflection": refl}


def score_forecasts(today: str | None = None, fetcher=None) -> int:
    """미채점 예측을 KOSPI 실제 등락으로 채점하고 반성 한 줄을 남긴다. 반환: 채점 건수."""
    from services.learning_loop import daily_changes, fetch_kospi_closes
    fetcher = fetcher or fetch_kospi_closes
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT id, date, direction, prob, low_pct, high_pct FROM forecast_log "
                                     "WHERE actual_chg IS NULL AND date < :t ORDER BY date"), {"t": today}).fetchall()
    except Exception as e:
        logger.warning("[예측일지] 미채점 조회 실패: %s", e)
        return 0
    if not rows:
        return 0
    chg = daily_changes(fetcher(rows[0][1], today))
    done = 0
    try:
        with get_conn() as conn:
            for fid, d, direction, prob, lo, hi in rows:
                if d not in chg:
                    continue
                r = score_one({"direction": direction, "prob": prob, "low_pct": lo, "high_pct": hi}, chg[d])
                conn.execute(text("UPDATE forecast_log SET actual_chg=:a, dir_hit=:dh, range_hit=:rh, brier=:b, "
                                  "reflection=:rf WHERE id=:i"),
                             {"a": r["actual_chg"], "dh": r["dir_hit"], "rh": r["range_hit"], "b": r["brier"],
                              "rf": r["reflection"], "i": fid})
                done += 1
    except Exception as e:
        logger.warning("[예측일지] 채점 저장 실패: %s", e)
    if done:
        logger.info("[예측일지] %d건 채점·반성 기록", done)
    return done


# ── 성적표 / 기준선 ─────────────────────────────────────────────────────

def load_scored(limit: int = 200) -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT id, date, direction, prob, low_pct, high_pct, prev_chg, actual_chg, dir_hit, "
                                     "range_hit, brier, reflection, reason, wrong_if FROM forecast_log "
                                     "WHERE actual_chg IS NOT NULL ORDER BY date DESC LIMIT :n"), {"n": limit}).fetchall()
        keys = ("id", "date", "direction", "prob", "low_pct", "high_pct", "prev_chg", "actual_chg", "dir_hit",
                "range_hit", "brier", "reflection", "reason", "wrong_if")
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.debug("[예측일지] 채점분 조회 실패: %s", e)
        return []


def scorecard(rows: list[dict]) -> dict:
    n = len(rows)
    sc: dict = {"n": n}
    if not n:
        return sc
    sc["dir_pct"] = round(sum(r["dir_hit"] for r in rows) / n * 100)
    sc["range_pct"] = round(sum(r["range_hit"] for r in rows) / n * 100)
    sc["brier"] = round(sum(r["brier"] for r in rows) / n, 3)     # 0.25 = 동전 던지기 수준
    # 기준선 1: 전일과 같은 방향이 이어진다고 찍기 / 기준선 2: 실제 최빈 방향만 찍기
    prev = [r for r in rows if r.get("prev_chg") is not None]
    if prev:
        sc["persist_pct"] = round(sum(1 for r in prev if _dir_of(r["prev_chg"]) == _dir_of(r["actual_chg"])) / len(prev) * 100)
    cnt: dict[str, int] = {}
    for r in rows:
        cnt[_dir_of(r["actual_chg"])] = cnt.get(_dir_of(r["actual_chg"]), 0) + 1
    sc["major_pct"] = round(max(cnt.values()) / n * 100)
    sc["baseline_pct"] = max(sc["major_pct"], sc.get("persist_pct", 0))
    sc["edge"] = sc["dir_pct"] - sc["baseline_pct"]
    return sc


def format_scorecard_line(sc: dict) -> str:
    if not sc.get("n"):
        return ""
    s = (f"독립 예측가 성적: {sc['n']}건 방향 적중 {sc['dir_pct']}% (기준선 최대 {sc['baseline_pct']}% → 우위 {sc['edge']:+d}%p), "
         f"범위 포함 {sc['range_pct']}%, 확률 정확도(Brier) {sc['brier']} (0.25=동전 수준)")
    return s + (" ※ 표본이 적어 우연일 수 있음" if sc["n"] < 20 else "")


# ── 교훈 효과 검증 (발전) ───────────────────────────────────────────────

def lesson_verdict(n_before: int, hit_before: float | None, n_after: int, hit_after: float | None) -> str:
    """교훈 생성 전/후 예측 적중 비교로 상태 판정. 표본 부족이면 pending — 결론을 내지 않는다."""
    if n_after < MIN_AFTER_FOR_VERDICT or hit_after is None:
        return "pending"
    if n_before < MIN_AFTER_FOR_VERDICT or hit_before is None:
        return "pending"          # 비교 대상이 없으면 효과를 말할 수 없다
    diff = hit_after - hit_before
    if diff >= CONFIRM_MARGIN:
        return "confirmed"
    if diff <= -RETIRE_MARGIN:
        return "retired"
    return "neutral"


def evaluate_lessons(rows: list[dict] | None = None) -> list[dict]:
    """활성 교훈마다 생성일 전/후 적중률을 계산해 상태를 갱신. 반환: 갱신된 교훈 목록."""
    rows = load_scored() if rows is None else rows
    out = []
    try:
        with get_conn() as conn:
            lessons = conn.execute(text("SELECT id, date, lesson FROM learning_lessons WHERE active=1 ORDER BY id")).fetchall()
            for lid, ldate, lesson in lessons:
                before = [r for r in rows if r["date"] < ldate]
                after = [r for r in rows if r["date"] >= ldate]
                hb = round(sum(r["dir_hit"] for r in before) / len(before) * 100, 1) if before else None
                ha = round(sum(r["dir_hit"] for r in after) / len(after) * 100, 1) if after else None
                st = lesson_verdict(len(before), hb, len(after), ha)
                conn.execute(text("UPDATE learning_lessons SET status=:s, n_before=:nb, hit_before=:hb, n_after=:na, "
                                  "hit_after=:ha, active=:a WHERE id=:i"),
                             {"s": st, "nb": len(before), "hb": hb, "na": len(after), "ha": ha,
                              "a": 0 if st == "retired" else 1, "i": lid})
                out.append({"id": lid, "date": ldate, "lesson": lesson, "status": st, "n_before": len(before),
                            "hit_before": hb, "n_after": len(after), "hit_after": ha})
    except Exception as e:
        logger.warning("[예측일지] 교훈 효과 검증 실패: %s", e)
    return out


_STATUS_KR = {"pending": "검증 대기(표본 부족)", "confirmed": "효과 확인", "neutral": "효과 불분명",
              "retired": "효과 없어 폐기", "new": "신규"}


# ── 주간 순환 리포트 ────────────────────────────────────────────────────

def format_cycle_section(rows: list[dict], sc: dict, lessons: list[dict]) -> str:
    """예측→결과→반성→교훈 상태를 코드로 조립한 순환 섹션. 보여줄 것이 없으면 빈 문자열."""
    if not rows and not lessons:
        return ""
    lines = ["🔁 *학습 순환 점검*", ""]
    if rows:
        lines.append("① 예측 → 결과 → 반성 (최근)")
        for r in rows[:4]:
            lines.append(f"  {r['date']} {r['reflection']}")
        lines += ["", "② " + format_scorecard_line(sc)]
        worst = max(rows, key=lambda r: abs(r["actual_chg"] - (r["low_pct"] + r["high_pct"]) / 2))
        lines.append(f"③ 가장 크게 빗나간 예측: {worst['date']} (틀리는 조건으로 적어둔 것: {worst['wrong_if'] or '없음'})")
    if lessons:
        lines += ["", "④ 교훈의 효과 (생성 전/후 적중률 비교)"]
        for ls in lessons[:4]:
            tail = ""
            if ls["hit_before"] is not None and ls["hit_after"] is not None:
                tail = f" — 전 {ls['hit_before']:.0f}%({ls['n_before']}) → 후 {ls['hit_after']:.0f}%({ls['n_after']})"
            lines.append(f"  [{_STATUS_KR.get(ls['status'], ls['status'])}] {ls['lesson'][:60]}{tail}")
        lines.append("  ※ 여러 교훈이 동시에 작용해 개별 효과는 섞인다. 표본 8건 미만이면 판정하지 않는다")
    return "\n".join(lines)
