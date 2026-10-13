from services import briefing_quality as bq

_OK = "충분히 긴 정상 리포트 본문입니다. 수치와 근거가 들어 있습니다. " * 2


def _full_state(run_type="pre_market"):
    st = {f: _OK for f in bq._EXPECTED[run_type]}
    st.update(ceo_report=_OK, ceo_decisions={"x": 1}, debate_verdict="우세: 팽팽",
              ceo_fact_check={"count": 1, "total": 9})
    return st


def test_healthy_state_has_no_problems():
    r = bq.assess(_full_state(), "pre_market")
    assert r["problems"] == [] and r["metrics"]["empty"] == 0


def test_many_empty_or_failed_reports_is_problem():
    st = _full_state()
    for f in ("macro_report", "news_report", "risk_report"):
        st[f] = ""
    st["sector_report"] = "섹터 분석 실패"
    r = bq.assess(st, "pre_market")
    assert len(r["empty"]) == 4 and any("상류 리포트" in p for p in r["problems"])


def test_ceo_failure_and_missing_verdict_flagged():
    st = _full_state()
    st.update(ceo_report="브리핑 생성 실패", ceo_decisions={}, debate_verdict="")
    r = bq.assess(st, "pre_market")
    assert any("CEO 브리핑 생성 실패" in p for p in r["problems"])
    assert any("토론 판정" in p for p in r["problems"])


def test_unknown_run_type_is_skipped():
    assert bq.assess({}, "intraday1")["expected"] == 0


def test_record_uses_branch_prefix(monkeypatch):
    calls = []
    import services.job_ledger as jl
    monkeypatch.setattr(jl, "record_job", lambda n, s, d="": calls.append((n, s, d)))
    st = _full_state()
    st["macro_report"] = st["news_report"] = st["risk_report"] = st["sector_report"] = ""
    bq.record(st, "pre_market")
    assert calls[0][0] == "branch:briefing_quality" and calls[0][1] == "fail"
    calls.clear()
    bq.record(_full_state(), "pre_market")
    assert calls[0][1] == "success"
