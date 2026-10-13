import services.memory_watch as mw


def test_returns_none_when_not_measurable(monkeypatch):
    monkeypatch.setattr(mw.os.path, "isdir", lambda p: False)
    assert mw.container_rss_mb() is None
    assert mw.after_job("x") is None   # 예외 없이 조용히


def test_alert_when_high_and_cooldown(monkeypatch):
    sent = []
    import clients.telegram_client as tc
    monkeypatch.setattr(tc, "send_error_alert", lambda m: sent.append(m))
    monkeypatch.setattr(mw, "container_rss_mb", lambda: 450.0)
    monkeypatch.setattr(mw, "trim", lambda: None)
    monkeypatch.setattr(mw, "_last_alert_ts", 0.0)
    mw.after_job("pre_market")
    mw.after_job("market_monitor")      # 쿨다운 안 → 재경보 없음
    assert len(sent) == 1 and "450MB" in sent[0]


def test_no_alert_when_low(monkeypatch):
    sent = []
    import clients.telegram_client as tc
    monkeypatch.setattr(tc, "send_error_alert", lambda m: sent.append(m))
    monkeypatch.setattr(mw, "container_rss_mb", lambda: 300.0)
    monkeypatch.setattr(mw, "trim", lambda: None)
    monkeypatch.setattr(mw, "_last_alert_ts", 0.0)
    assert mw.after_job("x") == 300.0 and sent == []


def test_scheduler_registers_listener():
    import scheduler as s
    assert any(l[0] is s._on_job_finished for l in s.scheduler._listeners)
