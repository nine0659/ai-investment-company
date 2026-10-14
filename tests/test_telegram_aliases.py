"""한글 명령 별칭 — 안전 규칙(주문 계열은 슬래시 필수, 일반 대화는 건드리지 않음) 회귀 테스트."""
from clients.telegram_aliases import ALIASES, MENU_KEYBOARD, SLASH_ONLY, resolve


def test_korean_with_and_without_slash():
    assert resolve("/위험") == "/risk"
    assert resolve("위험") == "/risk"
    assert resolve("일정") == "/calendar"
    assert resolve("/위험@my_bot") == "/risk"


def test_args_preserved():
    assert resolve("만약 SK하이닉스 20") == "/whatif SK하이닉스 20"
    assert resolve("/결정 000660 무시 반등 기다림") == "/did 000660 무시 반등 기다림"


def test_order_commands_require_slash():
    for w in SLASH_ONLY:
        assert resolve(w) == w                       # 슬래시 없으면 그대로(AI 대화로 감)
    assert resolve("매수할까?") == "매수할까?"
    assert resolve("/매수 005930 1") == "/buy 005930 1"
    assert resolve("/중지") == "/pause"


def test_normal_chat_untouched():
    for t in ("위험이 뭐야?", "오늘 시장 어때", "삼성전자 지금 살만해?", "10 71500", "/risk", "/help"):
        assert resolve(t) == t


def test_menu_buttons_all_resolve_to_known_commands():
    from clients.telegram_bot import _HANDLERS
    for row in MENU_KEYBOARD:
        for b in row:
            assert b not in SLASH_ONLY
            assert resolve(b) in _HANDLERS, b


def test_every_alias_target_has_a_handler():
    from clients.telegram_bot import _HANDLERS
    missing = [w for w, c in ALIASES.items() if c not in _HANDLERS]
    assert missing == []
