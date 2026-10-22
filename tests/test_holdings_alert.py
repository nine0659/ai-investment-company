"""보유 종목 공시 즉시 알림 회귀 테스트 — 보유 종목만, 사건당 1회(claim), 발송 실패 시 슬롯 반환, 해소 공시 무시."""
import scheduler
from services import disclosure_events as de
from services import holdings_alert as ha


def _ev(no, code="009150", typ="유상증자", pol="악재", summary=""):
    return {"rcept_no": no, "date": "2026-10-22", "stock_code": code, "corp_name": "삼성전기", "market": "KOSPI",
            "report_nm": "x", "event_type": typ, "polarity": pol, "summary": summary, "metric": None}


class _Slots:
    def __init__(self):
        self.taken, self.released = set(), []

    def claim(self, d, t):
        if t in self.taken:
            return False
        self.taken.add(t)
        return True

    def release(self, d, t):
        self.taken.discard(t)
        self.released.append(t)


def test_only_holdings_are_alerted_once_per_event():
    sent, slots = [], _Slots()
    evs = [_ev("A1"), _ev("A2", code="999999"), _ev("A3", typ="위험해소·절차", pol="중립")]
    n = ha.alert_new_events(evs, {"009150"}, lambda m: sent.append(m) or True, slots.claim, slots.release)
    assert n == 1 and len(sent) == 1 and "삼성전기(009150)" in sent[0] and "rcpNo=A1" in sent[0]
    # 같은 사건을 다시 훑어도(재실행·중복 트리거) 두 번 보내지 않는다
    n2 = ha.alert_new_events(evs, {"009150"}, lambda m: sent.append(m) or True, slots.claim, slots.release)
    assert n2 == 0 and len(sent) == 1


def test_claim_slot_name_is_per_event():
    slots = _Slots()
    ha.alert_new_events([_ev("A1"), _ev("A2")], {"009150"}, lambda m: True, slots.claim, slots.release)
    assert slots.taken == {"hold_A1", "hold_A2"}


def test_failed_send_releases_slot_for_retry():
    slots = _Slots()
    n = ha.alert_new_events([_ev("A1")], {"009150"}, lambda m: False, slots.claim, slots.release)
    assert n == 0 and slots.released == ["hold_A1"] and "hold_A1" not in slots.taken


def test_send_exception_is_contained_and_slot_released():
    slots = _Slots()

    def boom(m):
        raise RuntimeError("telegram down")
    assert ha.alert_new_events([_ev("A1")], {"009150"}, boom, slots.claim, slots.release) == 0
    assert slots.released == ["hold_A1"]


def test_message_has_label_summary_link_and_disclaimer():
    msg = ha._format(_ev("20261022000123", typ="잠정실적", pol="호재", summary="영업이익 +127.1%"))
    assert "📈 호재" in msg and "영업이익 +127.1%" in msg and "rcpNo=20261022000123" in msg and "원문에서 확인" in msg
    assert "⚠️ 악재" in ha._format(_ev("X"))


def test_run_once_no_holdings_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(de, "collect", lambda days=1: called.append(1))
    assert ha.run_once(holding_codes=set())["sent"] == 0 and not called


def test_run_once_reports_dart_failure(monkeypatch):
    monkeypatch.setattr(de, "collect", lambda days=1: {"ok": False, "errors": ["x"], "events": []})
    res = ha.run_once(holding_codes={"009150"})
    assert res["ok"] is False and res["sent"] == 0


def test_run_once_alerts_new_holding_events(monkeypatch):
    monkeypatch.setattr(de, "collect", lambda days=1: {"ok": True, "errors": [], "new": 2,
                                                       "events": [_ev("A1"), _ev("A2", code="1")]})
    seen = []
    monkeypatch.setattr(ha, "alert_new_events", lambda evs, codes, *a, **k: seen.append((len(evs), codes)) or 1)
    res = ha.run_once(holding_codes={"009150"})
    assert res == {"sent": 1, "new": 2, "ok": True} and seen == [(2, {"009150"})]


def test_scheduler_job_records_only_failures(monkeypatch):
    rec = []
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    monkeypatch.setattr(ha, "run_once", lambda: {"ok": True, "sent": 0, "new": 0})
    scheduler.job_holdings_disclosure()
    assert rec == []                                         # 정상 실행은 기록하지 않는다
    monkeypatch.setattr(ha, "run_once", lambda: {"ok": False, "errors": ["x"]})
    scheduler.job_holdings_disclosure()
    monkeypatch.setattr(ha, "run_once", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.job_holdings_disclosure()
    assert rec == [("holdings_disclosure", "fail"), ("holdings_disclosure", "fail")]


def test_scheduler_job_skips_non_trading_day(monkeypatch):
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    monkeypatch.setattr(ha, "run_once", lambda: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")))
    scheduler.job_holdings_disclosure()
