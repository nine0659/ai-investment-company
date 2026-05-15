import logging
import time
import requests
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_MAX_LEN = 4096


def send_message(text: str, chat_id: str | None = None) -> bool:
    cid = chat_id or TELEGRAM_CHAT_ID
    url = f"{_BASE}/sendMessage"
    all_ok = True
    for chunk in _split(text):
        sent = False
        for attempt in range(3):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": cid, "text": chunk, "parse_mode": "Markdown"},
                    timeout=10,
                )
                if r.ok:
                    sent = True
                    break
                if r.status_code == 400:
                    # Markdown 파싱 오류 → plain text 즉시 재시도
                    r2 = requests.post(
                        url, json={"chat_id": cid, "text": chunk}, timeout=10
                    )
                    if r2.ok:
                        sent = True
                    break
                if r.status_code == 429:
                    # Rate limit → 지수 백오프
                    time.sleep(2 ** attempt)
                else:
                    time.sleep(1)
            except Exception as e:
                logger.warning("Telegram 전송 오류 (시도 %d/3): %s", attempt + 1, e)
                time.sleep(1)
        if not sent:
            all_ok = False
            logger.error("Telegram 청크 전송 최종 실패 (%d자)", len(chunk))
    return all_ok


def send_error_alert(text: str) -> bool:
    return send_message(f"🚨 *오류 알림*\n{text}")


def _split(text: str) -> list[str]:
    if len(text) <= _MAX_LEN:
        return [text]
    return [text[i:i + _MAX_LEN] for i in range(0, len(text), _MAX_LEN)]
