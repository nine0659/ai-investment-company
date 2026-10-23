"""브리핑 전망·발굴 섹션 회귀 테스트 — 코드가 만든 줄만, 쉬운 말, 검증 표시, 실패 격리 (2026-10-23)."""
from services import briefing_sections as bs


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
    monkeypatch.setattr(bs, "_sector_lines", lambda: [])
    text, cnt = bs.build({"005930"})
    assert cnt == {"kr": 1, "us": -1, "buyback": 0, "sector": 0}      # 실패는 -1로 드러난다(호출부가 헬스체크 기록)
    assert "A(000001)" in text and bs.DISCLAIMER in text and "미국 후보" not in text


def test_build_empty_returns_blank(monkeypatch):
    for n in ("_kr_lines", "_us_lines"):
        monkeypatch.setattr(bs, n, lambda ex, n=0: [])
    monkeypatch.setattr(bs, "_buyback_lines", lambda n=3: [])
    monkeypatch.setattr(bs, "_sector_lines", lambda: [])
    assert bs.build(set())[0] == ""


def test_exclude_codes_passed_to_sources(monkeypatch):
    seen = {}
    monkeypatch.setattr(bs, "_kr_lines", lambda ex, n=3: seen.setdefault("kr", ex) and [])
    monkeypatch.setattr(bs, "_us_lines", lambda ex, n=2: [])
    monkeypatch.setattr(bs, "_buyback_lines", lambda n=3: [])
    monkeypatch.setattr(bs, "_sector_lines", lambda: [])
    bs.build({"000660"})
    assert seen["kr"] == {"000660"}          # 보유 종목은 발굴 후보에서 제외
