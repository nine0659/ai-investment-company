"""공식 통계 대시보드 회귀 테스트 (2026-10-21).

변화폭·YoY·스프레드는 코드 계산, 모든 수치에 기준일 병기, 오래된 시리즈는 표시.
(ECB HICP 데이터셋이 2025-11에서 멈춘 폐기 시리즈였던 발견에서 나온 신선도 규칙)
"""
from datetime import datetime, timedelta

import clients.official_stats_client as osc
from services import macro_dashboard as md


def test_bp_change_and_missing():
    assert md.bp_change([4.0, 4.1, 4.3], 1) == 20.0
    assert md.bp_change([4.0, 4.1, 4.3], 2) == 30.0
    assert md.bp_change([4.0], 1) is None
    assert md.bp_change([None, 4.1], 1) is None


def test_yoy_needs_same_month_last_year():
    pts = [("2025-09", 100.0), ("2025-10", 101.0), ("2026-09", 103.0)]
    assert md.yoy_pct(pts) == ("2026-09", 3.0)
    assert md.yoy_pct([("2026-09", 103.0)]) is None
    assert md.yoy_pct([]) is None


def test_stale_tag():
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m")
    assert "오래됨" in md.stale_tag(old, 60)
    fresh = datetime.now().strftime("%Y-%m-%d")
    assert md.stale_tag(fresh, 6) == ""


def _curve():
    base = [{"date": f"2026-10-{d:02d}", "3M": 4.0, "2Y": 4.5, "5Y": 4.6, "10Y": 4.4, "30Y": 4.8} for d in range(1, 21)]
    base[-1]["10Y"] = 4.50
    return base


def test_format_flags_inversion_and_computes_spread():
    text, ok, total = md.format_dashboard({"curve": _curve()})
    assert "10Y-2Y -0bp" in text or "10Y-2Y +0bp" in text or "10Y-2Y -" in text   # 4.50-4.50
    assert ok == 1 and total == 5
    c = _curve()
    c[-1]["10Y"] = 4.30
    text, _, _ = md.format_dashboard({"curve": c})
    assert "역전" in text


def test_format_with_all_sources_has_dates():
    data = {
        "curve": _curve(), "sofr": {"as_of": datetime.now().strftime("%Y-%m-%d"), "rate": 3.87},
        "bis": {"KR": [("2026-05", 2.5), ("2026-09", 3.0)]},
        "cli": {"KOR": [("2026-08", 102.0), ("2026-09", 102.5)]},
        "bls": {"cpi_index": [("2025-09", 100.0), ("2026-09", 103.74)],
                "unemployment": [("2026-08", 4.1), ("2026-09", 4.2)],
                "payrolls": [("2026-08", 159015.0), ("2026-09", 159044.0)]},
    }
    text, ok, total = md.format_dashboard(data)
    assert ok == total == 5
    assert "CPI 전년비 +3.74%(2026-09)" in text
    assert "비농업고용 전월비 +29천명" in text
    assert "한국 3.00%(1개월 +0.50%p)" in text
    assert "한국 102.5↑" in text


def test_empty_data_gives_empty_text():
    assert md.format_dashboard({}) == ("", 0, 5)


def test_build_records_fail_when_most_sources_down(monkeypatch):
    calls = []
    import services.job_ledger as jl
    monkeypatch.setattr(jl, "record_job", lambda n, s, d="": calls.append((n, s)))
    monkeypatch.setattr(md, "_collect", lambda: {"curve": _curve()})
    md._CACHE.update(t=0.0, data=None)
    out = md.build_macro_dashboard(force=True)
    assert "미국 국채수익률" in out
    assert calls == [("branch:macro_data", "fail")]      # 5묶음 중 1개만 성공


def test_build_never_raises(monkeypatch):
    monkeypatch.setattr(md, "_collect", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert md.build_macro_dashboard(force=True) == ""


# ── 수집기 파서 ────────────────────────────────────────────────────

class _R:
    def __init__(self, text="", js=None):
        self.text, self._js = text, js
    def raise_for_status(self): pass
    def json(self): return self._js


def test_treasury_parser(monkeypatch):
    xml = ("<m:properties><d:NEW_DATE m:type=\"Edm.DateTime\">2026-10-20T00:00:00</d:NEW_DATE>"
           "<d:BC_3MONTH m:type=\"Edm.Double\">4.26</d:BC_3MONTH><d:BC_2YEAR m:type=\"Edm.Double\">4.79</d:BC_2YEAR>"
           "<d:BC_5YEAR m:type=\"Edm.Double\">4.9</d:BC_5YEAR><d:BC_10YEAR m:type=\"Edm.Double\">5.12</d:BC_10YEAR>"
           "<d:BC_30YEAR m:type=\"Edm.Double\">5.42</d:BC_30YEAR></m:properties>")
    monkeypatch.setattr(osc, "_get", lambda url, **k: _R(text=xml))
    rows = osc.fetch_treasury_curve(months=1)
    assert rows == [{"date": "2026-10-20", "3M": 4.26, "2Y": 4.79, "5Y": 4.9, "10Y": 5.12, "30Y": 5.42}]


def test_fetchers_return_empty_on_failure(monkeypatch):
    def boom(*a, **k): raise RuntimeError("down")
    monkeypatch.setattr(osc, "_get", boom)
    monkeypatch.setattr(osc.requests, "post", boom)
    assert osc.fetch_treasury_curve() == [] and osc.fetch_sofr() == {}
    assert osc.fetch_bis_policy_rates() == {} and osc.fetch_oecd_cli() == {} and osc.fetch_bls() == {}


def test_bls_rejects_non_success(monkeypatch):
    class P:
        def raise_for_status(self): pass
        def json(self): return {"status": "REQUEST_NOT_PROCESSED", "message": ["daily limit"]}
    monkeypatch.setattr(osc.requests, "post", lambda *a, **k: P())
    assert osc.fetch_bls() == {}


def test_official_doc_sources_include_policy_speeches():
    from clients.official_docs_client import _SOURCES
    names = {s[0] for s in _SOURCES}
    assert {"Fed", "한국은행", "ECB", "Fed 연설", "BIS 연설"} <= names


# ── 키 필요 소스 (ECOS·FRED·EIA, 2026-10-21) ──────────────────────────

def _ecos():
    cpi = [(f"2025-{m:02d}", 100.0 + m * 0.1) for m in range(1, 13)] + [("2026-01", 104.0)]
    cpi = [("2025-01", 100.0), ("2026-01", 102.5)]
    return {"base_rate": [("2026-08", 3.0), ("2026-09", 3.0)], "cpi": cpi,
            "current_account": [("2026-08", 46113.0)],
            "exports": [("2025-08", 58000000.0), ("2026-08", 98000000.0)],
            "usdkrw": [("2026-10-20", 1332.1), ("2026-10-21", 1328.0)]}


def test_ecos_formatting_units_and_yoy():
    out = md._fmt_ecos(_ecos())
    text = "\n".join(out)
    assert "기준금리 3.00%[2026-09]" in text
    assert "소비자물가 전년비 +2.50%[2026-01]" in text
    assert "경상수지 +461억달러[2026-08]" in text      # 46,113 백만달러 = 461억달러
    assert "수출 전년비 +69.0%[2026-08]" in text
    assert "원/달러 1,328.0(-4.1원)" in text


def test_fred_monthly_sentiment_uses_month_for_staleness():
    f = {"sentiment": [("2026-07-01", 55.2), ("2026-08-01", 51.7)], "vix": [("2026-10-15", 16.46), ("2026-10-19", 18.44)]}
    text = "\n".join(md._fmt_fred(f))
    assert "VIX 18.44(2026-10-19, 직전 대비 +1.98)" in text
    assert "미시간 소비자심리 51.7(2026-08, 직전 대비 -3.50)" in text


def test_eia_formatting():
    e = {"wti": [("2026-10-13", 100.86), ("2026-10-14", 101.91)],
         "crude_stocks": [("2026-10-02", 424134.0), ("2026-10-09", 415577.0)]}
    text = "\n".join(md._fmt_eia(e))
    assert "WTI $101.91(2026-10-14, 직전 대비 +1.05)" in text
    assert "415.6백만배럴" in text and "전주 대비 -8.6백만배럴" in text


def test_keyed_groups_count_only_when_configured():
    base = {"curve": _curve()}
    _, ok, total = md.format_dashboard(base)
    assert total == 5                                   # 키 미설정은 실패로 집계하지 않는다
    _, ok2, total2 = md.format_dashboard({**base, "ecos": _ecos(), "fred": {}, "eia": {}})
    assert total2 == 8 and ok2 == 2                     # 키는 있는데 수집 실패한 fred·eia는 집계됨


def test_keyed_fetchers_are_silent_without_key(monkeypatch):
    for k in ("ECOS_API_KEY", "FRED_API_KEY", "EIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert osc.fetch_ecos() == {} and osc.fetch_fred() == {} and osc.fetch_eia() == {}


def test_collect_skips_unconfigured_keyed_sources(monkeypatch):
    for k in ("ECOS_API_KEY", "FRED_API_KEY", "EIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ECOS_API_KEY", "x")
    for n in ("fetch_treasury_curve", "fetch_sofr", "fetch_bis_policy_rates", "fetch_oecd_cli", "fetch_bls"):
        monkeypatch.setattr(osc, n, lambda: {})
    monkeypatch.setattr(osc, "fetch_ecos", lambda: {"base_rate": [("2026-09", 3.0)]})
    d = md._collect()
    assert "ecos" in d and "fred" not in d and "eia" not in d


def test_ecos_key_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("ECOS_API_KEY", "SECRETKEY123")
    def boom(url, **k): raise RuntimeError(f"fail {url}")
    monkeypatch.setattr(osc, "_get", boom)
    with caplog.at_level("WARNING"):
        assert osc.fetch_ecos() == {}
    assert "SECRETKEY123" not in caplog.text


# ── 관세청 수출입 (공공데이터포털 Newtrade, 2026-10-21) ─────────────────

def _customs():
    return {"exports": [("2025-09", 60e9), ("2026-08", 98.4e9), ("2026-09", 121e9), ("2026-10", 61.6e9)],
            "imports": [("2025-09", 50e9), ("2026-09", 71e9), ("2026-10", 47e9)],
            "balance": [("2026-09", 50e9), ("2026-10", 14.5e9)]}


def test_customs_excludes_partial_month_from_yoy_and_balance():
    text = "\n".join(md._fmt_customs(_customs(), today="2026-10-21"))
    assert "수출 1,210억달러(전년비 +101.7%)[2026-09]" in text
    assert "수입 710억달러(전년비 +42.0%)[2026-09]" in text
    assert "무역수지 +500억달러[2026-09]" in text            # 10월 부분치(145억)가 아니라 9월
    assert "당월(2026-10) 수출 누계 616억달러(집계 중·전년 동기 비교 불가)" in text


def test_customs_parser_and_period_split(monkeypatch):
    monkeypatch.setenv("DATA_GO_KR_KEY", "SECRETKEY999")
    calls = []

    def fake_get(url, **kw):
        p = kw["params"]
        calls.append((p["strtYymm"], p["endYymm"]))
        xml = ("<response><header><resultCode>00</resultCode></header><body><items>"
               f"<item><balPayments>10</balPayments><expDlr>300</expDlr><impDlr>290</impDlr><year>{p['endYymm'][:4]}.{p['endYymm'][4:]}</year></item>"
               "<item><balPayments>1</balPayments><expDlr>1</expDlr><impDlr>1</impDlr><year>총계</year></item>"
               "</items></body></response>")
        return _R(text=xml)
    monkeypatch.setattr(osc, "_get", fake_get)
    out = osc.fetch_customs()
    assert len(calls) == 2 and calls[0][1] > calls[1][1]       # 1년 이내 두 구간으로 나눠 요청
    assert all(len(v) == 2 for v in out.values())              # '총계' 행은 제외, 구간당 1건씩
    assert out["exports"][-1][1] == 300.0


def test_customs_error_response_returns_empty_and_hides_key(monkeypatch, caplog):
    monkeypatch.setenv("DATA_GO_KR_KEY", "SECRETKEY999")
    monkeypatch.setattr(osc, "_get", lambda url, **k: _R(text="<response><header><resultCode>99</resultCode>"
                                                              "<resultMsg>SECRETKEY999 bad</resultMsg></header></response>"))
    with caplog.at_level("WARNING"):
        assert osc.fetch_customs() == {}
    assert "SECRETKEY999" not in caplog.text


def test_customs_silent_without_key(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    assert osc.fetch_customs() == {}
