import logging
import requests
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_MAX_LEN = 4096


def send_message(text: str, chat_id: str | None = None) -> bool:
    cid = chat_id or TELEGRAM_CHAT_ID
    url = f"{_BASE}/sendMessage"
    for chunk in _split(text):
        try:
            r = requests.post(url, json={"chat_id": cid, "text": chunk, "parse_mode": "Markdown"}, timeout=10)
            if not r.ok:
                # Retry without parse_mode (Markdown syntax error 방지)
                requests.post(url, json={"chat_id": cid, "text": chunk}, timeout=10)
        except Exception as e:
            logger.error("Telegram 전송 실패: %s", e)
            return False
    return True


def send_error_alert(text: str) -> bool:
    return send_message(f"🚨 *오류 알림*\n{text}")


def _split(text: str) -> list[str]:
    if len(text) <= _MAX_LEN:
        return [text]
    return [text[i:i + _MAX_LEN] for i in range(0, len(text), _MAX_LEN)]
