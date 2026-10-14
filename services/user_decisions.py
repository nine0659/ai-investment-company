"""사용자 결정 로그 — "AI 제안을 따랐나/무시했나"와 그 후 성과 비교.

AI 성과(decision_scoring)만으로는 '이 시스템이 나에게 돈이 되는가'를 알 수 없다. 내가 따랐을 때와
무시했을 때 각각 종목이 지수 대비 어떻게 됐는지를 같은 기준으로 비교한다. 표본이 작으면 코드가 경고를 붙인다.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
VALID = {"followed", "ignored", "partial"}
KOR = {"따름": "followed", "따랐": "followed", "무시": "ignored", "안따름": "ignored", "일부": "partial", "부분": "partial",
       "followed": "followed", "ignored": "ignored", "partial": "partial"}
MIN_N = 5


def record(code: str, followed: str, reason: str = "", name: str = "", advice: str = "",
           price_at: float | None = None) -> bool:
    followed = KOR.get(followed, followed)
    if followed not in VALID:
        return False
    if price_at is None:
        try:
            from clients import naver_client as nv
            q = nv.stock_quote(code)
            price_at = q.get("price") if q else None
        except Exception:
            price_at = None
    try:
        with get_conn() as conn:
            conn.execute(text(
                "INSERT INTO user_decisions (date, code, name, followed, advice, reason, price_at) "
                "VALUES (:d,:c,:n,:f,:a,:r,:p)"),
                {"d": datetime.now(_KST).strftime("%Y-%m-%d"), "c": code, "n": name, "f": followed,
                 "a": advice, "r": reason, "p": price_at})
        return True
    except Exception as e:
        logger.warning("[결정로그] 저장 실패: %s", e)
        return False


def load() -> list[dict]:
    try:
        with get_conn() as conn:
            rows = conn.execute(text(
                "SELECT date, code, name, followed, reason, price_at FROM user_decisions ORDER BY date")).fetchall()
        return [dict(zip(("date", "code", "name", "followed", "reason", "price_at"), r)) for r in rows]
    except Exception:
        return []


def summarize(decisions: list[dict], now_prices: dict[str, float]) -> str:
    """결정 시점 가격 대비 현재 수익률(%)을 따름/무시로 나눠 평균. 지수 대비는 가격 이력이 있을 때만 별도 계산."""
    groups: dict[str, list[float]] = {"followed": [], "ignored": [], "partial": []}
    for d in decisions:
        p0, p1 = d.get("price_at"), now_prices.get(d["code"])
        if p0 and p1:
            groups[d["followed"]].append((p1 / p0 - 1) * 100)
    if not any(groups.values()):
        return ""
    lines = ["[내 결정 vs AI 제안 — 결정 시점 가격 대비 현재 수익률(%), 코드 계산]"]
    for k, label in (("followed", "AI 제안을 따른 종목"), ("ignored", "AI 제안을 무시한 종목"), ("partial", "일부만 따른 종목")):
        v = groups[k]
        if v:
            lines.append(f"  {label}: {len(v)}건, 평균 {sum(v)/len(v):+.1f}%")
    n = sum(len(v) for v in groups.values())
    if n < MIN_N:
        lines.append(f"  ※ 표본 {n}건뿐 — 우열을 단정하지 말 것")
    return "\n".join(lines)


def build_context() -> str:
    ds = load()
    if not ds:
        return ""
    prices = {}
    try:
        from clients import naver_client as nv
        for c in {d["code"] for d in ds}:
            q = nv.stock_quote(c)
            if q:
                prices[c] = q["price"]
    except Exception:
        pass
    return summarize(ds, prices)
