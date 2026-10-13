from services import decision_outcome_service as dos
from services.risk_gate import check_portfolio_rules


def _item(i, status="target_hit", ret=10.0, alpha=4.0, adopted=False, conviction=""):
    return {"rec_id": i, "code": f"00{i}", "name": f"종목{i}", "rec_date": f"2026-09-0{1 + i % 4}",
            "conviction": conviction,
            "last_date": "2026-09-30", "return_pct": ret, "days_held": 20, "status": status,
            "adopted": adopted, "kospi_return_pct": 6.0, "alpha_pct": alpha}


def test_prompt_is_empty_below_sample_gate(monkeypatch):
    monkeypatch.setattr(dos, "get_outcomes", lambda: [_item(i) for i in range(7)])
    assert dos.format_outcomes_for_prompt() == ""


def test_tracking_items_do_not_count_as_settled(monkeypatch):
    monkeypatch.setattr(dos, "get_outcomes", lambda: [_item(i, status="tracking") for i in range(9)])
    assert dos.format_outcomes_for_prompt() == ""


def test_prompt_labels_basis_and_computes_stats(monkeypatch):
    items = [_item(i, adopted=(i == 0)) for i in range(8)]
    items[1].update(return_pct=-8.0, alpha=-14.0, status="stop_hit")
    monkeypatch.setattr(dos, "get_outcomes", lambda: items)
    out = dos.format_outcomes_for_prompt()
    assert "적중률 88%" in out and "내 체결가 기준" in out and "가상 추적(내 매매 아님)" in out
    assert "손절가 도달" in out and "새 수치를 만들지 마라" in out


def test_alpha_na_when_kospi_missing(monkeypatch):
    monkeypatch.setattr(dos, "get_outcomes", lambda: [_item(i, alpha=None) for i in range(8)])
    assert "알파 N/A" in dos.format_outcomes_for_prompt()


_D = {"cash_target_pct": 30, "new_positions": [
    {"code": "111111", "name": "밴드밖", "size_pct": 9.0, "conviction": "medium"},
    {"code": "222222", "name": "이미보유", "size_pct": 4.0, "conviction": "medium"},
    {"code": "333333", "name": "정상", "size_pct": 4.0, "conviction": "medium"}]}


def test_blocks_band_violation_and_duplicate_holding():
    warns, blocked = check_portfolio_rules(_D, {"222222"})
    assert blocked == {"111111", "222222"}
    assert any("이미 보유" in w for w in warns)


def test_total_exceeds_room_warns_without_blocking():
    d = {"cash_target_pct": 95, "new_positions": [
        {"code": "1", "name": "a", "size_pct": 4.0, "conviction": "medium"},
        {"code": "2", "name": "b", "size_pct": 4.0, "conviction": "medium"}]}
    warns, blocked = check_portfolio_rules(d, set())
    assert not blocked and any("한도" in w for w in warns)


def test_clean_decisions_are_silent():
    assert check_portfolio_rules({"cash_target_pct": 30, "new_positions": []}, set()) == ([], set())
    assert check_portfolio_rules({}, None) == ([], set())


def test_same_day_recommendations_do_not_open_the_gate(monkeypatch):
    items = [dict(_item(i), rec_date="2026-08-30") for i in range(9)]
    monkeypatch.setattr(dos, "get_outcomes", lambda: items)
    assert dos.format_outcomes_for_prompt() == ""


def test_calibration_flags_overconfidence(monkeypatch):
    items = [_item(i, conviction="high", ret=-5.0, alpha=-8.0, status="stop_hit") for i in range(3)]
    items += [_item(i + 3, conviction="medium") for i in range(5)]
    monkeypatch.setattr(dos, "get_outcomes", lambda: items)
    out = dos.format_outcomes_for_prompt()
    assert "확신도별 실제 적중: high 0%(0/3" in out and "medium 100%" in out
    assert "과신일 수 있으니" in out


def test_calibration_skips_small_buckets():
    cal = dos.calibration_by_conviction([_item(1, conviction="high"), _item(2, conviction="high")])
    assert cal == {}


def test_get_outcomes_falls_back_when_conviction_column_missing(monkeypatch):
    """배포 직후 마이그레이션 전에도 조용히 빈 결과가 되면 안 된다."""
    calls = []

    class _Res:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows

    class _Conn:
        def execute(self, stmt, *a, **k):
            sql = str(stmt)
            calls.append("conviction" in sql)
            if "sr.conviction" in sql:
                raise RuntimeError("column sr.conviction does not exist")
            return _Res([(1, "005930", "삼성전자", "2026-09-01", "2026-09-10", 5.0, 9, "target_hit", None, None)])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(dos, "get_conn", lambda: _Conn())
    monkeypatch.setattr(dos, "_kospi_return", lambda a, b: 2.0)
    out = dos.get_outcomes()
    assert len(out) == 1 and out[0]["alpha_pct"] == 3.0 and out[0]["conviction"] == ""
    assert calls == [True, False]
