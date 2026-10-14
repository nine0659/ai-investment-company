"""KRX 투자자별(외국인·기관·개인) 순매수 금액(억원) — 수집·보관·요약.

지금까지 수급은 '종목 순위'뿐이라 AI가 "외국인 매도세"를 근거 없이 서술했다.
네이버 집계 금액을 일별로 DB(system_settings)에 쌓아 N일 누적으로 보여준다.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_PREFIX = "flow."


def collect() -> dict:
    """KOSPI·KOSDAQ 최신 투자자 동향을 가져와 저장. 반환: {시장: {...}}"""
    from clients import naver_client as nv
    out = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        d = nv.investor_trend(mkt)
        if d and d.get("date"):
            now = datetime.now(_KST)
            partial = d["date"] == now.strftime("%Y-%m-%d") and now.hour * 60 + now.minute < 15 * 60 + 40
            d["partial"] = partial
            out[mkt] = d
            if not partial:   # 확정치만 보관
                _save(mkt, d)
    return out


def _save(mkt: str, d: dict) -> None:
    key = f"{_PREFIX}{mkt}.{d['date']}"
    try:
        with get_conn() as conn:
            conn.execute(text("DELETE FROM system_settings WHERE key=:k"), {"k": key})
            conn.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, :v)"),
                         {"k": key, "v": json.dumps(d)})
    except Exception as e:
        logger.debug("[수급] 저장 실패: %s", e)


def history(mkt: str, n: int = 5) -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT value FROM system_settings WHERE key LIKE :p ORDER BY key DESC LIMIT :n"),
                {"p": f"{_PREFIX}{mkt}.%", "n": n}).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception:
        return []


def summarize(flows: dict, n: int = 5) -> str:
    """CEO 컨텍스트용. 금액은 억원, 부호 명시. 데이터 없으면 빈 문자열."""
    if not flows:
        return ""
    lines = ["[투자자별 순매수 금액 — 네이버 집계, 억원, +순매수/-순매도. 이 값만 인용]"]
    for mkt, d in flows.items():
        tag = "(장중 누적, 미확정)" if d.get("partial") else ""
        lines.append(f"  {mkt} {d['date']}{tag}: 외국인 {d['foreign']:+,.0f} · 기관 {d['institution']:+,.0f} · 개인 {d['personal']:+,.0f}")
        hist = [h for h in history(mkt, n) if h.get("date") != d.get("date") or not d.get("partial")]
        if len(hist) >= 2:
            f_sum = sum(h["foreign"] for h in hist)
            i_sum = sum(h["institution"] for h in hist)
            lines.append(f"    최근 {len(hist)}거래일 누적: 외국인 {f_sum:+,.0f} · 기관 {i_sum:+,.0f}")
    return "\n".join(lines)
