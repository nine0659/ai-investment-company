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
