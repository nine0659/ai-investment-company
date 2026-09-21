"""텔레그램 /holdings·/portfolio에 웹 대시보드 버튼이 붙는지 회귀 테스트 (2026-09-21).

/holdings remove 명령어는 전량매도·평단가 기준(0% 수익률)으로만 기록되고
부분매도·실제 매도가를 받을 방법이 없었다 — /api/portfolio/close는 이미
exit_price·partial_qty를 지원하는데 텔레그램 명령어만 못 받았다. 새 명령어
문법을 만드는 대신 이미 있는 웹 폼으로 보내는 버튼을 붙였다.
"""
import clients.telegram_bot as telegram_bot


def test_send_with_web_button_uses_buttons_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "clients.telegram_client.send_message_with_buttons",
        lambda text, buttons, chat_id: calls.append((text, buttons, chat_id)) or True,
    )
    telegram_bot._send_with_web_button("chat1", "hello")

    assert len(calls) == 1
    text, buttons, chat_id = calls[0]
    assert text == "hello"
    assert chat_id == "chat1"
    assert buttons[0][0]["url"] == telegram_bot._WEB_BASE_URL


def test_send_with_web_button_falls_back_to_plain_send_on_failure(monkeypatch):
    monkeypatch.setattr(
        "clients.telegram_client.send_message_with_buttons", lambda *a, **k: False
    )
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda chat_id, text: sent.append(text))

    telegram_bot._send_with_web_button("chat1", "hello")

    assert sent == ["hello"]


def test_cmd_portfolio_sends_with_web_button(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_bot, "_send_with_web_button",
        lambda chat_id, text: calls.append(text),
    )
    monkeypatch.setattr(
        "services.portfolio_service.format_portfolio_for_briefing",
        lambda kis=None: "포트폴리오 텍스트",
    )

    telegram_bot._cmd_portfolio("chat1", "")

    assert calls == ["포트폴리오 텍스트"]


def test_cmd_holdings_remove_result_sends_with_web_button(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_bot, "_send_with_web_button",
        lambda chat_id, text: calls.append(text),
    )
    monkeypatch.setattr(
        "services.portfolio_service.close_position",
        lambda code: {"code": code},
    )

    telegram_bot._cmd_holdings("chat1", "remove 005930")

    assert len(calls) == 1
    assert "005930" in calls[0]


def test_cmd_holdings_remove_usage_hint_mentions_web_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_bot, "_send_with_web_button",
        lambda chat_id, text: calls.append(text),
    )

    telegram_bot._cmd_holdings("chat1", "remove")  # 코드 누락 → 사용법 안내

    assert len(calls) == 1
    assert "웹" in calls[0]
