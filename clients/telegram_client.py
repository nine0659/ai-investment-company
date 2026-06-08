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


def send_message_with_buttons(
    text: str,
    buttons: list[list[dict]],
    chat_id: str | None = None,
) -> bool:
    """인라인 키보드 버튼과 함께 메시지를 발송한다.

    Args:
        text: 메시지 본문
        buttons: [[{"text": "버튼명", "callback_data": "data"}, ...], ...] 형식의 2D 배열
        chat_id: 대상 chat_id (None이면 기본값 사용)

    Returns:
        발송 성공 여부
    """
    cid = chat_id or TELEGRAM_CHAT_ID
    url = f"{_BASE}/sendMessage"
    keyboard = {"inline_keyboard": buttons}

    for attempt in range(3):
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": cid,
                    "text": text[:4096],
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard,
                },
                timeout=10,
            )
            if r.ok:
                return True
            if r.status_code == 400:
                # Markdown 파싱 오류 → plain text 재시도
                r2 = requests.post(
                    url,
                    json={
                        "chat_id": cid,
                        "text": text[:4096],
                        "reply_markup": keyboard,
                    },
                    timeout=10,
                )
                return r2.ok
            if r.status_code == 429:
                time.sleep(2 ** attempt)
            else:
                time.sleep(1)
        except Exception as e:
            logger.warning("Telegram 버튼 메시지 전송 오류 (시도 %d/3): %s", attempt + 1, e)
            time.sleep(1)
    logger.error("Telegram 버튼 메시지 전송 최종 실패")
    return False


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """인라인 버튼 클릭 응답 (로딩 스피너 제거용)."""
    try:
        requests.post(
            f"{_BASE}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text, "show_alert": False},
            timeout=5,
        )
    except Exception as e:
        logger.debug("answerCallbackQuery 실패: %s", e)


def _split(text: str) -> list[str]:
    if len(text) <= _MAX_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= _MAX_LEN:
            chunks.append(text)
            break
        # 개행 기준으로 분할 → 마크다운 포맷 보존
        split_at = text.rfind('\n', 0, _MAX_LEN)
        if split_at <= 0:
            split_at = _MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    return chunks
