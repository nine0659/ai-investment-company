"""증권사 체결내역 캡처(이미지) → 체결 목록 (비전 모델 + 코드 검증).

원칙(2026-09-21 사고 교훈 + 절대원칙 2·3): 모델은 '화면에 보이는 글자를 옮겨 적기'만 하고, 종목 매칭·수치 검증·
합산·중복 확인은 전부 코드가 한다. 불확실한 칸은 null로 받아 추측으로 채우지 않으며, 읽은 결과는 항상
사람의 확인(/ledger ok) 뒤에만 원장에 반영된다. 이미지는 저장하지 않고 로그에도 남기지 않는다.
"""
import base64
import hashlib
import json
import logging
import re

logger = logging.getLogger(__name__)

MAX_BYTES = 8 * 1024 * 1024
PRICE_DEVIATION_WARN = 0.40        # 현재가와 40% 넘게 다르면 확인 요청

SYSTEM = """너는 증권사 앱 캡처의 글자를 옮겨 적는 OCR 도우미다. 해석·추측·계산을 하지 않는다.
화면에 실제로 보이는 '체결된' 주문만 추출한다(미체결·주문접수·취소는 제외).
읽을 수 없거나 화면에 없는 칸은 반드시 null. 숫자의 쉼표·단위는 제거한 순수 숫자로.
출력은 JSON 하나만:
{"fills": [{"name": 종목명, "code": 6자리코드 또는 null, "side": "buy" 또는 "sell",
            "qty": 체결수량(주), "price": 체결단가(원), "date": "YYYY-MM-DD" 또는 null}],
 "readable": true 또는 false}
체결내역이 아닌 이미지이거나 글자가 흐려 읽을 수 없으면 {"fills": [], "readable": false}."""
USER = "이 캡처에서 체결된 주문을 모두 JSON으로 옮겨 적어줘."


def _json_from(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        return {}


def _side(v) -> str | None:
    s = str(v or "").strip().lower()
    if s in ("buy", "매수", "사기", "b"):
        return "buy"
    if s in ("sell", "매도", "팔기", "s"):
        return "sell"
    return None


def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def match_holding(name: str | None, code: str | None, holdings: list[dict]) -> dict | None:
    for h in holdings:
        if code and h["code"] == str(code).strip().upper():
            return h
    n = (name or "").replace(" ", "")
    if not n:
        return None
    for h in holdings:
        hn = (h["name"] or "").replace(" ", "")
        if hn == n:
            return h
    for h in holdings:
        hn = (h["name"] or "").replace(" ", "")
        if hn and (hn in n or n in hn) and min(len(hn), len(n)) >= 3:
            return h
    return None


def fill_hash(f: dict) -> str:
    key = f"{f['code']}|{f['side']}|{int(f['qty'])}|{f['price']:.0f}|{f.get('date') or ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def normalize(raw_fills: list[dict], holdings: list[dict], quote=None) -> list[dict]:
    """모델 출력 → 검증·합산된 체결 목록. 각 항목: {side, code, name, qty, price, date, warnings, ok}.
    quote(code)->{'price':…} 는 현재가 대조용(없으면 생략)."""
    items: list[dict] = []
    for r in raw_fills or []:
        side, qty, price = _side(r.get("side")), _num(r.get("qty")), _num(r.get("price"))
        warns: list[str] = []
        h = match_holding(r.get("name"), r.get("code"), holdings)
        code = h["code"] if h else (str(r.get("code")).strip().upper() if r.get("code") else None)
        name = h["name"] if h else (r.get("name") or code)
        if not (side and qty and price):
            items.append({"ok": False, "name": name, "reason": "매수/매도·수량·단가 중 읽지 못한 칸이 있음"})
            continue
        if qty != int(qty):
            items.append({"ok": False, "name": name, "reason": f"수량이 정수가 아님({qty})"})
            continue
        if not code:
            items.append({"ok": False, "name": name, "reason": "보유 종목과 매칭되지 않고 종목코드도 없음"})
            continue
        if not h:
            warns.append("현재 원장에 없는 종목(신규 등록이 됩니다)" if side == "buy" else "원장에 없는 종목 매도")
        items.append({"ok": True, "side": side, "code": code, "name": name, "qty": int(qty), "price": price,
                      "date": r.get("date"), "warnings": warns})
    # (코드, 방향) 별 합산: 수량 합, 단가는 가중평균 — 부분체결 여러 줄을 한 건으로
    merged: dict[tuple, dict] = {}
    bad = [i for i in items if not i["ok"]]
    for it in (i for i in items if i["ok"]):
        k = (it["code"], it["side"])
        if k in merged:
            m = merged[k]
            tot = m["qty"] + it["qty"]
            m["price"] = (m["price"] * m["qty"] + it["price"] * it["qty"]) / tot
            m["qty"] = tot
            m["warnings"] = list(dict.fromkeys(m["warnings"] + it["warnings"] + ["같은 종목 여러 줄을 합산함"]))
        else:
            merged[k] = dict(it)
    out = list(merged.values())
    for m in out:
        if quote:
            try:
                q = quote(m["code"])
                if q and q.get("price") and abs(m["price"] / q["price"] - 1) > PRICE_DEVIATION_WARN:
                    m["warnings"].append(f"체결가 {m['price']:,.0f}원이 현재가 {q['price']:,.0f}원과 크게 다름 — 숫자를 다시 확인")
            except Exception:
                pass
    return out + bad


def extract(image_bytes: bytes, mime: str, holdings: list[dict], vision=None, quote=None) -> dict:
    """반환: {ok, fills, error}. vision(system,user,b64,mime)->str 주입 가능(테스트)."""
    if not image_bytes:
        return {"ok": False, "fills": [], "error": "이미지를 받지 못했습니다"}
    if len(image_bytes) > MAX_BYTES:
        return {"ok": False, "fills": [], "error": "이미지가 너무 큽니다(8MB 초과)"}
    if vision is None:
        from clients.openai_client import chat_vision
        vision = lambda s, u, b, m: chat_vision(s, u, b, m)
    if quote is None:
        from clients import naver_client as nv
        quote = nv.stock_quote
    try:
        txt = vision(SYSTEM, USER, base64.b64encode(image_bytes).decode(), mime or "image/jpeg")
    except Exception as e:
        logger.warning("[캡처인식] 비전 호출 실패: %s", type(e).__name__)
        return {"ok": False, "fills": [], "error": "이미지 인식 서비스 오류"}
    data = _json_from(txt)
    if not data or data.get("readable") is False or not data.get("fills"):
        return {"ok": False, "fills": [], "error": "체결 내역을 읽지 못했습니다(흐리거나 체결내역 화면이 아님)"}
    return {"ok": True, "fills": normalize(data["fills"], holdings, quote), "error": ""}
