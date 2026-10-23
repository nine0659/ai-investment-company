"""브리핑 전망·발굴 섹션 회귀 테스트 — 코드가 만든 줄만, 쉬운 말, 검증 표시, 실패 격리 (2026-10-23)."""
import pytest

from services import briefing_sections as bs


@pytest.fixture(autouse=True)
def _no_account(monkeypatch):
    monkeypatch.setattr(bs, "_action_lines", lambda ex: [])      # 계좌 조회는 candidate_action 테스트에서 따로 검증


def test_plain_signals_puts_validated_first_and_drops_profitability():
    out = bs.plain_signals("고점대비낙폭,저평가✔,수익성(참고),공시호재✔")
    assert out.split(" · ")[:2] == ["이익 대비 싼 편✔", "호재 공시✔"]
    assert "수익성" not in out and "고점 대비 많이 하락" in out


def test_plain_signals_empty():
    assert bs.plain_signals(None) == "" and bs.plain_signals("") == ""


def test_build_isolates_failures_and_marks_them(monkeypatch):
    monkeypatch.setattr(bs, "_kr_lines", lambda ex, n=3: ["· A(000001) 1,000원 — 이익 대비 싼 편✔"])
    monkeypatch.setattr(bs, "_us_lines", lambda ex, n=2: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bs, "_buyback_lines", lambda n=3: [])
    monkeypatch.setattr(bs, "sector_lines", lambda: []); monkeypatch.setattr(bs, "_board_lines", lambda: [])
    text, cnt = bs.build({"005930"})
    assert cnt == {"board": 0, "action": 0, "kr": 1, "us": -1, "buyback": 0, "sector": 0}      # 실패는 -1로 드러난다(호출부가 헬스체크 기록)
    assert "A(000001)" in text and bs.DISCLAIMER in text and "미국 후보" not in text


def test_build_empty_returns_blank(monkeypatch):
    for n in ("_kr_lines", "_us_lines"):
        monkeypatch.setattr(bs, n, lambda ex, n=0: [])
    monkeypatch.setattr(bs, "_buyback_lines", lambda n=3: [])
    monkeypatch.setattr(bs, "sector_lines", lambda: []); monkeypatch.setattr(bs, "_board_lines", lambda: [])
    assert bs.build(set())[0] == ""


def test_exclude_codes_passed_to_sources(monkeypatch):
    seen = {}
    monkeypatch.setattr(bs, "_kr_lines", lambda ex, n=3: seen.setdefault("kr", ex) and [])
    monkeypatch.setattr(bs, "_us_lines", lambda ex, n=2: [])
    monkeypatch.setattr(bs, "_buyback_lines", lambda n=3: [])
    monkeypatch.setattr(bs, "sector_lines", lambda: []); monkeypatch.setattr(bs, "_board_lines", lambda: [])
    bs.build({"000660"})
    assert seen["kr"] == {"000660"}          # 보유 종목은 발굴 후보에서 제외


def test_buyback_drops_financials_when_known(monkeypatch):
    from services import market_ideas as mi
    monkeypatch.setattr(mi, "validated_events", lambda days=3, min_mcap_eok=1000, n=8: [
        "  · 삼성E&A(028050) 시총 108,780억 — 자사주취득 결정 (2026-10-22)",
        "  · 현대차증권(001500) 시총 4,254억 — 자사주취득 결정 (2026-10-22)"])
    monkeypatch.setattr(bs, "_financial_codes", lambda: {"001500"})
    out = bs._buyback_lines()
    assert len(out) == 1 and "삼성E&A" in out[0]
    monkeypatch.setattr(bs, "_financial_codes", lambda: set())            # 업종 정보 없으면 거르지 못함(그대로)
    assert len(bs._buyback_lines()) == 2


def test_board_lines_state_only_with_caveat(monkeypatch):
    from services import industry_board as ib
    from datetime import datetime
    today = datetime.now(bs._KST).strftime("%Y-%m-%d")
    rows = [{"ind_no": "1", "name": "반도체", "med_ret_3m": 12.0, "regime": "강세"},
            {"ind_no": "2", "name": "건설", "med_ret_3m": -30.0, "regime": "약세"},
            {"ind_no": "ALL", "name": "전체", "med_ret_3m": -5.0, "regime": "-"}]
    monkeypatch.setattr(ib, "load_snapshots", lambda limit=6: [(today, rows)])
    out = bs._board_lines()
    assert "반도체(+12%)" in out[0] and "건설(-30%)" in out[1] and "예측력이 없었음" in out[-1]


def test_plain_signals_hides_broader_duplicate():
    out = bs.plain_signals("업종대비저PBR✔,자산대비저평가,업종대비저평가✔,저평가✔,공시호재✔")
    assert "같은 업종보다 장부가치 대비 쌈✔" in out and "같은 업종보다 싼 편✔" in out
    assert "장부가치 대비 쌈" not in out.replace("같은 업종보다 장부가치 대비 쌈", "") and "이익 대비 싼 편" not in out
