"""2026-10-14 자문 기능 18종의 핵심 계산 로직 회귀 테스트 (네트워크·DB 없이)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from services import (behavior_check, briefing_facts, briefing_mode, calibration, data_freshness,
                      event_calendar, fair_value, fill_parser, playbook, portfolio_risk,
                      scenario_engine, user_decisions, account_perf)

POS = [
    {"code": "000660", "name": "SK하이닉스", "qty": 19, "avg": 1935965.0, "price": 1456000.0,
     "value": 19 * 1456000.0, "sector": "반도체"},
    {"code": "005380", "name": "현대차", "qty": 5, "avg": 446333.0, "price": 311500.0,
     "value": 5 * 311500.0, "sector": "자동차"},
]


# ── B5 포트폴리오 위험 ───────────────────────────────────────────
def test_weights_and_concentration():
    w = portfolio_risk.weights(POS)
    assert abs(sum(w["by_stock"].values()) - 1) < 1e-9
    assert w["by_sector"]["반도체"] > 0.9
    assert w["top1"] == w["by_stock"]["000660"]


def test_stress_sector_loss_matches_weight():
    w = portfolio_risk.weights(POS)
    st = portfolio_risk.stress(POS, {})
    sec = next(s for s in st if s["name"].startswith("반도체"))
    assert sec["acct_pct"] == pytest.approx(-10 * w["by_sector"]["반도체"])
    mk = next(s for s in st if s["name"] == "KOSPI -10%")
    assert mk["acct_pct"] == pytest.approx(-10)          # 베타 1.0 가정
    assert "1.0 가정" in mk["note"]


def test_risk_metrics_without_history_is_explicit():
    m = portfolio_risk.risk_metrics(POS, {})
    assert m["vol_ann"] is None and set(m["missing"]) == {"SK하이닉스", "현대차"}


def test_risk_metrics_with_history():
    import random
    random.seed(1)
    mk = [random.gauss(0, 0.01) for _ in range(120)]
    rets = {"KOSPI": mk, "000660": [1.5 * x for x in mk], "005380": [0.5 * x for x in mk]}
    m = portfolio_risk.risk_metrics(POS, rets)
    assert m["vol_ann"] > 0 and m["var95"] > 0
    assert m["betas"]["000660"] == pytest.approx(1.5, rel=0.05)


# ── B7 시나리오 ──────────────────────────────────────────────────
def test_what_if_sell_signs():
    r = scenario_engine.what_if_sell(POS[0], 20, sigma=0.1, account_total=1e7)
    assert r["ok"] and r["sell_qty"] == 3
    down = next(x for x in r["rows"] if x["k"] == -1)["net_vs_hold"]
    up = next(x for x in r["rows"] if x["k"] == 1)["net_vs_hold"]
    assert down > 0 > up                       # 하락 시 판 게 이득, 상승 시 후회
    assert "가정" in r["tax_note"]


def test_what_if_zero_qty():
    r = scenario_engine.what_if_sell({"name": "x", "qty": 3, "avg": 1, "price": 1}, 10)
    assert not r["ok"]


# ── B8 체결 문자 ─────────────────────────────────────────────────
H = [{"code": "000660", "name": "SK하이닉스", "quantity": 19, "avg_price": 1935965.0}]


def test_fill_parse_sell_and_plan():
    f = fill_parser.parse("[미래에셋] SK하이닉스 5주 매도 체결 1,456,000원", H)
    assert f["ok"] and f["side"] == "sell" and f["qty"] == 5 and f["price"] == 1456000
    p = fill_parser.plan(f, H)
    assert p["new_q"] == 14 and p["new_avg"] == 1935965.0 and not p["close"]


def test_fill_buy_weighted_avg():
    f = fill_parser.parse("SK하이닉스 1주 매수 체결 1,000,000원", H)
    p = fill_parser.plan(f, H)
    assert p["new_q"] == 20
    assert p["new_avg"] == pytest.approx((19 * 1935965 + 1000000) / 20)


def test_fill_rejects_oversell_and_unknown():
    f = fill_parser.parse("SK하이닉스 50주 매도 체결 1,000,000원", H)
    assert not fill_parser.plan(f, H)["ok"]
    assert not fill_parser.parse("모르는종목 5주 매수 체결 100,000원", H)["ok"]
    assert not fill_parser.parse("오늘 시장 어때", H)["ok"]


# ── B6 계좌 성과 ─────────────────────────────────────────────────
def test_account_perf_vs_index():
    closes = {"2026-09-01": 100.0, "2026-10-13": 90.0}
    c = account_perf.compute(POS, closes, {"000660": "2026-09-01", "005380": ""})
    h = next(r for r in c["rows"] if r["name"] == "SK하이닉스")
    assert h["idx"] == pytest.approx(-10.0)
    assert h["vs_idx_won"] < 0                       # -24.8% vs 지수 -10% → 지수보다 못함
    assert not c["complete"]                         # 현대차는 지수 비교 불가 표시


# ── C9 적정가치 ──────────────────────────────────────────────────
def test_fair_value_needs_two_evidence():
    assert fair_value.classify(100, None, None, None, None)["zone"] == "근거 약함"
    assert fair_value.classify(100, 90, 150, 200, 10)["zone"] == "싼 편"
    assert fair_value.classify(300, 90, 150, 200, 95)["zone"] == "비싼 편"
    assert fair_value.classify(100, 90, 150, 200, 90)["zone"] == "엇갈림"


# ── C10 플레이북 ─────────────────────────────────────────────────
def test_playbook_regimes():
    assert playbook.classify(-5.46) == "shock_down"
    assert playbook.classify(-1.5) == "down"
    assert playbook.classify(0.4) == "calm"
    assert playbook.classify(3.2) == "shock_up"
    assert playbook.classify(None) == "calm"
    assert "공황매도" in playbook.context_block(-5.46)


# ── C11 이벤트 ───────────────────────────────────────────────────
def test_event_calendar_fomc_and_option_expiry():
    evs = event_calendar.upcoming(today=date(2026, 10, 14), days=20)
    titles = [e["title"] for e in evs]
    assert any("FOMC" in t for t in titles)            # 10/28 (D-14)
    exp = [e for e in event_calendar.upcoming(today=date(2026, 10, 14), days=40) if "선물옵션" in e["title"]]
    assert exp and exp[0]["date"] == date(2026, 11, 12)   # 11월 둘째 목요일
    assert event_calendar.nth_weekday(2026, 10, 3, 2) == date(2026, 10, 8)


# ── A2 신선도 ────────────────────────────────────────────────────
def test_freshness_flags_one_day_late_korea():
    now = datetime(2026, 10, 14, 8, 20, tzinfo=ZoneInfo("Asia/Seoul"))
    raw = {"kospi": {"data_date": "2026-10-12"}, "sp500": {"data_date": "2026-10-13"}}
    out = data_freshness.audit(raw, now)
    assert len(out) == 1 and out[0].startswith("kospi")
    raw["kospi"]["data_date"] = "2026-10-13"
    assert data_freshness.audit(raw, now) == []


# ── D14 보정 ─────────────────────────────────────────────────────
def _res(conv, ok, n):
    return [{"horizon": 5, "conviction": conv, "correct": ok} for _ in range(n)]


def test_calibration_flags_overconfidence():
    res = _res("high", True, 3) + _res("high", False, 7) + _res("medium", True, 6) + _res("medium", False, 2)
    v = calibration.verdict(calibration.stats(res))
    assert v["overconfident"] and v["reliable"]
    assert "지시" in calibration.format_for_prompt(res)


def test_calibration_small_sample_not_reliable():
    v = calibration.verdict(calibration.stats(_res("high", True, 2)))
    assert not v["reliable"]


# ── 확신도 파싱 / 수급 모순 ──────────────────────────────────────
def test_opinion_conviction_parse():
    assert briefing_facts.parse_opinion_conviction("- 내 의견: 줄이기. 확신도 중 — 근거") == "medium"
    assert briefing_facts.parse_opinion_conviction("없음") is None


def test_flow_contradiction():
    flows = {"KOSPI": {"foreign": -3482.0, "partial": False}}
    assert briefing_facts.flow_contradictions("외국인 순매수 유입이 이어짐", flows)
    assert not briefing_facts.flow_contradictions("외국인 매도세 지속", flows)
    assert not briefing_facts.flow_contradictions("외국인 순매수", {"KOSPI": {"foreign": -3482.0, "partial": True}})


# ── E17 습관 / E18 모드 ──────────────────────────────────────────
def test_behavior_avg_down_and_panic():
    pos = {"name": "SK하이닉스", "qty": 19, "avg": 1935965.0}
    assert any("물타기" in n for n in behavior_check.check_buy(pos, 1456000, 2))
    assert not behavior_check.check_buy(pos, 2000000, 2)
    assert any("공황" in n for n in behavior_check.check_sell(pos, 1456000, market_chg=-5.4))
    assert behavior_check.check_sell(pos, 2100000, market_chg=0.5) == []


def test_briefing_mode():
    assert briefing_mode.decide(-5.46)["mode"] == "full"
    assert briefing_mode.decide(0.3)["mode"] == "short"
    d = briefing_mode.decide(0.3, holding_pl={"SK하이닉스": -24.8})
    assert d["mode"] == "full" and "손절" in d["reasons"][0]
    assert briefing_mode.decide(0.2, events=[{"impact": "HIGH", "title": "FOMC", "d_day": 1}])["mode"] == "full"


# ── D15 사용자 결정 ──────────────────────────────────────────────
def test_user_decision_summary_small_sample_warning():
    ds = [{"code": "a", "followed": "followed", "price_at": 100}, {"code": "b", "followed": "ignored", "price_at": 100}]
    txt = user_decisions.summarize(ds, {"a": 110, "b": 90})
    assert "+10.0%" in txt and "-10.0%" in txt and "표본" in txt
    assert user_decisions.record("x", "엉뚱한값") is False
