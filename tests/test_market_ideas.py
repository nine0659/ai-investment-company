"""시장 전체 아이디어 블록 회귀 테스트 — 오래된 스냅샷 미사용, 보유 종목 제외, 신규 후보 표시, 공시 하이라이트 필터,
소스 하나가 실패해도 나머지는 들어가는지(전부-아니면-전무 방지), advisor_context 배선."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from db.database import get_conn, init_db
from services import market_ideas as mi

_KST = ZoneInfo("Asia/Seoul")


def _d(days_ago=0):
    return (datetime.now(_KST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
def db():
    init_db()
    with get_conn() as c:
        for t in ("universe_screen_daily", "us_screen_daily", "dart_events"):
            c.execute(text(f"DELETE FROM {t}"))
    yield
    with get_conn() as c:
        for t in ("universe_screen_daily", "us_screen_daily", "dart_events"):
            c.execute(text(f"DELETE FROM {t}"))


def _kr(date, code, score=4, mcap=5000.0, name=None):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO universe_screen_daily (date, code, name, market, close, chg_pct, mcap_eok, per, pbr, cns_per, eps, "
            "target, high52, score, signals) VALUES (:d, :c, :n, 'KOSPI', 10000, 1.0, :m, 8, 0.8, 6, 1000, 15000, 20000, :s, "
            "'수익성,저평가')"), {"d": date, "c": code, "n": name or f"종목{code}", "m": mcap, "s": score})


def _us(date, code, score=4):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO us_screen_daily (date, code, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, high52, score, "
            "signals) VALUES (:d, :c, :n, 'Tech', 100, 1.0, 5e9, 12, 0.2, 130, 200, :s, '수익성,저평가')"),
                  {"d": date, "c": code, "n": f"US{code}", "s": score})


def _ev(no, code, typ, pol, summary="", metric=None, date=None, name="회사"):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, summary, "
            "metric) VALUES (:n, :d, :c, :nm, 'KOSPI', 'x', :t, :p, :s, :m)"),
                  {"n": no, "d": date or _d(0), "c": code, "nm": name, "t": typ, "p": pol, "s": summary, "m": metric})


def test_stale_snapshot_not_used(db):
    _kr(_d(10), "111111")
    _us(_d(10), "AAA")
    assert mi.build_block() == ""


def test_holdings_excluded_and_numbers_computed(db):
    _kr(_d(0), "111111")
    _kr(_d(0), "222222")
    out = mi.build_block({"111111"})
    assert "111111" not in out and "222222" in out
    assert "목표가 대비 +50%" in out and "52주 고점 대비 -50%" in out          # 15000/10000, 10000/20000 — 코드 계산


def test_new_candidate_flagged_only_when_previous_snapshot_exists(db):
    _kr(_d(1), "111111")
    _kr(_d(0), "111111")
    _kr(_d(0), "222222")
    out = mi.build_block()
    line_new = [l for l in out.splitlines() if "222222" in l][0]
    line_old = [l for l in out.splitlines() if "111111" in l][0]
    assert "🆕" in line_new and "🆕" not in line_old


def test_no_new_flag_on_first_snapshot(db):
    _kr(_d(0), "111111")
    assert "🆕" not in mi.build_block()


def test_low_score_not_listed(db):
    _kr(_d(0), "111111", score=2)
    assert mi.build_block() == ""


def test_event_attached_to_candidate(db):
    _kr(_d(0), "111111")
    _ev("A1", "111111", "자사주취득", "호재")
    _ev("A2", "111111", "유상증자", "악재")
    out = mi.build_block()
    assert "최근 공시: 자사주취득" in out and "⚠최근 악재 공시: 유상증자" in out


def test_highlights_filter(db):
    _kr(_d(0), "111111", mcap=50000.0, name="대형A")
    _kr(_d(0), "222222", mcap=500.0, name="소형B")
    _ev("E1", "111111", "공급계약", "호재", "계약금액 1000억원", 15.0, name="대형A")        # 포함
    _ev("E2", "111111", "공급계약", "호재", "계약금액 10억원", 2.0, name="대형A")          # 매출 대비 작아 제외
    _ev("E3", "222222", "자사주취득", "호재", name="소형B")                                # 시총 미달 제외
    _ev("E4", "111111", "유상증자", "악재", name="대형A")                                  # 악재 포함
    _ev("E5", "111111", "위험해소·절차", "중립", name="대형A")                             # 제외
    _ev("E6", "111111", "자사주취득", "호재", name="대형A", date=_d(9))                    # 오래돼 제외
    hl = "\n".join(mi.event_highlights())
    assert "계약금액 1000억원" in hl and "유상증자" in hl
    assert "10억원" not in hl and "소형B" not in hl and "위험해소" not in hl and hl.count("자사주취득") == 0


def test_one_source_failing_does_not_drop_the_rest(db, monkeypatch):
    _us(_d(0), "AAA")
    _kr(_d(0), "111111")
    monkeypatch.setattr(mi, "kr_picks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = mi.build_block()
    assert "USAAA" in out and "111111" not in out


def test_advisor_context_includes_block(monkeypatch):
    from services import advisor_context
    monkeypatch.setattr(mi, "build_block", lambda exclude=None: "[🔭 TEST-BLOCK]")
    advisor_context._CACHE.clear()
    res = advisor_context.build({}, {}, "pre_market")
    assert any("TEST-BLOCK" in b for b in res["blocks"])
    advisor_context._CACHE.clear()


def test_advisor_context_survives_block_failure(monkeypatch):
    from services import advisor_context
    monkeypatch.setattr(mi, "build_block", lambda exclude=None: (_ for _ in ()).throw(RuntimeError("boom")))
    advisor_context._CACHE.clear()
    res = advisor_context.build({}, {}, "pre_market")
    assert isinstance(res["blocks"], list) and not any("TEST-BLOCK" in b for b in res["blocks"])
    advisor_context._CACHE.clear()


def test_highlights_mark_validated_event_types(db):
    _kr(_d(0), "111111", mcap=50000.0, name="대형A")
    _ev("V1", "111111", "자사주취득", "호재", name="대형A")
    _ev("V2", "111111", "공급계약", "호재", "계약금액 1000억원", 15.0, name="대형A")
    hl = "\n".join(mi.event_highlights())
    line_buyback = [l for l in hl.splitlines() if "자사주취득" in l][0]
    line_contract = [l for l in hl.splitlines() if "공급계약" in l][0]
    assert "✔호재(과거 검증)" in line_buyback and "✔" not in line_contract


def test_highlights_show_earnings_as_reference_not_good_news(db):
    _kr(_d(0), "111111", mcap=50000.0, name="대형A")
    _ev("ER1", "111111", "잠정실적", "중립", "영업이익 +127.1%", 127.1, name="대형A")
    hl = "\n".join(mi.event_highlights())
    assert "실적 발표(참고)" in hl and "호재" not in hl
