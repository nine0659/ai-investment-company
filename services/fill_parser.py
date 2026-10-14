"""체결 알림 문자 → 보유 원장 갱신안 (미래에셋은 API가 없어 원장을 사람이 맞춰야 한다).

사용자가 텔레그램에 증권사 체결 문자를 붙여넣으면 종목·수량·가격·매수/매도를 읽어
'갱신안'을 보여주고, 승인하면 원장에 반영한다. 자동 반영 금지(2026-09-21 사고 교훈):
항상 사람의 확인 뒤에만 쓴다. 이미지(캡처) 인식은 아직 없다.
"""
import re

_SIDE = re.compile(r"(매수|매도|buy|sell)", re.I)
_QTY = re.compile(r"([\d,]+)\s*주")
_PRICE = re.compile(r"([\d,]{3,})\s*원")
_CODE = re.compile(r"\b(\d{5}[0-9A-Z])\b")


def parse(text: str, holdings: list[dict] | None = None) -> dict:
    """반환: {ok, side, code, name, qty, price} 또는 {ok: False, reason}.
    종목은 6자리 코드가 있으면 코드, 없으면 보유 종목명 포함 여부로 찾는다."""
    t = text.replace("\n", " ")
    side_m = _SIDE.search(t)
    qty_m = _QTY.search(t)
    price_m = _PRICE.search(t)
    if not (side_m and qty_m and price_m):
        return {"ok": False, "reason": "매수/매도, 수량(주), 가격(원)을 모두 찾지 못했습니다"}
    side = "buy" if side_m.group(1) in ("매수", "buy", "BUY", "Buy") else "sell"
    qty = int(qty_m.group(1).replace(",", ""))
    price = float(price_m.group(1).replace(",", ""))
    code, name = None, None
    cm = _CODE.search(t)
    for h in holdings or []:
        if (cm and h["code"] == cm.group(1)) or (h["name"] and h["name"] in t):
            code, name = h["code"], h["name"]
            break
    if not code and cm:
        code, name = cm.group(1), cm.group(1)
    if not code:
        return {"ok": False, "reason": "종목을 못 찾았습니다 — 문자에 6자리 종목코드를 넣거나 /holdings add 를 쓰세요"}
    return {"ok": True, "side": side, "code": code, "name": name, "qty": qty, "price": price}


def plan(fill: dict, holdings: list[dict]) -> dict:
    """체결 1건을 반영한 뒤의 (수량, 평단). 매수는 가중평균, 매도는 수량만 줄고 평단 유지."""
    cur = next((h for h in holdings if h["code"] == fill["code"]), None)
    old_q = cur["quantity"] if cur else 0
    old_avg = cur["avg_price"] if cur else 0.0
    if fill["side"] == "buy":
        new_q = old_q + fill["qty"]
        new_avg = (old_q * old_avg + fill["qty"] * fill["price"]) / new_q if new_q else 0.0
    else:
        if fill["qty"] > old_q:
            return {"ok": False, "reason": f"보유 {old_q}주보다 많이 매도({fill['qty']}주)로 읽혔습니다 — 문자를 확인하세요"}
        new_q, new_avg = old_q - fill["qty"], old_avg
    return {"ok": True, "old_q": old_q, "old_avg": old_avg, "new_q": new_q, "new_avg": new_avg,
            "close": new_q == 0}
