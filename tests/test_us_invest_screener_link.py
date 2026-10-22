"""미국 주간 추천 ↔ 전 종목 스크리너 연동 — 스냅샷 후보는 성장주에 가점으로 합류하고, 스냅샷이 없거나 오래되거나
조회가 실패하면 기존 고정 목록 방식 그대로 동작해야 한다(전부-아니면-전무 방지)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agents import us_invest_agent as ag
from services import us_universe_screener as us

_KST = ZoneInfo("Asia/Seoul")


def _pick(code="ZZZ", score=4, days_ago=0):
    d = (datetime.now(_KST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {"date": d, "code": code, "name": f"{code} Inc", "sector": "Tech", "close": 50.0,
            "chg_pct": 1.0, "mcap_usd": 5e9, "score": score, "signals": "수익성,저평가,컨센서스여력"}


def _fake_data(ticker):
    return {"ticker": ticker, "price": 50.0, "change_1w": 1.0, "change_1m": 2.0, "vol_ratio": 1.0,
            "market_cap": 5e9, "pe_ratio": 20.0, "dividend_yield": None, "sector": "Tech"}


def test_screener_picks_excludes_fixed_list(monkeypatch):
    monkeypatch.setattr(us, "load_picks", lambda **kw: [_pick("NVDA"), _pick("ZZZ")])
    out = ag._screener_picks({"NVDA"})
    assert [p["code"] for p in out] == ["ZZZ"]


def test_screener_picks_ignores_stale_snapshot(monkeypatch):
    monkeypatch.setattr(us, "load_picks", lambda **kw: [_pick("ZZZ", days_ago=10)])
    assert ag._screener_picks(set()) == []


def test_screener_picks_failure_falls_back(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(us, "load_picks", boom)
    assert ag._screener_picks(set()) == []


def test_collect_candidates_merges_screener_pick_with_bonus(monkeypatch):
    monkeypatch.setattr(ag, "_fetch_stock_data", _fake_data)
    monkeypatch.setattr(us, "load_picks", lambda **kw: [_pick("ZZZ", score=5)])
    cands = ag.collect_candidates()
    zzz = [d for d in cands["growth"] if d["ticker"] == "ZZZ"]
    assert zzz and "전종목스크린 5묶음" in zzz[0]["screen_note"]
    base = ag._score_growth(_fake_data("ZZZ"))
    assert zzz[0]["score"] == round(base + ag._SCREENER_BONUS * 5, 1)
    assert "전종목스크린" in ag._format_candidates(cands)


def test_exclusion_gate_applies_to_screener_picks(monkeypatch):
    monkeypatch.setattr(ag, "_fetch_stock_data", _fake_data)
    monkeypatch.setattr(us, "load_picks", lambda **kw: [_pick("ZZZ")])
    skipped = []
    cands = ag.collect_candidates(lambda d: "최근 추천" if d["ticker"] == "ZZZ" else None, skipped)
    assert all(d["ticker"] != "ZZZ" for d in cands["growth"])
    assert any("최근 추천" in why for _, why in skipped)


def test_collect_candidates_without_snapshot_is_unchanged(monkeypatch):
    monkeypatch.setattr(ag, "_fetch_stock_data", _fake_data)
    monkeypatch.setattr(us, "load_picks", lambda **kw: [])
    cands = ag.collect_candidates()
    assert cands["growth"] and all("screen_note" not in d for d in cands["growth"])
