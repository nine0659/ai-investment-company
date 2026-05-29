"""
scripts/kakao_exchange.py
인가 코드 → 토큰 교환 (수동 방식)

사용법:
  python scripts/kakao_exchange.py <인가코드>

인가코드 얻는 법:
  1. 아래 URL을 브라우저에서 열기
  2. 카카오 계정으로 로그인
  3. 리다이렉트된 주소창에서 code= 값 복사
     예: http://localhost:5000/callback?code=XXXXXXXXXXXXXXXX
                                              ↑ 이 부분 복사
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT       = Path(__file__).parent.parent
_TOKEN_FILE = _ROOT / "data" / "kakao_tokens.json"
_ENV_FILE   = _ROOT / ".env"
_TOKEN_URL  = "https://kauth.kakao.com/oauth/token"
_SEND_URL   = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
_REDIRECT   = "http://localhost:5000/callback"


def _exchange(rest_api_key: str, auth_code: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "client_id":    rest_api_key,
        "redirect_uri": _REDIRECT,
        "code":         auth_code,
    }).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _save(rest_api_key: str, td: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tokens = {
        "rest_api_key":  rest_api_key,
        "access_token":  td["access_token"],
        "refresh_token": td.get("refresh_token", ""),
        "expires_at":    time.time() + td.get("expires_in", 21600),
        "issued_at":     time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_env(key: str, value: str) -> None:
    lines, found = [], False
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _test_send(access_token: str) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    payload = {
        "object_type": "text",
        "text": (
            "AI 투자 어시스턴트\n"
            "카카오톡 긴급 알림 연동 완료!\n\n"
            "앞으로 이런 상황에서 카카오톡으로 즉시 알림:\n"
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
    except Exception as e:
        print(f"발송 오류: {e}")
        return False


def main():
    rest_api_key = os.getenv("KAKAO_REST_API_KEY", "5ef7e2d4074ee81b1c76084c757f3045").strip()

    if len(sys.argv) < 2:
        # 인가 코드 없이 실행 → URL만 출력
        auth_url = (
            "https://kauth.kakao.com/oauth/authorize"
            f"?client_id={rest_api_key}"
            "&redirect_uri=http%3A//localhost%3A5000/callback"
            "&response_type=code"
            "&scope=talk_message"
        )
        print()
        print("=" * 60)
        print("  카카오 로그인 URL")
        print("=" * 60)
        print()
        print("아래 URL을 브라우저에서 열어 카카오 계정으로 로그인하세요:")
        print()
        print(auth_url)
        print()
        print("로그인 후 브라우저 주소창이 아래처럼 바뀝니다:")
        print("  http://localhost:5000/callback?code=XXXXXXXXXXXXXXXXXX")
        print()
        print("주소창의 code= 값을 복사한 뒤 아래 명령을 실행하세요:")
        print()
        print(f"  python scripts/kakao_exchange.py <복사한_코드>")
        print()
        return

    auth_code = sys.argv[1].strip()
    # URL 전체를 붙여넣은 경우 code 파라미터 추출
    if "code=" in auth_code:
        import urllib.parse as up
        qs = up.parse_qs(up.urlparse(auth_code).query)
        auth_code = qs.get("code", [auth_code])[0]

    print()
    print(f"인가 코드: {auth_code[:12]}...")
    print("토큰 교환 중...", end="", flush=True)

    try:
        td = _exchange(rest_api_key, auth_code)
    except Exception as e:
        print(f"\n실패: {e}")
        sys.exit(1)

    if "access_token" not in td:
        print(f"\n토큰 발급 실패: {td}")
        err = td.get("error", "")
        if err == "KOE320":
            print()
            print("인가 코드가 만료됐습니다 (약 10분 유효).")
            print("URL을 다시 방문해 새 코드를 발급받으세요:")
            print()
            auth_url = (
                "https://kauth.kakao.com/oauth/authorize"
                f"?client_id={rest_api_key}"
                "&redirect_uri=http%3A//localhost%3A5000/callback"
                "&response_type=code"
                "&scope=talk_message"
            )
            print(auth_url)
        sys.exit(1)

    print(" 완료!")
    _save(rest_api_key, td)
    _update_env("KAKAO_REST_API_KEY", rest_api_key)
    print(f"토큰 저장: {_TOKEN_FILE}")

    print("테스트 메시지 발송...", end="", flush=True)
    ok = _test_send(td["access_token"])
    if ok:
        print(" 성공!")
        print()
        print("=" * 60)
        print("  카카오톡을 확인하세요! 설정이 완료됐습니다.")
        print("=" * 60)
        print()
        print("추가 테스트: python scripts/kakao_test.py --all")
    else:
        print(" 실패")
        print()
        print("토큰은 발급됐습니다.")
        print("발송 실패 원인 확인:")
        print("  developers.kakao.com → 앱 → 카카오 로그인 → 동의항목")
        print("  → '카카오톡 메시지 전송' → [설정] → 이용 중 동의")
    print()


if __name__ == "__main__":
    main()
