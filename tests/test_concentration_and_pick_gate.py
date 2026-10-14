"""2026-10-14: 집중도 규칙(종목 상한·분할 축소안) + 주간 추천 관문(반복·쏠림·과열/급락) 회귀 테스트."""
import pytest

from services import concentration_rule as cr
from services import pick_gate as pg


def _p(code, name, value, price=1000.0, avg=1500.0, sector="반도체"):
    return {"code": code, "name": name, "qty": int(value / price), "avg": avg, "price": price,
            "value": value, "sector": sector}


POS = [_p("A", "에이", 80_000_000, price=10_000, avg=15_000), _p("B", "비", 20_000_000, sector="자동차")]
CAPS = {"stock": 50.0, "sector": 60.0}


# ── 집중도 규칙 ─────────────────────────────────────────────────
def test_stock_over_cap_and_trim_math():
    v = cr.evaluate(POS, CAPS)
    assert len(v) == 1 and v[0]["code"] == "A" and v[0]["weight"] == pytest.approx(80.0)
    p = v[0]["plan"]
    T = 100_000_000
    assert p["sell_reinvest_won"] == pytest.approx(0.8 * T - 0.5 * T)          # 옮기면 3천만원
    assert p["sell_cash_won"] == pytest.approx((0.8 * T - 0.5 * T) / 0.5)      # 현금화하면 6천만원
    # 현금화 후 비중이 정확히 상한이 되는지 검산: (V-s)/(T-s) == 0.5
    s = p["sell_cash_won"]
    assert (0.8 * T - s) / (T - s) == pytest.approx(0.5)
    assert p["per_day_won"] == pytest.approx(T * 0.10) and p["days_cash"] == 6 and p["days_reinvest"] == 3
    assert p["realized_per_share"] == -5_000


def test_no_violation_when_under_cap():
    ok = [_p("A", "에이", 45_000_000), _p("B", "비", 55_000_000, sector="자동차")]
    assert cr.evaluate([_p("A", "에이", 40_000_000), _p("B", "비", 30_000_000, sector="자동차"),
                        _p("C", "씨", 30_000_000, sector="금융")], CAPS) == []
    assert [v["code"] for v in cr.evaluate(ok, CAPS)] == ["B"]


def test_sector_violation_only_when_multiple_stocks():
    pos = [_p("A", "에이", 40_000_000), _p("B", "비", 30_000_000), _p("C", "씨", 30_000_000, sector="금융")]
    v = cr.evaluate(pos, CAPS)
    assert [x["kind"] for x in v] == ["sector"] and v[0]["name"] == "반도체"       # 종목은 50% 이하, 섹터 70%
    assert cr.evaluate([_p("A", "에이", 80_000_000), _p("B", "비", 20_000_000, sector="자동차")], CAPS)[0]["kind"] == "stock"


def test_alert_text_has_numbers_and_loss_warning():
    txt = cr.format_alert(cr.evaluate(POS, CAPS)[0])
    assert "80.0%" in txt and "상한 50%" in txt and "손실이 확정" in txt and "거래일" in txt


def test_check_decisions_flags_adding_to_overweight():
    viol = cr.evaluate(POS, CAPS)
    w = cr.check_decisions({"new_positions": [{"code": "A", "name": "에이"}],
                            "position_changes": [{"action": "add", "code": "A", "name": "에이"}]}, viol)
    assert len(w) == 2
    assert cr.check_decisions({"position_changes": [{"action": "reduce", "code": "A"}]}, viol) == []


def test_warn_if_adding():
    assert "집중도" in cr.warn_if_adding("A", POS)
    assert cr.warn_if_adding("B", POS) == ""


def test_caps_roundtrip_and_validation():
    assert cr.set_cap("stock", 40) and cr.get_caps()["stock"] == 40.0
    assert not cr.set_cap("stock", 5) and not cr.set_cap("etc", 50)
    cr.set_cap("stock", 50)


# ── 주간 추천 관문 ──────────────────────────────────────────────
def _cands(*codes):
    return [{"code": c, "name": "N" + c} for c in codes]


def test_filter_excludes_held_recent_and_overweight_sector():
    codes = ["005930", "000660", "005380", "000270", "105560", "055550", "086790", "316140", "207940",
             "068270", "035420", "035720", "017670", "033780"]
    kept, out = pg.filter_candidates(_cands(*codes), recent={"000270"}, held={"005380"}, sector_w={"반도체": 84.0})
    kept_codes = {d["code"] for d in kept}
    assert "005930" not in kept_codes and "000660" not in kept_codes      # 반도체 쏠림 → 제외
    assert "005380" not in kept_codes and "000270" not in kept_codes      # 보유 / 최근 추천
    assert {"105560", "207940"} <= kept_codes
    reasons = " ".join(w for _, w in out)
    assert "반도체 비중이 이미 84%" in reasons and "이미 보유" in reasons and "최근 28일" in reasons


def test_filter_relaxes_sector_when_pool_too_small():
    codes = ["005930", "000660", "105560"]
    kept, _ = pg.filter_candidates(_cands(*codes), recent=set(), held=set(), sector_w={"반도체": 84.0})
    assert {d["code"] for d in kept} == set(codes)          # 후보 고갈 방지(섹터 제외 해제)


def _closes(vals):
    return {f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": v for i, v in enumerate(vals)}


def test_market_gate_modes():
    base = [100.0] * 60
    assert pg.market_gate(_closes(base))["mode"] == "normal"
    assert pg.market_gate(_closes(base + [100.0] * 0))["max_picks"] == 3
    crash = base[:-6] + [100, 100, 100, 100, 100, 100]
    crash = [100.0] * 60 + [99, 97, 95, 93, 91, 90]                    # 5일 -10%
    g = pg.market_gate(_closes(crash))
    assert g["mode"] == "crash" and g["max_picks"] == 2 and "분할" in g["note"]
    heated = [100.0] * 40 + [100 + i * 1.0 for i in range(1, 26)]       # 20일 +20% 이상
    g = pg.market_gate(_closes(heated))
    assert g["mode"] == "heated" and g["max_picks"] == 1
    assert pg.market_gate(_closes([100.0] * 10))["mode"] == "unknown"


def test_enforce_one_per_sector_and_cap():
    recs = [{"code": "005930"}, {"code": "000660"}, {"code": "105560"}, {"code": "207940"}]
    out = pg.enforce(recs, 3)
    assert [r["code"] for r in out] == ["005930", "105560", "207940"]     # 하이닉스는 같은 반도체 → 탈락
    assert [r["code"] for r in pg.enforce(recs, 1)] == ["005930"]


def test_truncate_report_keeps_footer():
    rep = ("💡 한 줄 결론: x\n\n1. A (1) — ...\n   왜: a\n\n2. B (2) — ...\n   왜: b\n\n3. C (3) — ...\n   왜: c\n\n📌 시장 한 줄 평: z")
    out = pg.truncate_report(rep, 2)
    assert "3. C" not in out and "2. B" in out and out.rstrip().endswith("📌 시장 한 줄 평: z")
    assert pg.truncate_report(rep, 3) == rep


def test_constraint_block_content():
    b = pg.constraint_block({"max_picks": 2, "reason": "급락 직후", "note": "1/3씩 분할"},
                            [("현대차", "이미 보유 중")], {"반도체": 84.0})
    assert "최대 2개" in b and "1/3씩 분할" in b and "반도체 84%" in b and "현대차" in b and "추천 없음" in b
