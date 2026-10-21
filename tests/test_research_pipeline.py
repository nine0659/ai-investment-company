"""리서치 인텔리전스(본문 분석·검증·집계) 회귀 테스트 (2026-10-21).

핵심 원칙: LLM이 뽑은 인용·수치는 코드가 원문과 대조해 통과한 것만 쓴다 /
네이버 API의 prevGoalPrice는 '이전 목표가'가 아니라 작성일 주가다 /
증권사 의견 집계는 증권사당 1표, 사실 서술은 투표에서 제외.
"""
import json

from services import report_analyzer as ra
from services import research_synthesis as rs

SRC = ("20일 코스피는 AI 수익성 우려로 급락했다. 증권사는 연말 코스피 목표를 3,200pt로 제시했다. "
       "원/달러 환율 하락이 수출업체 4분기 실적에 부정적일 전망이다.")


# ── 원문 대조 검증 ─────────────────────────────────────────────────

def test_claim_with_fabricated_quote_is_dropped():
    raw = {"stance": "강세", "claims": [
        {"claim": "진짜", "quote": "원/달러 환율 하락이 수출업체 4분기 실적에 부정적일 전망이다", "theme": "환율·달러", "kind": "전망", "stance": "약세"},
        {"claim": "가짜", "quote": "코스피는 내년 5,000을 돌파할 것이다 라고 단언했다", "theme": "금리·통화정책", "kind": "전망", "stance": "강세"},
    ]}
    out = ra.verify_analysis(raw, SRC)
    assert [c["claim"] for c in out["claims"]] == ["진짜"]


def test_quote_matching_ignores_whitespace():
    raw = {"claims": [{"claim": "c", "quote": "원/달러환율하락이수출업체4분기실적에부정적일전망이다", "theme": "환율·달러", "kind": "전망", "stance": "약세"}]}
    assert len(ra.verify_analysis(raw, SRC)["claims"]) == 1


def test_figure_not_in_source_is_dropped():
    raw = {"claims": [], "figures": [
        {"label": "연말 코스피 목표", "value": "3,200pt", "quote": "연말 코스피 목표를 3,200pt로 제시", "kind": "전망치"},
        {"label": "환각 수치", "value": "9,999pt", "quote": "", "kind": "전망치"},
    ]}
    figs = ra.verify_analysis(raw, SRC)["figures"]
    assert [f["value"] for f in figs] == ["3,200pt"]


def test_stance_demoted_when_no_verified_claim():
    out = ra.verify_analysis({"stance": "강세", "claims": [{"claim": "x", "quote": "원문에 없는 문장입니다 정말로", "theme": "금리·통화정책", "kind": "전망", "stance": "강세"}]}, SRC)
    assert out["stance"] == "판단불가"


def test_invalid_theme_and_enums_are_sanitized():
    raw = {"stance": "초강세", "claims": [{"claim": "c", "quote": "AI 수익성 우려로 급락했다", "theme": "없는테마", "kind": "아무거나", "stance": "강세"}]}
    out = ra.verify_analysis(raw, SRC)
    assert out["stance"] == "판단불가"
    assert out["claims"][0]["theme"] == "" and out["claims"][0]["kind"] == "사실" and out["claims"][0]["stance"] == ""


def test_parse_json_object_tolerates_wrapping_text():
    assert ra.parse_json_object('설명 {"a": 1} 끝') == {"a": 1}
    assert ra.parse_json_object("json 아님") is None
    assert ra.parse_json_object('{"a": ') is None


# ── 선택 ───────────────────────────────────────────────────────────

def _doc(t, firm, link, date="2026-10-21"):
    return {"type": t, "firm": firm, "link": link, "date": date}


def test_select_pending_respects_caps_and_cache_and_per_broker():
    docs = [_doc("경제분석", "A", f"l{i}") for i in range(4)] + [_doc("경제분석", "B", "lb")]
    picked = ra.select_pending(docs, analyzed_links={"lb"}, caps={"경제분석": 5}, per_broker=2)
    links = [d["link"] for d in picked]
    assert "lb" not in links            # 이미 읽은 것은 제외
    assert len(links) == 2              # 같은 증권사는 2건까지


def test_select_pending_ignores_unknown_types():
    assert ra.select_pending([_doc("채권분석", "A", "x")], set()) == []


# ── 집계 ───────────────────────────────────────────────────────────

def _row(firm, date, claims):
    return {"firm": firm, "report_date": date, "analysis": {"claims": claims}}


def _c(theme, stance, kind="전망"):
    return {"claim": f"{theme}{stance}", "quote": "q" * 10, "theme": theme, "kind": kind, "stance": stance}


def test_one_vote_per_firm_per_theme_latest_wins():
    rows = [_row("A", "2026-10-18", [_c("환율·달러", "강세")]),
            _row("A", "2026-10-20", [_c("환율·달러", "약세")]),
            _row("B", "2026-10-20", [_c("환율·달러", "약세")])]
    agg = rs.aggregate_macro(rows)
    assert agg["환율·달러"]["stances"] == {"약세": 2}


def test_facts_and_other_theme_do_not_vote():
    rows = [_row("A", "2026-10-20", [_c("환율·달러", "", kind="사실"), _c("기타", "강세")])]
    assert rs.aggregate_macro(rows) == {}


def test_crowding_is_flagged_not_celebrated():
    assert "쏠림" in rs.classify_consensus({"강세": 4, "중립": 1})
    assert rs.classify_consensus({"강세": 2, "약세": 2}) == "엇갈림"
    assert rs.classify_consensus({"강세": 1}) == "소수 의견"


def test_forecast_figure_decided_by_code_not_llm_label():
    assert rs.is_forecast_figure({"label": "연말 코스피 전망", "quote": "", "kind": "전망치"})
    assert not rs.is_forecast_figure({"label": "코스피 종가", "quote": "5,552", "kind": "전망치"})   # LLM이 잘못 라벨링
    assert not rs.is_forecast_figure({"label": "연말 전망", "quote": "", "kind": "실적치"})


# ── 종목 커버리지: prevGoalPrice 함정 ──────────────────────────────

def test_target_change_uses_same_firm_previous_report_only():
    reps = [
        {"firm": "A", "report_date": "2026-09-20", "opinion": "Buy", "goal_price": 100, "price_at_write": 50},
        {"firm": "A", "report_date": "2026-10-10", "opinion": "Buy", "goal_price": 120, "price_at_write": 60},
        {"firm": "B", "report_date": "2026-10-11", "opinion": "매수", "goal_price": 90, "price_at_write": 60},   # 이전 리포트 없음 → 변경 판정 안 함
    ]
    st = rs.target_price_stats(reps)
    assert (st["ups"], st["downs"], st["same"]) == (1, 0, 0)
    assert st["n_firms"] == 2 and st["goal_median"] == 105
    assert st["opinions"] == {"매수": 2}


def test_opinion_normalization():
    assert rs.normalize_opinion("StrongBuy") == "매수"
    assert rs.normalize_opinion("Hold") == "중립"
    assert rs.normalize_opinion("없음") == ""


def test_client_does_not_use_prev_goal_price():
    import clients.securities_report_client as c
    import inspect
    src = inspect.getsource(c.fetch_report_detail)
    assert "priceAtWriteDate" in src
    assert "prev_goal_price" not in src


# ── 저장/조회 왕복 ─────────────────────────────────────────────────

def test_save_and_synthesis_roundtrip(monkeypatch):
    from db.database import init_db
    init_db()
    doc = {"type": "경제분석", "firm": "TESTFIRM_RT", "link": "https://t/rt1", "title": "t", "date": "2099-01-01"}
    analysis = {"stance": "약세", "policy_tone": "해당없음", "themes": ["환율·달러"],
                "claims": [_c("환율·달러", "약세")], "figures": [], "risks": [], "horizon": "단기", "n_claims_dropped": 0}
    ra.save(doc, {}, analysis, 500)
    assert "https://t/rt1" in ra.analyzed_links()
    monkeypatch.setattr(rs, "holdings", lambda: [])
    ctx = rs.build_research_context(macro_days=36500)
    assert "TESTFIRM_RT" in ctx and "[AI 요약]" in ctx
    row = json.dumps(analysis)  # 직렬화 가능성
    assert row


def test_pipeline_never_raises_and_records_failure(monkeypatch):
    calls = []
    import services.job_ledger as jl
    monkeypatch.setattr(jl, "record_job", lambda n, s, d="": calls.append((n, s)))
    import clients.official_docs_client as oc
    monkeypatch.setattr(oc, "list_official_documents", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    from services import research_pipeline
    assert research_pipeline.run([]) == ""
    assert ("branch:report_analysis", "fail") in calls
