"""후보 → 내 계좌 행동안: 비중 밴드·보류 사유·분할·손절선은 전부 코드 계산."""
from services import candidate_action as ca


def _c(signals="저평가✔,공시호재✔", close=10000.0, **kw):
    return {"code": "111111", "name": "테스트", "close": close, "signals": signals, "industry": "5", **kw}


def _ctx(**kw):
    base = {"total": 10_000_000.0, "held_inds": set(), "views": {}, "fin": set(), "funding": None}
    base.update(kw)
    return base


def test_two_verified_groups_get_3pct_split_with_stop():
    d = ca.decide(_c("업종대비저평가✔,공시호재✔"), _ctx())
    assert d["pct"] == 3.0 and d["qty"] == 30 and d["first"] == 15 and d["stop"] == 8500
    assert "틀렸다고 볼 선: 8,500원" in ca.format_line(d, _ctx())


def test_one_verified_group_gets_2pct():
    assert ca.decide(_c("공시호재✔,수익성(참고)"), _ctx())["pct"] == 2.0


def test_no_verified_signal_is_watch_only():
    d = ca.decide(_c("쌍끌이매수"), _ctx())
    assert d["pct"] == 0 and "검증을 통과한 근거가 없음" in ca.format_line(d, _ctx())


def test_hold_reasons():
    assert "이미 보유한 업종" in " ".join(ca.decide(_c(), _ctx(held_inds={"5"}))["hold_reasons"])
    assert "비우호" in " ".join(ca.decide(_c(), _ctx(views={"5": "비우호"}))["hold_reasons"]) or \
        "못할" in " ".join(ca.decide(_c(), _ctx(views={"5": "비우호"}))["hold_reasons"])
    assert "악재" in " ".join(ca.decide(_c(bad_event="유상증자"), _ctx())["hold_reasons"])
    assert "금융업" in " ".join(ca.decide(_c(), _ctx(fin={"111111"}))["hold_reasons"])


def test_too_expensive_to_split_becomes_watch():
    d = ca.decide(_c(close=5_000_000.0), _ctx())
    assert d["pct"] == 0 and "분할이 어려움" in ca.format_line(d, _ctx())


def test_no_account_gives_no_lines():
    assert ca.build_lines(set(), positions=[]) == []


def test_ceo_context_includes_action_block(monkeypatch):
    from services import market_ideas as mi
    monkeypatch.setattr(mi, "kr_picks", lambda ex, n=5: (None, [], set()))
    monkeypatch.setattr(mi, "us_picks", lambda ex, n=5: (None, [], set()))
    monkeypatch.setattr(ca, "build_lines", lambda ex, positions=None, n=3: ["· 테스트(111111): 지금은 관찰만 — x"])
    blk = mi.build_block(set())
    assert "내 계좌 기준 행동안" in blk and "테스트(111111)" in blk and "바꿔 쓰지 마라" in blk


def test_crash_gate_uses_three_tranches_and_warns():
    ctx = _ctx(gate={"mode": "crash", "max_picks": 2, "reason": "급락 직후", "note": "1/3씩 3회"})
    d = ca.decide(_c("업종대비저평가✔,공시호재✔"), ctx)
    assert d["tranches"] == 3 and d["first"] == 10
    assert "⚠시장 상태" in ca.format_line(d, ctx)


def test_gate_caps_simultaneous_entries(monkeypatch):
    from services import market_ideas as mi
    picks = [{**_c("업종대비저평가✔,공시호재✔"), "code": f"11111{i}", "name": f"T{i}"} for i in range(3)]
    monkeypatch.setattr(mi, "kr_picks", lambda ex, n=3: (None, picks, set()))
    monkeypatch.setattr(mi, "_query", lambda *a, **k: [])
    monkeypatch.setattr("services.disclosure_events.by_code", lambda days=7: {})
    monkeypatch.setattr(ca, "_ctx", lambda pos: _ctx(gate={"mode": "heated", "max_picks": 1, "reason": "단기 과열", "note": "추격 금지"}))
    pos = [{"code": "999999", "value": 1e7, "name": "x", "qty": 1, "price": 1e7, "avg": 1e7, "sector": "a"}]
    lines = ca.build_lines(set(), positions=pos)
    assert sum("소액 분할 후보" in l for l in lines) == 1 and sum("동시 진입은 1종목까지" in l for l in lines) == 2
