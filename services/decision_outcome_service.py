"""과거 추천/결정의 실제 결과를 KOSPI 대비 알파로 채점하고 CEO 프롬프트용 사실 목록으로 만든다.

TradingAgents의 "결정 → 실현수익 정산 → 반성 주입" 루프를 우리 구조에 맞게 옮긴 것.
원칙 2(계산은 코드가, LLM은 서술만)에 따라 수익률·알파는 전부 여기서 계산하고
LLM에는 계산이 끝난 숫자만 준다.

**자동 표본 게이트**: 종료(목표/손절/만료)된 추천이 `MIN_SETTLED_FOR_PROMPT`건 미만이면
프롬프트 주입은 빈 문자열이라 CEO 입력이 전혀 바뀌지 않는다 — CLAUDE.md의 재개 기준
(추천 8건+·추적 4주+)이 채워지기 전엔 완전 무동작, 데이터가 쌓이면 코드 수정 없이 켜진다.
스냅샷용 요약(`summarize_for_snapshot`)은 게이트 없이 항상 계산해 시계열을 쌓는다.
"""
import logging
from datetime import datetime, timedelta

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

MIN_SETTLED_FOR_PROMPT = 5
_TERMINAL = ("target_hit", "stop_hit", "expired")
_STATUS_KR = {"target_hit": "목표가 도달", "stop_hit": "손절가 도달", "expired": "기간 만료",
              "tracking": "추적 중"}


def _kospi_return(from_date: str, to_date: str) -> float | None:
    """같은 시작점·같은 기간의 KOSPI 수익률(%). 조회 실패 시 None (알파는 N/A 처리)."""
    try:
        import yfinance as yf
        end = (datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=3)).strftime("%Y-%m-%d")
        h = yf.Ticker("^KS11").history(start=from_date, end=end, interval="1d")
        if h.empty or len(h) < 2:
            return None
        start_px = float(h.iloc[0]["Close"])
        end_px = float(h.iloc[-1]["Close"])
        if start_px <= 0:
            return None
        return round((end_px - start_px) / start_px * 100, 2)
    except Exception as e:
        logger.debug("[결과채점] KOSPI 수익률 조회 실패 (%s~%s): %s", from_date, to_date, e)
        return None


def get_outcomes() -> list[dict]:
    """추천별 최신 추적 상태 + KOSPI 대비 알파. 최신 추천부터."""
    try:
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT rt.rec_id, rt.code, rt.name, rt.rec_date, rt.date, rt.return_pct,
                       rt.days_held, rt.status, sr.fill_price
                FROM recommendation_tracking rt
                JOIN stock_recommendations sr ON sr.id = rt.rec_id
                WHERE rt.date = (SELECT MAX(date) FROM recommendation_tracking x
                                 WHERE x.rec_id = rt.rec_id)
                ORDER BY rt.rec_date DESC
            """)).fetchall()
    except Exception as e:
        logger.warning("[결과채점] 조회 실패: %s", e)
        return []

    out, seen = [], set()
    for rec_id, code, name, rec_date, last_date, ret, days, status, fill in rows:
        if rec_id in seen or ret is None:
            continue
        seen.add(rec_id)
        kospi = _kospi_return(rec_date, last_date)
        out.append({
            "rec_id": rec_id, "code": code, "name": name, "rec_date": rec_date,
            "last_date": last_date, "return_pct": round(float(ret), 2),
            "days_held": days, "status": status or "tracking",
            "adopted": bool(fill and fill > 0),
            "kospi_return_pct": kospi,
            "alpha_pct": round(float(ret) - kospi, 2) if kospi is not None else None,
        })
    return out


def summarize_for_snapshot() -> str:
    """backtest_snapshot용 한 줄 요약 (게이트 없음)."""
    items = get_outcomes()
    settled = [i for i in items if i["status"] in _TERMINAL]
    alphas = [i["alpha_pct"] for i in settled if i["alpha_pct"] is not None]
    avg_alpha = f"{sum(alphas) / len(alphas):+.2f}%p" if alphas else "N/A"
    return f"결과채점 추적={len(items)} 종료={len(settled)} 종료평균알파={avg_alpha}"


def format_outcomes_for_prompt(max_items: int = 8) -> str:
    """CEO 프롬프트용 '지난 추천의 실제 결과'. 종료 표본이 부족하면 빈 문자열."""
    settled = [i for i in get_outcomes() if i["status"] in _TERMINAL]
    if len(settled) < MIN_SETTLED_FOR_PROMPT:
        return ""
    wins = sum(1 for i in settled if i["return_pct"] > 0)
    alphas = [i["alpha_pct"] for i in settled if i["alpha_pct"] is not None]
    lines = [
        "[지난 추천의 실제 결과 — 코드가 계산한 사실. 이 숫자만 인용하고 새 수치를 만들지 마라]",
        f"종료된 추천 {len(settled)}건 중 수익 {wins}건 (적중률 {wins / len(settled) * 100:.0f}%)"
        + (f", KOSPI 대비 평균 알파 {sum(alphas) / len(alphas):+.1f}%p" if alphas else ""),
    ]
    for i in settled[:max_items]:
        basis = "내 체결가 기준" if i["adopted"] else "가상 추적(내 매매 아님)"
        alpha = f"알파 {i['alpha_pct']:+.1f}%p" if i["alpha_pct"] is not None else "알파 N/A"
        lines.append(
            f"- {i['name']}({i['code']}) 추천 {i['rec_date']}: {i['return_pct']:+.1f}% · {alpha} · "
            f"{_STATUS_KR.get(i['status'], i['status'])} [{basis}]"
        )
    lines.append("→ 오판 패턴(손절 반복 섹터·확신도 등)이 보이면 이번 판단에서 근거를 더 엄격히 요구하라.")
    return "\n".join(lines)
