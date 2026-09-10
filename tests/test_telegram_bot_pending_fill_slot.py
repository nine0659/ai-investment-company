"""clients/telegram_bot.py _PENDING_FILL 슬롯 덮어쓰기 회귀 테스트 (2026-09-10).

_PENDING_FILL은 chat_id 하나당 슬롯이 1개뿐이다. 사용자가 종목 A 승인
버튼("napp")을 누른 뒤 체결가를 답장하기 전에 종목 B 승인 버튼도 누르면,
그 슬롯이 조용히 B로 덮어써져 A의 승인이 유실된다(A는 draft 상태로 영원히
남고, 사용자가 "수량 가격"으로 보낸 답장은 엉뚱하게 B에 적용된다). 다른
종목이 이미 대기 중이면 덮어쓰지 않고 경고하도록 고쳤다 — 같은 종목 재클릭
(새로고침)은 그대로 허용한다.
"""
import clients.telegram_bot as telegram_bot


def setup_function(_):
    telegram_bot._PENDING_FILL.clear()


def test_second_napp_for_different_code_does_not_overwrite_pending_slot(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda chat_id, text: sent.append(text))
    monkeypatch.setattr("clients.telegram_client.answer_callback_query", lambda *a, **k: None)

    import db.database as database
    monkeypatch.setattr(database, "get_conn", lambda: _NoRowConn())

    telegram_bot._handle_callback("chat1", "napp:005930:2026-09-10")
    assert telegram_bot._PENDING_FILL["chat1"]["code"] == "005930"

    telegram_bot._handle_callback("chat1", "napp:000660:2026-09-10")

    assert telegram_bot._PENDING_FILL["chat1"]["code"] == "005930", (
        "다른 종목 승인이 대기 중인 슬롯을 덮어씀 — 첫 종목의 승인이 유실될 수 있다"
    )
    assert any("이미" in s and "005930" in s for s in sent[-1:]), "덮어쓰기 차단 경고 메시지가 발송되지 않음"


def test_second_napp_for_same_code_refreshes_slot(monkeypatch):
    """같은 종목을 다시 누르는 건 새로고침이라 그대로 허용해야 한다."""
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda chat_id, text: sent.append(text))
    monkeypatch.setattr("clients.telegram_client.answer_callback_query", lambda *a, **k: None)

    import db.database as database
    monkeypatch.setattr(database, "get_conn", lambda: _NoRowConn())

    telegram_bot._handle_callback("chat1", "napp:005930:2026-09-10")
    telegram_bot._handle_callback("chat1", "napp:005930:2026-09-11")

    assert telegram_bot._PENDING_FILL["chat1"]["code"] == "005930"
    assert telegram_bot._PENDING_FILL["chat1"]["date"] == "2026-09-11"


class _NoRowConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _R:
            def fetchone(self):
                return None
        return _R()
