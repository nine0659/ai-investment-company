"""
clients/kakao_client.py
카카오톡 나에게 보내기 클라이언트

[토큰 관리]
- 액세스 토큰: 6시간 유효 (자동 갱신)
- 리프레시 토큰: 60일 유효 (만료 시 재발급 필요 → kakao_setup.py 재실행)
- 토큰 저장: data/kakao_tokens.json

[필요 환경변수]
- KAKAO_REST_API_KEY: 카카오 개발자 콘솔의 REST API 키
  (발급: https://developers.kakao.com)
"""
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 토큰 저장 경로
_TOKEN_FILE = Path(__file__).parent.parent / "data" / "kakao_tokens.json"
_TOKEN_URL  = "https://kauth.kakao.com/oauth/token"
_SEND_URL   = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
_REDIRECT   = "http://localhost:5000/callback"


# ── 토큰 파일 읽기/쓰기 ────────────────────────────────────────────

def _load_tokens() -> dict:
    """저장된 토큰 로드. 없으면 환경변수에서 시도."""
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 환경변수 폴백 (구버전 호환)
    access  = os.getenv("KAKAO_ACCESS_TOKEN", "")
    refresh = os.getenv("KAKAO_REFRESH_TOKEN", "")
    if access:
        return {"access_token": access, "refresh_token": refresh, "expires_at": 0}
    return {}


def _save_tokens(tokens: dict) -> None:
    """토큰을 파일에 저장."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("카카오 토큰 저장 완료: %s", _TOKEN_FILE)


# ── 토큰 갱신 ─────────────────────────────────────────────────────

def _refresh_access_token(rest_api_key: str, refresh_token: str) -> dict | None:
    """리프레시 토큰으로 액세스 토큰 갱신."""
    try:
        data = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "client_id":     rest_api_key,
            "refresh_token": refresh_token,
        }).encode("utf-8")
        req = urllib.request.Request(
            _TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if "access_token" in result:
                logger.info("카카오 액세스 토큰 갱신 완료")
                return result
            logger.warning("토큰 갱신 실패: %s", result)
    except Exception as e:
        logger.error("토큰 갱신 오류: %s", e)
    return None


def get_valid_access_token() -> str | None:
    """유효한 액세스 토큰 반환. 만료 시 자동 갱신."""
    tokens = _load_tokens()
    if not tokens:
        logger.warning("카카오 토큰 없음 — scripts/kakao_setup.py 를 먼저 실행하세요")
        return None

    rest_key     = os.getenv("KAKAO_REST_API_KEY", tokens.get("rest_api_key", ""))
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_at   = tokens.get("expires_at", 0)

    # 만료 300초(5분) 전부터 갱신
    now = time.time()
    if now < expires_at - 300:
        return access_token  # 아직 유효

    # 갱신 시도
    if not refresh_token or not rest_key:
        logger.warning("리프레시 토큰 또는 REST API 키 없음 — kakao_setup.py 재실행 필요")
        return access_token  # 마지막으로 보관된 토큰 그대로 반환

    new_tokens = _refresh_access_token(rest_key, refresh_token)
    if new_tokens:
        tokens["access_token"] = new_tokens["access_token"]
        tokens["expires_at"]   = now + new_tokens.get("expires_in", 21600)
        # 리프레시 토큰도 새로 발급된 경우 갱신
        if "refresh_token" in new_tokens:
            tokens["refresh_token"] = new_tokens["refresh_token"]
        _save_tokens(tokens)
        return tokens["access_token"]

    logger.warning("토큰 갱신 실패 — 기존 토큰으로 재시도")
    return access_token


# ── 메시지 발송 ───────────────────────────────────────────────────

def send_message(text: str, button_title: str = "확인") -> bool:
    """카카오톡 나에게 보내기.

    Returns:
        True  - 발송 성공
        False - 토큰 없음 또는 발송 실패
    """
    access_token = get_valid_access_token()
    if not access_token:
        return False

    payload = {
        "object_type": "text",
        "text": text[:2000],  # 카카오 최대 2000자
        "link": {"web_url": "", "mobile_web_url": ""},
        "button_title": button_title,
    }

    try:
        data = urllib.parse.urlencode({
            "template_object": json.dumps(payload, ensure_ascii=False)
        }).encode("utf-8")
        req = urllib.request.Request(
            _SEND_URL, data=data,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("result_code") == 0:
                logger.info("카카오톡 발송 성공")
                return True
            # -401: 토큰 만료 → 갱신 후 1회 재시도
            if result.get("code") in (-401, -402):
                logger.warning("카카오 토큰 만료 감지 — 강제 갱신 시도")
                tokens = _load_tokens()
                rest_key = os.getenv("KAKAO_REST_API_KEY", tokens.get("rest_api_key", ""))
                new_tok = _refresh_access_token(rest_key, tokens.get("refresh_token", ""))
                if new_tok:
                    tokens["access_token"] = new_tok["access_token"]
                    tokens["expires_at"]   = time.time() + new_tok.get("expires_in", 21600)
                    _save_tokens(tokens)
                    return send_message(text, button_title)  # 1회 재귀 재시도
            logger.warning("카카오톡 발송 실패: %s", result)
            return False
    except Exception as e:
        logger.error("카카오톡 발송 오류: %s", e)
        return False


def is_configured() -> bool:
    """카카오톡 발송 설정 완료 여부 확인."""
    tokens = _load_tokens()
    return bool(tokens.get("access_token"))
