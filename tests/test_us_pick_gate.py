"""2026-10-14: 미국 추천 관문 — 쿨다운·쏠림 업종·급등 추격 제외, 업종 중복/개수 강제."""
from services import pick_gate as pg

REPORT = """## 성장주 TOP3
1. [NVDA] (엔비디아) | 매수가 $100 | 목표가 $120 | 손절가 $90
   근거: 가

2. [AMD] (AMD) | 매수가 $50 | 목표가 $60 | 손절가 $45
   근거: 나

3. [META] (메타) | 매수가 $300 | 목표가 $350 | 손절가 $270
   근거: 다

## 배당 ETF TOP2
1. [SCHD] (ETF) | 매수가 $80 | 배당수익률 3.5% | 보유기간 6개월 권장
   근거: 라

## 섹터 ETF TOP2
1. [XLV] (헬스케어) | 매수가 $140 | 목표가 $150
   근거: 마

## 이번 주 미국 시장 전망
전망
"""


def test_exclusion_reasons():
    sw = {"반도체": 84.0}
    assert "반복 방지" in pg.us_exclusion_reason({"ticker": "AAPL"}, {"AAPL"}, sw)
    assert "반도체 비중이 이미 84%" in pg.us_exclusion_reason({"ticker": "NVDA", "change_1w": 1, "change_1m": 2}, set(), sw)
    assert "추격" in pg.us_exclusion_reason({"ticker": "PLTR", "change_1w": 20, "change_1m": 5}, set(), sw)
    assert "추격" in pg.us_exclusion_reason({"ticker": "PLTR", "change_1w": 2, "change_1m": 40}, set(), sw)
    assert pg.us_exclusion_reason({"ticker": "MSFT", "change_1w": 2, "change_1m": 5}, set(), sw) is None


def test_enforce_us_growth_group_and_cap_and_renumber():
    new, kept = pg.enforce_us_growth(REPORT, 3)
    assert kept == ["NVDA", "META"]                          # AMD는 NVDA와 같은 반도체 → 탈락
    assert "[AMD]" not in new and "2. [META]" in new         # 번호 재부여
    assert "## 배당 ETF TOP2" in new and "[SCHD]" in new     # 다른 섹션은 그대로
    new1, kept1 = pg.enforce_us_growth(REPORT, 1)
    assert kept1 == ["NVDA"] and "[META]" not in new1


def test_enforce_no_growth_section_is_noop():
    txt = "## 배당 ETF TOP2\n1. [SCHD] x\n"
    assert pg.enforce_us_growth(txt, 2) == (txt, [])


def test_picks_in_report_by_category():
    p = pg.us_picks_in_report(REPORT)
    assert p == {"growth": ["NVDA", "AMD", "META"], "dividend": ["SCHD"], "sector": ["XLV"]}


def test_us_market_gate_thresholds_narrower_than_kospi():
    flat = {f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": 100.0 for i in range(60)}
    assert pg.us_market_gate(flat)["mode"] == "normal"
    drop = dict(flat)
    keys = sorted(drop)
    for k, v in zip(keys[-6:], [100, 99, 98, 97, 96, 94]):          # 5일 -6%
        drop[k] = v
    assert pg.us_market_gate(drop)["mode"] == "crash"
    assert pg.market_gate(drop)["mode"] != "crash"                    # 같은 움직임은 KOSPI 기준으로는 급락이 아님


def test_recent_us_picks_empty_db_is_safe():
    assert isinstance(pg.recent_us_picks(), set)
