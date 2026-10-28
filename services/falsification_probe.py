"""반증 조건이 발동했을 때 '왜 지금 발동했나'를 독립 모델이 조사해 기록한다 (2026-10-28).

흐름: alert_service가 반증 경보를 보낸 직후 호출 → 발동 조건·현재 시장·당일 수집 정보만 보고 '가설:' 1~2문장 작성
→ 입력에 없는 숫자를 쓰면 폐기 → system_settings(`falsify.probe`)에 최근 10건 보관 →
falsification_monitor.build_status_context가 다음 브리핑의 CEO 입력에 "AI 조사(가설)"로 덧붙인다.

새 발송 경로가 아니다(텔레그램 미발송). 조사 결과는 가설이며 판단 근거가 아니라 '확인할 방향'이다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)

_KEY = "falsify.probe"
_KEEP = 10
_FRESH_DAYS = 7
_KST = ZoneInfo("Asia/Seoul")

_SYSTEM = """당신은 투자 근거 훼손 조사관입니다. 보유 종목의 '반증 조건'(투자 당시 스스로 정해 둔 이럴 땐 틀렸다는 선)이 발동했습니다.
입력의 발동 조건·시장 조건·수집된 정보만 보고, 왜 지금 발동했는지 가설을 1~2문장으로 쓰세요.
- 입력에 있는 수치·뉴스만 근거로 삼으세요. 없는 사건이나 숫자를 만들지 마세요.
- 반드시 '가설:'로 시작하고, 종목 고유 문제인지 시장 전체 영향인지 구분하세요. 근거가 부족하면 '근거 부족'이라고 쓰세요.
- 매도·매수 권고는 하지 마세요. 확인할 방향만 쓰세요."""


def build_facts(holding: dict, fired: list[str], snap: dict | None, intel: list[str]) -> str:
    """조사용 사실 묶음 (순수 함수)."""
    s = snap or {}
    market = (f"KOSPI {s.get('kospi_chg')}% · USD/KRW {s.get('usd_krw')} · VIX {s.get('vix')} · "
              f"미10년물 {s.get('us10y')} · S&P {s.get('sp500_chg')}% · 나스닥 {s.get('nasdaq_chg')}%") if s else "데이터 없음"
    lines = [
        f"[종목] {holding.get('name')}({holding.get('code')}) 평단 {holding.get('avg_price')} 현재가 {holding.get('price')}",
        "[발동된 반증 조건] " + "; ".join(fired),
        f"[시장 조건] {market}",
    ]
    if intel:
        lines.append("[오늘 수집된 정보 요약] " + " / ".join(i[:140] for i in intel[:3]))
    return "\n".join(lines)


def validate(why: str, facts: str) -> str | None:
    """'가설:'로 시작하고 입력에 없는 숫자가 없을 때만 통과."""
    why = (why or "").strip()
    if not why.startswith("가설"):
        return None
    from services.output_fact_check import check_report_numbers
    if check_report_numbers(why, facts)["count"] > 0:
        return None
    return why[:300]


def _load() -> list[dict]:
    try:
        with get_conn() as conn:
            row = conn.execute(text("SELECT value FROM system_settings WHERE key=:k"), {"k": _KEY}).fetchone()
        return json.loads(row[0]) if row and row[0] else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    with get_conn() as conn:
        conn.execute(text("DELETE FROM system_settings WHERE key=:k"), {"k": _KEY})
        conn.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, :v)"),
                     {"k": _KEY, "v": json.dumps(items[-_KEEP:], ensure_ascii=False)})


def recent_for(code: str, days: int = _FRESH_DAYS) -> str:
    """최근 days일 안의 해당 종목 조사 결과(가장 최근 1건). 없으면 ''."""
    since = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    hits = [p for p in _load() if p.get("code") == code and p.get("date", "") >= since]
    return hits[-1]["why"] if hits else ""


def run_probe(holding: dict, fired: list[str], chat_fn=None) -> str | None:
    """발동 1건을 조사해 기록. 통과한 가설 문장을 반환(실패·폐기는 None). 예외를 밖으로 던지지 않는다."""
    try:
        if chat_fn is None:
            from clients.openai_client import chat_independent as chat_fn   # 자기 판단과 다른 계열 모델
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        with get_conn() as conn:
            r = conn.execute(text(
                "SELECT kospi_chg, usd_krw, vix, us10y, sp500_chg, nasdaq_chg FROM market_snapshots "
                "WHERE run_type='pre_market' AND date<=:d ORDER BY date DESC LIMIT 1"), {"d": today}).fetchone()
            snap = dict(zip(("kospi_chg", "usd_krw", "vix", "us10y", "sp500_chg", "nasdaq_chg"), r)) if r else None
            intel = [x[0] for x in conn.execute(text(
                "SELECT summary FROM intelligence_archive WHERE date=:d AND summary IS NOT NULL LIMIT 3"),
                {"d": today}).fetchall()]
        facts = build_facts(holding, fired, snap, intel)
        why = validate(chat_fn(_SYSTEM, facts, max_tokens=220), facts)
        if not why:
            logger.info("[반증조사] %s — 가설 폐기(형식 불량 또는 입력에 없는 숫자)", holding.get("code"))
            return None
        items = [p for p in _load() if p.get("code") != holding.get("code")]
        items.append({"code": holding.get("code"), "name": holding.get("name"), "date": today, "why": why})
        _save(items)
        logger.info("[반증조사] %s 기록: %s", holding.get("code"), why[:60])
        return why
    except Exception as e:
        logger.warning("[반증조사] 실패 (무시): %s", e)
        return None
