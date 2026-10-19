"""집중도 규칙 — 한 종목/한 섹터가 계좌에서 차지하는 비중의 상한과 위반 시 분할 축소안 (전부 코드 계산).

2026-10-14 진단: 계좌의 84%가 SK하이닉스라 시장이 -10%일 때 하이닉스 하나가 전체 손실의 79%를 차지했다.
'비중이 과도하다'는 말만 반복되고 '그래서 몇 주를, 얼마씩, 며칠에 나눠 줄이면 되는가'가 없었다.
이 모듈이 상한(기본 종목 50%·섹터 60%, /상한 으로 변경)을 넘는 종목을 찾아 구체적인 분할 축소 계획을 만든다.

비중은 '투자금(현금 제외)' 기준이다. 매도대금을 현금으로 두면 분모가 줄어 더 많이 팔아야 상한에 닿으므로
(현금화 시 / 재투자 시) 두 경우의 수량을 모두 보여준다. 제안일 뿐 자동 매매하지 않는다.
하루 매도 한도(계좌 투자금의 10%)는 플레이북 '급락 당일 대규모 변경 금지' 규칙과 같은 기준이다.
"""
import logging
import math

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)

DEFAULT_STOCK_CAP = 50.0
DEFAULT_SECTOR_CAP = 60.0
DAILY_TRIM_PCT = 10.0
_KEY = "rule.cap."


def get_caps() -> dict:
    caps = {"stock": DEFAULT_STOCK_CAP, "sector": DEFAULT_SECTOR_CAP}
    try:
        with get_conn() as conn:
            for k in caps:
                row = conn.execute(text("SELECT value FROM system_settings WHERE key=:k"), {"k": _KEY + k}).fetchone()
                if row:
                    caps[k] = float(row[0])
    except Exception as e:
        logger.debug("[집중도] 상한 조회 실패(기본값 사용): %s", e)
    return caps


def set_cap(kind: str, pct: float) -> bool:
    if kind not in ("stock", "sector") or not (10 <= pct <= 100):
        return False
    try:
        with get_conn() as conn:
            conn.execute(text("DELETE FROM system_settings WHERE key=:k"), {"k": _KEY + kind})
            conn.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, :v)"),
                         {"k": _KEY + kind, "v": str(pct)})
        return True
    except Exception as e:
        logger.warning("[집중도] 상한 저장 실패: %s", e)
        return False


def _trim_plan(value: float, total: float, cap_pct: float, price: float, avg: float) -> dict:
    """value: 해당 종목(또는 섹터) 평가액, total: 투자금 합계. 상한까지 줄이는 데 필요한 매도량."""
    cap = cap_pct / 100
    if total <= 0 or price <= 0 or value / total <= cap:
        return {}
    to_cash = (value - cap * total) / (1 - cap)      # 대금을 현금으로 두면 분모도 줄어든다
    reinvest = value - cap * total                    # 다른 종목으로 옮기면 분모 유지
    per_day = total * DAILY_TRIM_PCT / 100
    return {
        "sell_cash_won": to_cash, "sell_cash_qty": math.ceil(to_cash / price),
        "sell_reinvest_won": reinvest, "sell_reinvest_qty": math.ceil(reinvest / price),
        "per_day_qty": max(1, int(per_day // price)), "per_day_won": per_day,
        "days_cash": math.ceil(to_cash / per_day) if per_day else 0,
        "days_reinvest": math.ceil(reinvest / per_day) if per_day else 0,
        "realized_per_share": price - avg,            # 음수면 매도 시 손실 확정
    }


def evaluate(positions: list[dict], caps: dict | None = None) -> list[dict]:
    """positions: portfolio_risk.load_positions 결과. 위반 목록(종목 → 섹터 순, 비중 큰 순)."""
    caps = caps or get_caps()
    total = sum(p["value"] for p in positions)
    if total <= 0:
        return []
    out = []
    for p in sorted(positions, key=lambda x: -x["value"]):
        w = p["value"] / total * 100
        if w > caps["stock"]:
            out.append({"kind": "stock", "code": p["code"], "name": p["name"], "weight": w, "cap": caps["stock"],
                        "qty": p["qty"], "price": p["price"], "plan": _trim_plan(p["value"], total, caps["stock"], p["price"], p["avg"])})
    by_sec: dict[str, list[dict]] = {}
    for p in positions:
        by_sec.setdefault(p["sector"], []).append(p)
    for sec, ps in by_sec.items():
        v = sum(x["value"] for x in ps)
        w = v / total * 100
        # 한 종목이 곧 섹터 전부인 경우는 종목 위반으로 이미 다뤘으므로 중복 경보를 만들지 않는다
        if w > caps["sector"] and len(ps) > 1:
            top = max(ps, key=lambda x: x["value"])
            out.append({"kind": "sector", "code": top["code"], "name": sec, "weight": w, "cap": caps["sector"],
                        "qty": top["qty"], "price": top["price"], "plan": _trim_plan(v, total, caps["sector"], top["price"], top["avg"])})
    return out


def _man(won: float) -> str:
    return f"{won/10000:,.0f}만원"


def format_alert(v: dict) -> str:
    """사람이 읽는 한 덩어리(여러 줄) 경보. 수치는 전부 코드 계산."""
    p = v["plan"]
    label = "종목" if v["kind"] == "stock" else "섹터"
    head = f"🔔 {v['name']} 한 {label}이 내 자산의 {v['weight']:.1f}% (한도 {v['cap']:.0f}%) — 한도 초과"
    if not p:
        return head
    lines = [head,
             f"   한도까지 줄이려면 약 {p['sell_cash_qty']:,}주({_man(p['sell_cash_won'])}) 매도 필요. "
             f"하루 약 {p['per_day_qty']:,}주씩 나눠 팔면 {p['days_reinvest']}~{p['days_cash']}거래일"]
    if v["kind"] == "stock" and p["realized_per_share"] < 0:
        lines.append(f"   ※ 평단보다 낮은 가격이라 팔면 주당 {abs(p['realized_per_share']):,.0f}원 손실이 확정됩니다")
    return "\n".join(lines)


def check_decisions(decisions: dict, violations: list[dict]) -> list[str]:
    """CEO가 위반 종목을 추가매수/신규편입 하자고 했는지. 경고 문자열 목록."""
    over = {v["code"]: v for v in violations if v["kind"] == "stock"}
    out = []
    for pos in (decisions or {}).get("new_positions", []) or []:
        if str(pos.get("code", "")).strip() in over:
            out.append(f"{pos.get('name')}({pos.get('code')}) 이미 상한 {over[pos['code']]['cap']:.0f}% 초과 종목인데 신규/추가 편입 제안 — 집중도 규칙 위반")
    for ch in (decisions or {}).get("position_changes", []) or []:
        if ch.get("action") == "add" and str(ch.get("code", "")).strip() in over:
            out.append(f"{ch.get('name')} 상한 초과 종목을 비중 확대하자는 제안 — 집중도 규칙 위반")
    return out


def warn_if_adding(code: str, positions: list[dict] | None = None) -> str:
    """사용자가 이미 상한을 넘은 종목을 더 사려 할 때(원장 등록/체결) 보여줄 한 줄. 해당 없으면 ''."""
    try:
        if positions is None:
            from services.portfolio_risk import load_positions
            positions = load_positions(None)
        for v in evaluate(positions):
            if v["kind"] == "stock" and v["code"] == code:
                return (f"🔔 집중도: {v['name']}은 이미 계좌의 {v['weight']:.0f}%로 상한 {v['cap']:.0f}%를 넘었습니다. "
                        f"더 사면 쏠림이 커집니다 — 정말 추가 매수가 맞는지 확인하세요.")
    except Exception as e:
        logger.debug("[집중도] 추가매수 점검 실패: %s", e)
    return ""
