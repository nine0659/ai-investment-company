"""평일 17:40 '오늘의 발굴·공시 요약' 회귀 테스트.

핵심 규약: 보낼 내용이 없으면 무발송(success) / 오늘분 스냅샷이 한국·미국 모두 없으면 조용히 넘어가지 않고 fail /
발송 실패는 fail + 슬롯 반환 / 비교할 어제 기록이 없으면 '신규'라고 단정하지 않는다 / 보유 종목은 후보에서 제외하고 공시만 알린다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

import scheduler
from db.database import get_conn, init_db
from services import daily_digest as dd
from services import job_ledger

_KST = ZoneInfo("Asia/Seoul")


def _d(days_ago=0):
    return (datetime.now(_KST) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
def db():
    init_db()
    tables = ("universe_screen_daily", "us_screen_daily", "dart_events")
    with get_conn() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM {t}"))
    yield
    with get_conn() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM {t}"))


def _kr(date, code, score=4, mcap=5000.0):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO universe_screen_daily (date, code, name, market, close, chg_pct, mcap_eok, per, pbr, cns_per, eps, "
            "target, high52, score, signals) VALUES (:d, :c, :n, 'KOSPI', 10000, 1.0, :m, 8, 0.8, 6, 1000, 15000, 20000, :s, "
            "'수익성,저평가')"), {"d": date, "c": code, "n": f"종목{code}", "m": mcap, "s": score})


def _us(date, code, score=4):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO us_screen_daily (date, code, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, high52, score, "
            "signals) VALUES (:d, :c, :n, 'Tech', 100, 1.0, 5e9, 12, 0.2, 130, 200, :s, '수익성,저평가')"),
                  {"d": date, "c": code, "n": f"US{code}", "s": score})


def _ev(no, code, typ, pol, summary="", metric=None, date=None):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, summary, "
            "metric) VALUES (:n, :d, :c, '회사', 'KOSPI', 'x', :t, :p, :s, :m)"),
                  {"n": no, "d": date or _d(0), "c": code, "t": typ, "p": pol, "s": summary, "m": metric})


def test_first_day_does_not_claim_new(db):
    _kr(_d(0), "111111")
    _us(_d(0), "AAA")
    r = dd.build()
    assert r["text"] and "신규' 표시는 내일부터" in r["text"] and "🆕" not in r["text"]
    assert r["kr_ok"] and r["us_ok"]


def test_shows_only_newly_lit_vs_yesterday(db):
    _kr(_d(1), "111111")
    _kr(_d(0), "111111")
    _kr(_d(0), "222222")
    _us(_d(1), "AAA")
    _us(_d(0), "AAA")
    _us(_d(0), "BBB")
    t = dd.build()["text"]
    assert "222222" in t and "111111" not in t and "USBBB" in t and "USAAA" not in t
    assert "어제 대비 새로 신호가 켜진" in t


def test_holdings_excluded_from_candidates_but_disclosures_reported(db):
    _kr(_d(1), "000001")
    _kr(_d(0), "000001")
    _kr(_d(0), "222222")                                  # 보유 아님 → 신규 후보
    _kr(_d(0), "333333")
    _ev("H1", "333333", "유상증자", "악재")
    _ev("H2", "333333", "위험해소·절차", "중립")           # 무시돼야 함
    _us(_d(0), "AAA")
    t = dd.build(holding_codes={"333333"})["text"]
    assert "내 보유 종목 공시" in t and "유상증자" in t and "위험해소" not in t
    assert "종목333333" not in t.split("🇰🇷")[1]          # 후보 목록엔 보유 종목이 없다


def test_candidate_line_includes_good_and_bad_events(db):
    _kr(_d(1), "999999")
    _kr(_d(0), "222222")
    _ev("E1", "222222", "자사주취득", "호재")
    _ev("E2", "222222", "유상증자", "악재")
    t = dd.build()["text"]
    assert "공시: 자사주취득" in t and "⚠악재 공시: 유상증자" in t


def test_numbers_computed_by_code(db):
    _kr(_d(0), "111111")
    _us(_d(0), "AAA")
    t = dd.build()["text"]
    assert "목표가 대비 +50%" in t and "52주 고점 대비 -50%" in t            # 15000/10000, 10000/20000


def test_empty_day_returns_none_text(db):
    _kr(_d(1), "111111")
    _kr(_d(0), "111111")                                  # 어제와 같은 후보뿐
    _us(_d(1), "AAA")
    _us(_d(0), "AAA")
    r = dd.build()
    assert r["text"] is None and r["kr_ok"] and r["us_ok"]


def test_one_market_missing_is_stated_not_silent(db):
    _kr(_d(0), "111111")                                  # 미국 오늘분 없음
    t = dd.build()["text"]
    assert "오늘분 미국 스크리닝 없음" in t


def test_run_job_fail_when_both_snapshots_missing(db, monkeypatch):
    sent = []
    st, detail, body = dd.run_daily_job(send_fn=lambda m: sent.append(m) or True, holding_codes=set())
    assert st == "fail" and body is None and not sent and "스냅샷" in detail


def test_run_job_silent_when_nothing_to_report(db):
    _kr(_d(1), "111111"); _kr(_d(0), "111111")
    _us(_d(1), "AAA"); _us(_d(0), "AAA")
    sent = []
    st, detail, body = dd.run_daily_job(send_fn=lambda m: sent.append(m) or True, holding_codes=set())
    assert st == "success" and "무발송" in detail and body is None and not sent


def test_run_job_sends_and_reports_counts(db):
    _kr(_d(1), "111111"); _kr(_d(0), "111111"); _kr(_d(0), "222222")
    _us(_d(1), "AAA"); _us(_d(0), "AAA")
    sent = []
    st, detail, body = dd.run_daily_job(send_fn=lambda m: sent.append(m) or True, holding_codes=set())
    assert st == "success" and len(sent) == 1 and body == sent[0] and "한국 1" in detail


def test_run_job_send_failure_is_fail(db):
    _kr(_d(0), "111111"); _us(_d(0), "AAA")
    st, detail, body = dd.run_daily_job(send_fn=lambda m: False, holding_codes=set())
    assert st == "fail" and "발송 실패" in detail and body is None


def test_holding_lookup_failure_does_not_kill_job(db, monkeypatch):
    import services.portfolio_service as ps
    monkeypatch.setattr(ps, "get_portfolio", lambda: (_ for _ in ()).throw(RuntimeError("db")))
    _kr(_d(0), "111111"); _us(_d(0), "AAA")
    st, _, body = dd.run_daily_job(send_fn=lambda m: True)
    assert st == "success" and body


# ── 스케줄러 잡: 중복 방지 가드·슬롯 반환 ─────────────────────────────────────

def _patch_job(monkeypatch, claim=True, result=("success", "x", "본문")):
    rec, claims, released = [], [], []
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr("services.report_service.claim_report_slot", lambda d, t: claims.append(t) or claim)
    monkeypatch.setattr("services.report_service.release_report_slot", lambda d, t: released.append(t))
    monkeypatch.setattr(dd, "run_daily_job", lambda *a, **k: result)
    return rec, claims, released


def test_job_uses_claim_guard_and_skips_when_already_claimed(monkeypatch):
    rec, claims, released = _patch_job(monkeypatch, claim=False)
    scheduler.job_daily_digest()
    assert claims == ["daily_digest"] and rec == [] and released == []


def test_job_keeps_slot_after_successful_send(monkeypatch):
    rec, claims, released = _patch_job(monkeypatch)
    scheduler.job_daily_digest()
    assert rec == [("daily_digest", "success")] and released == []


def test_job_releases_slot_when_nothing_sent_or_failed(monkeypatch):
    rec, _, released = _patch_job(monkeypatch, result=("success", "무발송", None))
    scheduler.job_daily_digest()
    assert released == ["daily_digest"]
    rec, _, released = _patch_job(monkeypatch, result=("fail", "x", None))
    scheduler.job_daily_digest()
    assert rec == [("daily_digest", "fail")] and released == ["daily_digest"]


def test_job_survives_exception_and_releases(monkeypatch):
    rec, _, released = _patch_job(monkeypatch)
    monkeypatch.setattr(dd, "run_daily_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.job_daily_digest()
    assert rec == [("daily_digest", "fail")] and released == ["daily_digest"]


def test_job_skips_non_trading_day(monkeypatch):
    rec, claims, _ = _patch_job(monkeypatch)
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    scheduler.job_daily_digest()
    assert rec == [("daily_digest", "skipped")] and claims == []


def test_ledger_expects_digest_every_weekday():
    for wd in range(5):
        assert "daily_digest" in job_ledger._EXPECTED_BY_WEEKDAY[wd]
    assert job_ledger._EXPECTED_BY_WEEKDAY[5] == []


def test_us_screener_finishes_before_digest():
    """미국 스크리너는 약 14분 걸린다. 17:40 요약보다 충분히 앞서 시작해야 오늘분을 보낸다(2026-10-22 설계 제약)."""
    src = open(scheduler.__file__, encoding="utf-8").read()
    i = src.index('id="us_universe_screen"')
    block = src[max(0, i - 200):i]
    assert "hour=17, minute=15" in block


def test_friday_digest_appends_scorecard_only_when_observations_exist(db, monkeypatch):
    _kr(_d(1), "111111"); _kr(_d(0), "111111"); _kr(_d(0), "222222")
    _us(_d(1), "AAA"); _us(_d(0), "AAA")
    from services import screen_scorecard
    monkeypatch.setattr(screen_scorecard, "format_report", lambda *a, **k: "🧪 스크리너 성적표 TEST")
    assert "스크리너 성적표 TEST" in dd.build(force_scorecard=True)["text"]
    assert dd.build(force_scorecard=True)["counts"]["scorecard"] == 1
    monkeypatch.setattr(screen_scorecard, "format_report", lambda *a, **k: "")
    r = dd.build(force_scorecard=True)
    assert "성적표" not in r["text"] and r["counts"]["scorecard"] == 0


def test_scorecard_failure_does_not_break_digest(db, monkeypatch):
    _kr(_d(1), "111111"); _kr(_d(0), "111111"); _kr(_d(0), "222222")
    _us(_d(1), "AAA"); _us(_d(0), "AAA")
    from services import screen_scorecard
    monkeypatch.setattr(screen_scorecard, "format_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert dd.build(force_scorecard=True)["text"]


def test_wait_returns_immediately_when_both_ready():
    sleeps = []
    assert dd.wait_for_snapshots(sleep_fn=sleeps.append, ready_fn=lambda: (True, True)) == (True, True)
    assert sleeps == []


def test_wait_polls_until_late_snapshot_arrives():
    states = iter([(True, False), (True, False), (True, True)])
    sleeps = []
    assert dd.wait_for_snapshots(max_wait=600, poll=60, sleep_fn=sleeps.append, ready_fn=lambda: next(states)) == (True, True)
    assert sleeps == [60, 60]


def test_wait_gives_up_after_max_wait_and_reports_state():
    sleeps = []
    assert dd.wait_for_snapshots(max_wait=180, poll=60, sleep_fn=sleeps.append, ready_fn=lambda: (True, False)) == (True, False)
    assert len(sleeps) == 3


def test_scheduler_job_requests_wait(monkeypatch):
    seen = {}
    rec, _, _ = _patch_job(monkeypatch)
    monkeypatch.setattr(dd, "run_daily_job", lambda *a, **k: seen.update(k) or ("success", "x", "본문"))
    scheduler.job_daily_digest()
    assert seen.get("wait") is True


def test_snapshots_ready_reflects_today_rows(db):
    assert dd.snapshots_ready() == (False, False)
    _kr(_d(0), "111111")
    assert dd.snapshots_ready() == (True, False)
    _us(_d(0), "AAA")
    assert dd.snapshots_ready() == (True, True)


def test_digest_attaches_headlines_to_top_candidates_only(db, monkeypatch):
    _kr(_d(1), "999999")
    for i in range(1, 6):
        _kr(_d(0), f"22222{i}")
    _us(_d(1), "ZZZ")
    _us(_d(0), "AAA")
    import services.candidate_news as cn
    asked = []
    monkeypatch.setattr(cn, "lines_for", lambda name, market="KR", n=2, fetch_fn=None: asked.append((name, market)) or [f"     📰 뉴스-{name}"])
    t = dd.build()["text"]
    kr_asked = [a for a in asked if a[1] == "KR"]
    assert len(kr_asked) == dd._NEWS_TOP and t.count("📰 뉴스-") == dd._NEWS_TOP + 1       # 한국 3 + 미국 1
    assert "기사 제목 원문" in t


def test_digest_survives_news_failure(db, monkeypatch):
    _kr(_d(1), "999999"); _kr(_d(0), "222222")
    _us(_d(1), "ZZZ"); _us(_d(0), "AAA")
    import services.candidate_news as cn
    monkeypatch.setattr(cn, "lines_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    assert dd.build()["text"]
