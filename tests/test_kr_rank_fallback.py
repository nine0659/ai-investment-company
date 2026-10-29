"""KIS 순위가 비면 네이버 전 종목 시세로 대체 — 2026-10-28 (KIS 조회 메서드 단일장애점 완화)."""
import services.kr_rank_fallback as fb
from clients.kis_client import KISClient

ROWS = [
    {"code": "000001", "name": "가", "market": "KOSPI", "close": 1000.0, "chg_pct": 5.0, "volume": 10.0, "value_eok": 1.0, "mcap_eok": 5000},
    {"code": "000002", "name": "나", "market": "KOSPI", "close": 2000.0, "chg_pct": -7.0, "volume": 90.0, "value_eok": 9.0, "mcap_eok": 5000},
    {"code": "000003", "name": "다", "market": "KOSPI", "close": 3000.0, "chg_pct": 1.0, "volume": 50.0, "value_eok": 30.0, "mcap_eok": 5000},
    {"code": "100001", "name": "큐", "market": "KOSDAQ", "close": 500.0, "chg_pct": 9.0, "volume": 70.0, "value_eok": 2.0, "mcap_eok": 2000},
]


def _fb(monkeypatch):
    monkeypatch.setattr(fb, "_universe_rows", lambda: ROWS)


def test_rankings_sorted_and_market_filtered(monkeypatch):
    _fb(monkeypatch)
    assert [r["stck_shrn_iscd"] for r in fb.rank_from_naver("volume", "J")] == ["000002", "000003", "000001"]
    assert [r["stck_shrn_iscd"] for r in fb.rank_from_naver("amount", "J")][0] == "000003"
    assert [r["stck_shrn_iscd"] for r in fb.rank_from_naver("fluctuation", "J", rise=True)][0] == "000001"
    assert [r["stck_shrn_iscd"] for r in fb.rank_from_naver("fluctuation", "J", rise=False)][0] == "000002"
    assert [r["stck_shrn_iscd"] for r in fb.rank_from_naver("volume", "Q")] == ["100001"]


def test_rows_use_kis_field_names(monkeypatch):
    _fb(monkeypatch)
    r = fb.rank_from_naver("amount", "J", top_n=1)[0]
    assert r["hts_kor_isnm"] == "다" and r["stck_prpr"] == "3000" and r["prdy_ctrt"] == "1.0"
    assert r["acml_tr_pbmn"] == str(int(30 * 1e8)) and r["mksc_shrn_iscd"] == "000003"


def test_failure_returns_empty_not_exception(monkeypatch):
    monkeypatch.setattr(fb, "_universe_rows", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fb.rank_from_naver("volume", "J") == []
    assert fb.rank_from_naver("nope", "J") == []


def test_kis_methods_fall_back_only_when_kis_empty(monkeypatch):
    _fb(monkeypatch)
    kis = KISClient.__new__(KISClient)
    monkeypatch.setattr(KISClient, "_rank", lambda self, *a, **k: [])
    assert [r["stck_shrn_iscd"] for r in kis.get_fluctuation_rank("J", rise=False, top_n=1)] == ["000002"]
    assert kis.get_volume_rank("J", top_n=2)[0]["source"] == "naver"
    assert kis.get_amount_rank("Q")[0]["stck_shrn_iscd"] == "100001"
    monkeypatch.setattr(KISClient, "_rank", lambda self, *a, **k: [{"stck_shrn_iscd": "KIS"}])
    assert kis.get_volume_rank("J") == [{"stck_shrn_iscd": "KIS"}]            # KIS가 되면 KIS 값 그대로


def test_foreign_institution_ranks_have_no_fallback(monkeypatch):
    _fb(monkeypatch)
    kis = KISClient.__new__(KISClient)
    monkeypatch.setattr(KISClient, "_rank", lambda self, *a, **k: [])
    assert kis.get_foreign_buy_rank("J") == [] and kis.get_institution_buy_rank("J") == []


# ── 외국인·기관 순매수 대체 (2026-10-29) ──────────────────────────────

def _flows(monkeypatch, table):
    monkeypatch.setattr(fb, "_latest_flow", lambda code: table.get(code))


def test_investor_rank_orders_by_net_value_and_uses_kis_fields(monkeypatch):
    _fb(monkeypatch)
    _flows(monkeypatch, {
        "000001": {"foreign": 100, "institution": -5, "bizdate": "20261028"},   # 금액 100,000
        "000002": {"foreign": 60, "institution": 10, "bizdate": "20261028"},    # 금액 120,000
        "000003": {"foreign": -9, "institution": 20, "bizdate": "20261028"},    # 순매도 → 제외
    })
    res = fb.rank_investor_from_naver("foreign", "J")
    assert [r["mksc_shrn_iscd"] for r in res] == ["000002", "000001"]
    assert res[0]["frgn_ntby_qty"] == "60" and "naver" in res[0]["source"]
    inst = fb.rank_investor_from_naver("institution", "J")
    assert [r["mksc_shrn_iscd"] for r in inst] == ["000003", "000002"] and inst[0]["inst_ntby_qty"] == "20"


def test_investor_rank_empty_when_most_lookups_fail(monkeypatch):
    _fb(monkeypatch)
    _flows(monkeypatch, {"000001": {"foreign": 100, "institution": 1, "bizdate": "x"}})  # 3개 중 1개만 성공
    assert fb.rank_investor_from_naver("foreign", "J") == []


def test_investor_rank_bad_input_returns_empty(monkeypatch):
    _fb(monkeypatch)
    assert fb.rank_investor_from_naver("retail", "J") == []
    assert fb.rank_investor_from_naver("foreign", "X") == []


def test_kis_investor_methods_fall_back_only_when_kis_empty(monkeypatch):
    _fb(monkeypatch)
    _flows(monkeypatch, {c: {"foreign": 5, "institution": 5, "bizdate": "x"} for c in ("000001", "000002", "000003")})
    k = KISClient()
    monkeypatch.setattr(KISClient, "_rank", lambda self, *a, **kw: [])
    assert k.get_foreign_buy_rank("J") and k.get_institution_buy_rank("J")
    monkeypatch.setattr(KISClient, "_rank", lambda self, *a, **kw: [{"mksc_shrn_iscd": "KIS"}])
    assert k.get_foreign_buy_rank("J") == [{"mksc_shrn_iscd": "KIS"}]
