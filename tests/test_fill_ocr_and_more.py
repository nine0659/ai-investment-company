"""체결 캡처 인식(비전 출력 검증·합산·중복) + 브리핑 【더보기】 분리 회귀 테스트 (네트워크 없음)."""
import json

import pytest

from services import briefing_facts, fill_ocr

H = [
    {"code": "000660", "name": "SK하이닉스", "quantity": 19, "avg_price": 1935965.0},
    {"code": "009150", "name": "삼성전기", "quantity": 2, "avg_price": 1795000.0},
]


def _vision(payload):
    return lambda s, u, b, m: payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def test_merge_partial_fills_weighted_avg():
    raw = [{"name": "SK하이닉스", "side": "매도", "qty": "3", "price": "1,465,000"},
           {"name": "SK하이닉스", "side": "sell", "qty": 2, "price": 1468000}]
    out = fill_ocr.normalize(raw, H)
    assert len(out) == 1 and out[0]["qty"] == 5
    assert out[0]["price"] == pytest.approx((3 * 1465000 + 2 * 1468000) / 5)
    assert any("합산" in w for w in out[0]["warnings"])


def test_unreadable_cells_are_rejected_not_guessed():
    out = fill_ocr.normalize([{"name": "SK하이닉스", "side": "buy", "qty": None, "price": 1000}], H)
    assert out[0]["ok"] is False
    out = fill_ocr.normalize([{"name": "삼성전기", "side": "buy", "qty": 1.5, "price": 1000}], H)
    assert out[0]["ok"] is False and "정수" in out[0]["reason"]


def test_unknown_stock_needs_code():
    out = fill_ocr.normalize([{"name": "처음보는회사", "side": "buy", "qty": 1, "price": 1000}], H)
    assert out[0]["ok"] is False
    out = fill_ocr.normalize([{"name": "처음보는회사", "code": "123456", "side": "buy", "qty": 1, "price": 1000}], H)
    assert out[0]["ok"] and out[0]["code"] == "123456" and out[0]["warnings"]


def test_price_far_from_market_warns():
    out = fill_ocr.normalize([{"name": "SK하이닉스", "side": "sell", "qty": 1, "price": 14650}], H,
                             quote=lambda c: {"price": 1465000})
    assert any("크게 다름" in w for w in out[0]["warnings"])


def test_extract_handles_fenced_json_and_unreadable():
    payload = '```json\n{"fills": [{"name": "삼성전기", "side": "buy", "qty": 1, "price": 1405000}], "readable": true}\n```'
    r = fill_ocr.extract(b"x", "image/png", H, vision=_vision(payload), quote=lambda c: None)
    assert r["ok"] and r["fills"][0]["code"] == "009150"
    r = fill_ocr.extract(b"x", "image/png", H, vision=_vision({"fills": [], "readable": False}), quote=lambda c: None)
    assert not r["ok"] and "읽지 못" in r["error"]
    r = fill_ocr.extract(b"x", "image/png", H, vision=_vision("그냥 문장"), quote=lambda c: None)
    assert not r["ok"]


def test_extract_vision_failure_is_graceful():
    def boom(*a):
        raise RuntimeError("api down")
    r = fill_ocr.extract(b"x", "image/png", H, vision=boom, quote=lambda c: None)
    assert not r["ok"] and "오류" in r["error"]
    assert not fill_ocr.extract(b"", "image/png", H)["ok"]
    assert not fill_ocr.extract(b"x" * (9 * 1024 * 1024), "image/png", H)["ok"]


def test_fill_hash_stable_and_distinct():
    a = {"code": "000660", "side": "sell", "qty": 5, "price": 1466200.0, "date": "2026-10-14"}
    assert fill_ocr.fill_hash(a) == fill_ocr.fill_hash(dict(a))
    assert fill_ocr.fill_hash(a) != fill_ocr.fill_hash({**a, "qty": 6})


def test_split_more():
    rep = "결론\n🧭 핵심 판단\n- 내 의견: x\n【더보기】\n- 엇갈린 시각: 가 / 나\n"
    main, more = briefing_facts.split_more(rep)
    assert "엇갈린" not in main and main.rstrip().endswith("- 내 의견: x")
    assert more.startswith("- 엇갈린 시각")
    assert briefing_facts.split_more("마커 없음") == ("마커 없음", "")


def test_more_roundtrip_in_test_db():
    assert briefing_facts.save_more("2026-10-14", "pre_market", "근거 상세")
    m = briefing_facts.load_more()
    assert m["more"] == "근거 상세" and m["run_type"] == "pre_market"


def test_mirae_style_side_labels():
    assert fill_ocr._side("현금매수") == "buy"
    assert fill_ocr._side("신용매도") == "sell"
    assert fill_ocr._side("매수취소") is None and fill_ocr._side("정정매도") is None
    assert fill_ocr._side("미체결매수") is None
