"""학습 고리 — 과거 판단을 채점하고, 복기해서 교훈을 저장하고, 다음 판단에 되먹임한다 (2026-10-13).

배경: 기존 구조는 '결과 → 다음 판단' 연결이 사실상 없었다. 시장 방향 예측은 50건 중 22건만
채점돼 있었고(마감 스냅샷이 금요일에만 있어 월·수 예측은 영영 미채점), 복기는 매번 백지에서
시작했다. 여기서는 ① 과거 예측 전부를 KOSPI 실제 등락으로 소급 채점하고(코드)
② 적중률·편향·확률 보정을 코드로 계산하고 ③ LLM이 오답을 복기해 교훈을 쓰고(서술만)
④ 교훈을 DB에 저장해 CEO 입력에 되먹임한다.

원칙: 숫자는 코드, LLM은 서술. 표본이 작으면 '우연일 수 있음'을 코드가 붙인다. 교훈은
유효기간이 있고(기본 60일), 현재 데이터와 충돌하면 현재 데이터가 우선이라고 명시한다.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

NEUTRAL_BAND = 0.3          # |등락률| 이 값 미만이면 '중립' — market_prediction_service와 동일
MIN_SCORED_FOR_REFLECT = 8  # 이보다 적으면 복기해도 우연과 구분 불가 → 교훈 생성 안 함
MIN_PER_BUCKET = 4          # 방향별·확률대별 해석 최소 표본
LESSON_TTL_DAYS = 60
MAX_PROMPT_LESSONS = 4


# ── 소급 채점 ─────────────────────────────────────────────────────────

def _to_direction(chg: float) -> str:
    return "상승" if chg >= NEUTRAL_BAND else ("하락" if chg <= -NEUTRAL_BAND else "중립")


def daily_changes(closes: dict[str, float]) -> dict[str, float]:
    """{날짜: 종가} → {날짜: 전일 대비 등락률(%)}. 첫 날은 전일이 없어 제외."""
    out: dict[str, float] = {}
    prev = None
    for d in sorted(closes):
        px = closes[d]
        if prev and prev > 0:
            out[d] = round((px - prev) / prev * 100, 2)
        prev = px
    return out


def fetch_kospi_closes(start: str, end: str) -> dict[str, float]:
    """yfinance ^KS11 일봉 종가. 실패 시 빈 dict (호출부가 채점을 건너뛴다)."""
    try:
        import yfinance as yf
        h = yf.Ticker("^KS11").history(start=start, end=end, interval="1d")
        return {idx.strftime("%Y-%m-%d"): float(r["Close"]) for idx, r in h.iterrows()}
    except Exception as e:
        logger.warning("[학습고리] KOSPI 일봉 조회 실패: %s", e)
        return {}


def backfill_market_outcomes(today: str | None = None, fetcher=fetch_kospi_closes) -> int:
    """아직 채점 안 된 과거 시장 예측을 KOSPI 실제 등락으로 채운다. 반환: 채점 건수."""
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT id, date, predicted_dir FROM market_predictions "
                     "WHERE correct IS NULL AND date < :t ORDER BY date"),
                {"t": today}).fetchall()
    except Exception as e:
        logger.warning("[학습고리] 미채점 예측 조회 실패: %s", e)
        return 0
    if not rows:
        return 0
    first = datetime.strptime(rows[0][1], "%Y-%m-%d") - timedelta(days=7)
    closes = fetcher(first.strftime("%Y-%m-%d"), today)
    chg = daily_changes(closes)
    done = 0
    try:
        with get_conn() as conn:
            for pid, d, pdir in rows:
                if d not in chg:       # 휴장일이거나 데이터 없음 — 건드리지 않는다
                    continue
                adir = _to_direction(chg[d])
                conn.execute(
                    text("UPDATE market_predictions SET actual_kospi=:c, actual_dir=:a, correct=:ok "
                         "WHERE id=:i"),
                    {"c": chg[d], "a": adir, "ok": 1 if pdir == adir else 0, "i": pid})
                done += 1
    except Exception as e:
        logger.warning("[학습고리] 소급 채점 저장 실패: %s", e)
    if done:
        logger.info("[학습고리] 시장 예측 소급 채점 %d건", done)
    return done


# ── 성적표 (순수 계산) ─────────────────────────────────────────────────

def market_scorecard(rows: list[dict]) -> dict:
    """채점된 예측(rows: date,predicted_dir,predicted_prob,actual_dir,actual_kospi,correct)의 성적표."""
    scored = [r for r in rows if r.get("correct") is not None and r.get("actual_dir")]
    n = len(scored)
    sc: dict = {"n": n}
    if n == 0:
        return sc
    hits = sum(1 for r in scored if r["correct"] == 1)
    sc["hits"], sc["hit_pct"] = hits, round(hits / n * 100)
    # 기준선: 항상 실제 최빈 방향만 찍는 단순 전략 — 이걸 못 넘으면 예측이 가치 없다
    actual_cnt: dict[str, int] = {}
    for r in scored:
        actual_cnt[r["actual_dir"]] = actual_cnt.get(r["actual_dir"], 0) + 1
    top_dir, top_n = max(actual_cnt.items(), key=lambda kv: kv[1])
    sc["baseline_dir"], sc["baseline_pct"] = top_dir, round(top_n / n * 100)
    pred_cnt: dict[str, int] = {}
    for r in scored:
        pred_cnt[r["predicted_dir"]] = pred_cnt.get(r["predicted_dir"], 0) + 1
    sc["pred_dist"], sc["actual_dist"] = pred_cnt, actual_cnt
    by_pred = {}
    for d in ("상승", "하락", "중립"):
        sub = [r for r in scored if r["predicted_dir"] == d]
        if len(sub) >= MIN_PER_BUCKET:
            by_pred[d] = {"n": len(sub), "hit_pct": round(sum(1 for r in sub if r["correct"] == 1) / len(sub) * 100)}
    sc["by_pred"] = by_pred
    # 확률 보정: 자신 있게(>=60%) 말한 예측이 실제로 더 맞았는가
    with_p = [r for r in scored if r.get("predicted_prob") is not None]
    hi = [r for r in with_p if r["predicted_prob"] >= 60]
    lo = [r for r in with_p if r["predicted_prob"] < 60]
    if len(hi) >= MIN_PER_BUCKET and len(lo) >= MIN_PER_BUCKET:
        sc["conf"] = {
            "hi_n": len(hi), "hi_hit": round(sum(1 for r in hi if r["correct"] == 1) / len(hi) * 100),
            "lo_n": len(lo), "lo_hit": round(sum(1 for r in lo if r["correct"] == 1) / len(lo) * 100),
        }
    # 시나리오 기울기: 우호% - 비관%가 10%p 이상이면 그 방향으로 기운 예측으로 보고, 실제 방향(횡보 제외)과 대조
    lean = []
    for r in scored:
        bu, be = r.get("bull_prob"), r.get("bear_prob")
        if bu is None or be is None or abs(bu - be) < 10 or r["actual_dir"] == "중립":
            continue
        lean.append(("상승" if bu > be else "하락") == r["actual_dir"])
    if len(lean) >= MIN_PER_BUCKET:
        sc["lean"] = {"n": len(lean), "hit_pct": round(sum(lean) / len(lean) * 100)}
    # 큰 움직임(|등락| >= 1%)을 놓친 횟수
    big = [r for r in scored if r.get("actual_kospi") is not None and abs(r["actual_kospi"]) >= 1.0]
    if big:
        sc["big_moves"] = {"n": len(big), "hit": sum(1 for r in big if r["correct"] == 1)}
    return sc


def format_scorecard(sc: dict) -> str:
    """성적표를 프롬프트용 사실 목록으로. 표본이 부족하면 빈 문자열."""
    n = sc.get("n", 0)
    if n < MIN_SCORED_FOR_REFLECT:
        return ""
    lines = [
        "[시장 방향 예측 성적표 — 코드가 KOSPI 실제 등락과 소급 대조한 사실. 이 숫자만 인용]",
        "※ 기록된 '방향'은 순수한 시장 예측이 아니라 CEO 스탠스(공격→상승/중립/방어→하락)를 환산한 값이다 — 중립 스탠스의 적중률은 보수적으로 읽어라",
        f"채점된 예측 {n}건 중 적중 {sc['hits']}건 ({sc['hit_pct']}%) / "
        f"실제 최빈 방향('{sc['baseline_dir']}')만 찍는 단순 전략의 적중 {sc['baseline_pct']}%",
        "예측 분포 " + ", ".join(f"{k} {v}회" for k, v in sc["pred_dist"].items())
        + " ↔ 실제 분포 " + ", ".join(f"{k} {v}회" for k, v in sc["actual_dist"].items()),
    ]
    if sc.get("by_pred"):
        lines.append("예측 방향별 적중: " + " | ".join(
            f"{k} 예측 {v['hit_pct']}%({v['n']}건)" for k, v in sc["by_pred"].items()))
    if sc.get("lean"):
        lines.append(f"시나리오 기울기(우호-비관 확률차 10%p↑) 방향 적중: {sc['lean']['hit_pct']}%({sc['lean']['n']}건, 횡보일 제외)")
    if sc.get("conf"):
        c = sc["conf"]
        lines.append(f"자신감 보정: 확률 60%+ 예측 적중 {c['hi_hit']}%({c['hi_n']}건) vs 60% 미만 {c['lo_hit']}%({c['lo_n']}건)")
    if sc.get("big_moves"):
        b = sc["big_moves"]
        lines.append(f"±1% 이상 큰 움직임 {b['n']}번 중 방향 적중 {b['hit']}번")
    neu = sc.get("by_pred", {}).get("중립")
    if neu and neu["hit_pct"] <= 20:
        real_neu = sc["actual_dist"].get("중립", 0)
        lines.append(f"→ '중립' 스탠스는 {neu['n']}건 중 적중 {neu['hit_pct']}%: 실제로 ±{NEUTRAL_BAND}% 안에 머문 날은 "
                     f"{sc['n']}건 중 {real_neu}회뿐이다. 중립으로 뭉개지 말고 '어느 쪽으로 기울었는지'와 그 조건을 써라")
    if sc["hit_pct"] <= sc["baseline_pct"]:
        lines.append("→ 방향 예측이 '최빈 방향만 찍기'를 못 넘는다: 방향 확신을 낮추고 시나리오·조건부 서술을 우선하라")
    if n < 30:
        lines.append(f"※ 표본 {n}건은 적다: 위 수치는 경향일 뿐 확정 패턴이 아니다")
    return "\n".join(lines)


def load_scored_predictions(limit: int = 120) -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT date, run_type, predicted_dir, predicted_prob, actual_dir, actual_kospi, "
                     "correct, tail_risk, miss_reason, bull_prob, bear_prob FROM market_predictions "
                     "WHERE correct IS NOT NULL ORDER BY date DESC LIMIT :n"), {"n": limit}).fetchall()
        keys = ("date", "run_type", "predicted_dir", "predicted_prob", "actual_dir", "actual_kospi",
                "correct", "tail_risk", "miss_reason", "bull_prob", "bear_prob")
        return [dict(zip(keys, r)) for r in rows]
    except Exception as e:
        logger.warning("[학습고리] 채점 예측 조회 실패: %s", e)
        return []


# ── 교훈 저장소 ───────────────────────────────────────────────────────

def save_lessons(lessons: list[dict], date: str) -> int:
    """lessons: [{"kind","lesson","evidence"}]. 같은 문장은 중복 저장 안 함. 반환: 저장 건수."""
    saved = 0
    exp = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=LESSON_TTL_DAYS)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            for ls in lessons:
                body = (ls.get("lesson") or "").strip()
                if not body:
                    continue
                dup = conn.execute(text("SELECT 1 FROM learning_lessons WHERE lesson=:l AND active=1"),
                                   {"l": body}).fetchone()
                if dup:
                    continue
                conn.execute(
                    text("INSERT INTO learning_lessons (date, kind, lesson, evidence, expires_at, active) "
                         "VALUES (:d,:k,:l,:e,:x,1)"),
                    {"d": date, "k": ls.get("kind", "market"), "l": body,
                     "e": (ls.get("evidence") or "")[:300], "x": exp})
                saved += 1
    except Exception as e:
        logger.warning("[학습고리] 교훈 저장 실패: %s", e)
    return saved


def active_lessons(limit: int = MAX_PROMPT_LESSONS, today: str | None = None) -> list[dict]:
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT date, kind, lesson, evidence FROM learning_lessons "
                     "WHERE active=1 AND (expires_at IS NULL OR expires_at >= :t) "
                     "ORDER BY id DESC LIMIT :n"), {"t": today, "n": limit}).fetchall()
        return [{"date": r[0], "kind": r[1], "lesson": r[2], "evidence": r[3]} for r in rows]
    except Exception as e:
        logger.debug("[학습고리] 교훈 조회 실패 (테이블 미생성 등): %s", e)
        return []


# ── 복기 (LLM은 서술만) ────────────────────────────────────────────────

_REFLECT_SYSTEM = """당신은 투자 판단 복기 담당입니다. 아래 '성적표'와 '빗나간 예측 사례'만 근거로
이 자문 시스템의 판단 습관을 점검하고, 다음 판단에 쓸 교훈을 도출하세요.

[절대 규칙]
- 입력에 있는 숫자만 인용하세요. 새 통계·점수·확률을 만들지 마세요. 없는 데이터는 '데이터 없음'.
- 표본이 작으면(성적표의 ※ 표시) '가능성'으로만 쓰고 단정하지 마세요.
- 교훈은 '다음 브리핑에서 무엇을 다르게 할지' 행동으로 쓰세요. 추상적 훈계 금지.

출력은 JSON 배열만(설명·코드블록 금지). 항목 2개, 서로 다른 주제(같은 취지 반복 금지). 각 항목은 아래 키를 모두 가집니다:
[{"분석":"무엇이 관찰됐나(숫자 인용)","사고":"왜 그랬을 가능성이 큰가","판단":"그래서 어떤 습관이 문제인가",
  "교훈":"다음엔 이렇게 한다(행동)","의견":"사용자에게 드리는 한 줄 의견"}]"""


def _miss_cases(rows: list[dict], k: int = 6) -> str:
    misses = [r for r in rows if r.get("correct") == 0]
    lines = []
    for r in misses[:k]:
        prob = f"{r['predicted_prob']:.0f}%" if r.get("predicted_prob") is not None else "확률 미기재"
        lines.append(f"- {r['date']} 예측 {r['predicted_dir']}({prob}) → 실제 {r['actual_dir']}({r['actual_kospi']:+.2f}%)"
                     + (f" | 당시 꼬리위험 서술: {(r.get('tail_risk') or '')[:90]}" if r.get("tail_risk") else "")
                     + (f" | 원인 조사: {r['miss_reason'][:160]}" if r.get("miss_reason") else ""))
    return "[빗나간 예측 사례]\n" + ("\n".join(lines) if lines else "없음")


def parse_reflection(raw: str) -> list[dict]:
    """LLM 출력(JSON 배열) → 교훈 레코드. 형식이 깨졌으면 빈 리스트(조용히 넘어가지 않고 호출부가 기록)."""
    m = re.search(r"\[.*\]", raw or "", re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("교훈"):
            continue
        out.append({
            "kind": "market",
            "lesson": str(it["교훈"]).strip(),
            "evidence": f"분석: {it.get('분석', '')} / 판단: {it.get('판단', '')}",
            "opinion": str(it.get("의견", "")).strip(),
            "thought": str(it.get("사고", "")).strip(),
            "analysis": str(it.get("분석", "")).strip(),
        })
    return out[:2]


def reflect(rows: list[dict], sc: dict, chat_fn=None) -> list[dict]:
    """성적표+오답 사례를 LLM에 주고 교훈을 받는다. 표본 부족이면 빈 리스트."""
    facts = format_scorecard(sc)
    if not facts:
        return []
    if chat_fn is None:
        from clients.openai_client import chat_independent as chat_fn   # 자기 판단을 다른 계열 모델이 복기
    ctx = facts + "\n\n" + _miss_cases(rows)
    try:
        from services.decision_scoring import build_context as _dec_ctx
        _dc = _dec_ctx()
        if _dc:
            ctx += "\n\n" + _dc
    except Exception:
        pass
    raw = chat_fn(_REFLECT_SYSTEM, ctx, max_tokens=900)
    lessons = parse_reflection(raw)
    # 근거 대조: 교훈이 입력에 없는 숫자를 들먹이면 그 항목은 버린다
    try:
        from services.output_fact_check import check_report_numbers
        lessons = [ls for ls in lessons
                   if check_report_numbers(ls["lesson"] + " " + ls["analysis"], ctx)["count"] == 0]
    except Exception:
        pass
    return lessons


# ── 자율 조사: 크게 빗나간 날의 원인을 그날의 데이터로 추적 ───────────────────

BIG_MISS_PCT = 1.0
MAX_INVESTIGATIONS_PER_RUN = 5     # LLM 호출 상한 (주 1회 실행 기준 비용 제한)

_INVEST_SYSTEM = """당신은 시장 예측 실패 원인 조사관입니다. '예측 당시 보였던 조건'과 '실제 결과'만 보고,
왜 빗나갔는지 가설을 1~2문장으로 쓰세요.
- 입력에 있는 수치·뉴스만 근거로 삼으세요. 없는 사건이나 숫자를 만들지 마세요.
- 반드시 '가설:'로 시작하고, 예측 당시 이미 보였던 신호를 놓친 것인지(놓친 신호를 구체적으로),
  당시엔 알 수 없던 충격이었는지 구분하세요. 근거가 부족하면 '근거 부족'이라고 쓰세요."""


def build_investigation_facts(pred: dict, before: dict | None, after: dict | None, intel: list[str]) -> str:
    """예측 1건의 조사용 사실 묶음 (순수 함수). before=예측 당일 장전 스냅샷, after=다음 영업일 장전 스냅샷."""
    def snap(s):
        if not s:
            return "데이터 없음"
        return (f"전일 KOSPI {s.get('kospi_chg')}% · USD/KRW {s.get('usd_krw')} · VIX {s.get('vix')} · "
                f"미10년물 {s.get('us10y')} · 미 S&P {s.get('sp500_chg')}% · 나스닥 {s.get('nasdaq_chg')}%")
    lines = [
        f"[예측] {pred['date']} {pred['predicted_dir']}"
        + (f"({pred['predicted_prob']:.0f}%)" if pred.get("predicted_prob") is not None else ""),
        f"[실제] KOSPI {pred['actual_kospi']:+.2f}% ({pred['actual_dir']})",
        f"[예측 당시 시장 조건] {snap(before)}",
        f"[다음 영업일 장전 조건] {snap(after)}",
    ]
    if pred.get("tail_risk"):
        lines.append(f"[당시 꼬리위험 서술] {pred['tail_risk'][:200]}")
    if intel:
        lines.append("[당시 수집된 정보 요약] " + " / ".join(i[:140] for i in intel[:3]))
    return "\n".join(lines)


def _snap(conn, date: str, after: bool = False):
    sql = ("SELECT kospi_chg, usd_krw, vix, us10y, sp500_chg, nasdaq_chg FROM market_snapshots "
           "WHERE run_type='pre_market' AND date " + (">" if after else "=") + " :d ORDER BY date "
           + ("ASC" if after else "DESC") + " LIMIT 1")
    r = conn.execute(text(sql), {"d": date}).fetchone()
    return dict(zip(("kospi_chg", "usd_krw", "vix", "us10y", "sp500_chg", "nasdaq_chg"), r)) if r else None


def investigate_misses(chat_fn=None, max_n: int = MAX_INVESTIGATIONS_PER_RUN) -> int:
    """큰 폭(±1%↑)으로 빗나간 예측 중 원인 미조사 건을 조사해 market_predictions.miss_reason에 기록."""
    if chat_fn is None:
        from clients.openai_client import chat_independent as chat_fn
    done = 0
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT id, date, predicted_dir, predicted_prob, actual_dir, actual_kospi, tail_risk "
                     "FROM market_predictions WHERE correct=0 AND ABS(actual_kospi) >= :b "
                     "AND (miss_reason IS NULL OR miss_reason='') ORDER BY date DESC LIMIT :n"),
                {"b": BIG_MISS_PCT, "n": max_n}).fetchall()
            cases = []
            for r in rows:
                pred = dict(zip(("id", "date", "predicted_dir", "predicted_prob", "actual_dir",
                                 "actual_kospi", "tail_risk"), r))
                intel = [x[0] for x in conn.execute(
                    text("SELECT summary FROM intelligence_archive WHERE date=:d AND summary IS NOT NULL LIMIT 3"),
                    {"d": pred["date"]}).fetchall()]
                cases.append((pred, _snap(conn, pred["date"]), _snap(conn, pred["date"], after=True), intel))
    except Exception as e:
        logger.warning("[학습고리] 조사 대상 조회 실패: %s", e)
        return 0
    for pred, before, after, intel in cases:
        try:
            facts = build_investigation_facts(pred, before, after, intel)
            why = (chat_fn(_INVEST_SYSTEM, facts, max_tokens=200) or "").strip()
            if not why.startswith("가설"):
                continue
            from services.output_fact_check import check_report_numbers
            if check_report_numbers(why, facts)["count"] > 0:      # 입력에 없는 숫자를 지어냈으면 폐기
                continue
            with get_conn() as conn:
                conn.execute(text("UPDATE market_predictions SET miss_reason=:m WHERE id=:i"),
                             {"m": "[AI 조사] " + why[:300], "i": pred["id"]})
            done += 1
        except Exception as e:
            logger.warning("[학습고리] 원인 조사 실패 (%s): %s", pred.get("date"), e)
    if done:
        logger.info("[학습고리] 빗나간 예측 원인 조사 %d건", done)
    return done


# ── 프롬프트 주입 / 주간 실행 ──────────────────────────────────────────

def format_for_prompt() -> str:
    """CEO 입력용: 성적표 + 활성 교훈. 아무것도 없으면 빈 문자열."""
    parts = []
    try:
        sc_txt = format_scorecard(market_scorecard(load_scored_predictions()))
        if sc_txt:
            parts.append(sc_txt)
    except Exception as e:
        logger.debug("[학습고리] 성적표 주입 실패: %s", e)
    try:
        from services.market_regime import build_context as _regime
        from services.decision_scoring import build_context as _decs
        for blk in (_regime(), _decs()):
            if blk:
                parts.append(blk)
    except Exception as e:
        logger.debug("[학습고리] 국면/판단채점 주입 실패: %s", e)
    ls = active_lessons()
    if ls:
        parts.append("[과거 복기에서 얻은 교훈 — 참고용. 오늘의 실제 데이터와 충돌하면 오늘 데이터가 우선]\n"
                     + "\n".join(f"- ({x['date']}) {x['lesson']}" for x in ls))
    return "\n\n".join(parts)


def format_weekly_message(sc: dict, lessons: list[dict]) -> str:
    """주간 복기 텔레그램 본문 (코드가 조립: 분석→사고→판단→교훈→의견)."""
    lines = ["🧠 *주간 복기·교훈*", ""]
    lines.append(f"• 시장 방향 예측: {sc['n']}건 채점, 적중 {sc['hit_pct']}% "
                 f"(최빈 방향만 찍기 {sc['baseline_pct']}%)")
    if sc["hit_pct"] <= sc["baseline_pct"]:
        lines.append("  → 아직 단순 전략을 못 넘음. 방향 확신을 낮추고 조건부로 서술하도록 조정")
    for i, ls in enumerate(lessons, 1):
        lines += ["", f"*{i}. 교훈*", f"분석: {ls.get('analysis', '')}", f"사고: {ls.get('thought', '')}",
                  f"판단→교훈: {ls['lesson']}", f"의견: {ls.get('opinion', '')}"]
    lines += ["", "_다음 브리핑부터 CEO 판단 입력에 반영됩니다(60일 유효)._"]
    return "\n".join(lines)


def run_weekly(date: str | None = None) -> str:
    """소급 채점 → 성적표 → 복기 → 교훈 저장 → (교훈이 생기면) 주간 복기 메시지 1통. 반환: job 상세."""
    date = date or datetime.now(_KST).strftime("%Y-%m-%d")
    filled = backfill_market_outcomes(today=date)
    try:
        investigated = investigate_misses()
    except Exception as e:
        logger.warning("[학습고리] 자율 조사 실패 (무시): %s", e)
        investigated = 0
    rows = load_scored_predictions()
    sc = market_scorecard(rows)
    if sc.get("n", 0) < MIN_SCORED_FOR_REFLECT:
        return f"학습고리 채점 {sc.get('n', 0)}건(신규 {filled}) — 표본 부족, 복기 생략"
    lessons = reflect(rows, sc)
    if not lessons:
        return f"학습고리 채점 {sc['n']}건 적중 {sc['hit_pct']}%(신규 {filled}) — 교훈 생성 0건(형식/근거 미달)"
    saved = save_lessons(lessons, date)
    sent = False
    if saved:
        try:
            from services.report_service import claim_report_slot, release_report_slot
            from clients.telegram_client import send_message
            if claim_report_slot(date, "weekly_reflection"):
                try:
                    send_message(format_weekly_message(sc, lessons))
                    sent = True
                except Exception:
                    release_report_slot(date, "weekly_reflection")
                    raise
        except Exception as e:
            logger.warning("[학습고리] 주간 복기 발송 실패 (교훈은 저장됨): %s", e)
    return f"학습고리 채점 {sc['n']}건 적중 {sc['hit_pct']}%(신규 {filled}) 교훈 {saved}건 저장 조사 {investigated}건 발송={sent}"
