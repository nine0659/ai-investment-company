"""
scripts/kakao_save_token.py
implicit flow로 받은 URL에서 토큰 추출 후 저장 + 테스트 발송

사용법:
  python scripts/kakao_save_token.py "http://localhost:5000/callback#access_token=..."
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT       = Path(__file__).parent.parent
_TOKEN_FILE = _ROOT / "data" / "kakao_tokens.json"
_SEND_URL   = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
JS_KEY      = "8a51d2753d2ca59edccf9377b83e7d4e"


def parse_fragment(url: str) -> dict:
    """URL 프래그먼트(#)에서 토큰 파싱."""
    parsed = urllib.parse.urlparse(url)
    fragment = parsed.fragment
    if not fragment and "access_token=" in url:
        # # 없이 직접 붙여넣은 경우
        fragment = url.split("#", 1)[-1] if "#" in url else url.split("?", 1)[-1]
    params = urllib.parse.parse_qs(fragment)
    return {k: v[0] for k, v in params.items()}


def save_tokens(tokens_dict: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "rest_api_key":           "5ef7e2d4074ee81b1c76084c757f3045",
        "js_key":                 JS_KEY,
        "access_token":           tokens_dict.get("access_token", ""),
        "refresh_token":          tokens_dict.get("refresh_token", ""),
        "expires_at":             time.time() + int(tokens_dict.get("expires_in", 21600)),
        "refresh_token_expires_at": time.time() + int(tokens_dict.get("refresh_token_expires_in", 5184000)),
        "issued_at":              time.strftime("%Y-%m-%d %H:%M:%S"),
        "flow":                   "implicit",
    }
    _TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"토큰 저장 완료: {_TOKEN_FILE}")


def test_send(access_token: str) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    payload = {
        "object_type": "text",
        "text": (
            "AI 투자 어시스턴트\n"
            "카카오톡 긴급 알림 연동 완료!\n\n"
            "앞으로 이런 상황에서 즉시 알림:\n"
            "  [기회] 관심종목 복수 매수 신호 발생\n"
            "  [위험] KOSPI 급락 / 지정학 충격 / 보유종목 급락\n\n"
            f"연결 시각: {now}"
        ),
        "link": {"web_url": "", "mobile_web_url": ""},
        "button_title": "확인",
    }
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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("result_code") == 0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"발송 실패 HTTP {e.code}: {body}")
        return False
    except Exception as e:
        print(f"발송 오류: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        print("사용법: python scripts/kakao_save_token.py ACCESS_TOKEN [REFRESH_TOKEN]")
        sys.exit(1)

    arg1 = sys.argv[1].strip().strip('"')

    # URL 형식으로 붙여넣은 경우 (# 포함)
    if arg1.startswith("http") or "#" in arg1:
        tokens = parse_fragment(arg1)
        access_token  = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = int(tokens.get("expires_in", 21600))
        refresh_expires = int(tokens.get("refresh_token_expires_in", 5184000))
    else:
        # 직접 토큰 값만 넘긴 경우 (HTML 페이지에서 복사한 명령어)
        access_token    = arg1
        refresh_token   = sys.argv[2].strip().strip('"') if len(sys.argv) > 2 else ""
        expires_in      = 21600
        refresh_expires = 5184000

    if not access_token:
        print("access_token을 찾을 수 없습니다.")
        sys.exit(1)

    print(f"access_token : {access_token[:16]}...")
    print(f"refresh_token: {'있음 ' + refresh_token[:12] + '...' if refresh_token else '없음'}")

    token_dict = {
        "access_token":           access_token,
        "refresh_token":          refresh_token,
        "expires_in":             str(expires_in),
        "refresh_token_expires_in": str(refresh_expires),
    }
    save_tokens(token_dict)

    print("\n테스트 메시지 발송 중...")
    ok = test_send(access_token)
    if ok:
        print("\n성공! 카카오톡을 확인하세요.")
        print("\n설정 완료! 앞으로 긴급 알림이 카카오톡으로 발송됩니다.")
    else:
        print("\n발송 실패 — 동의항목을 확인하세요:")
        print("  개발자 콘솔 → 카카오 로그인 → 동의항목")
        print("  → '카카오톡 메시지 전송' → [설정] → 이용 중 동의")


if __name__ == "__main__":
    import urllib.error
    main()
