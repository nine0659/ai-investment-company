"""
scripts/kakao_setup.py
카카오톡 API 토큰 발급 마법사

실행:
  python scripts/kakao_setup.py
  python scripts/kakao_setup.py --key 5ef7e2d4074ee81b1c76084c757f3045
"""
import argparse
import json
import sys
import time
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ROOT       = Path(__file__).parent.parent
_TOKEN_FILE = _ROOT / "data" / "kakao_tokens.json"
_ENV_FILE   = _ROOT / ".env"
_TOKEN_URL  = "https://kauth.kakao.com/oauth/token"
_AUTH_URL   = "https://kauth.kakao.com/oauth/authorize"
_REDIRECT   = "http://localhost:5000/callback"
_PORT       = 5000

_captured: dict = {"code": None, "error": None}


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _captured["code"] = params["code"][0]
            html = (
                "<html><head><meta charset='utf-8'></head>"
                "<body style='font-family:sans-serif;text-align:center;padding:60px;background:#0f172a;color:#e2e8f0'>"
                "<h2 style='color:#22c55e'>&#10003; 카카오 로그인 완료!</h2>"
                "<p>이 창을 닫고 터미널로 돌아가세요.</p>"
                "</body></html>"
            )
        else:
            _captured["error"] = params.get("error", ["unknown"])[0]
            html = (
                "<html><head><meta charset='utf-8'></head>"
                "<body style='font-family:sans-serif;text-align:center;padding:60px;background:#0f172a;color:#e2e8f0'>"
                "<h2 style='color:#ef4444'>&#10007; 로그인 실패</h2>"
                "<p>터미널 오류를 확인하세요.</p>"
                "</body></html>"
            )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _exchange_code(rest_api_key: str, auth_code: str) -> dict:
    import urllib.error
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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"\n  [HTTP {e.code}] 카카오 응답: {body}")
        try:
            return json.loads(body)
        except Exception:
            return {"error": f"HTTP {e.code}", "error_description": body}


def _update_env(key: str, value: str) -> None:
    lines = []
    found = False
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


def _save_tokens(rest_api_key: str, token_data: dict) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tokens = {
        "rest_api_key":  rest_api_key,
        "access_token":  token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at":    time.time() + token_data.get("expires_in", 21600),
        "issued_at":     time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  토큰 저장: {_TOKEN_FILE}")


def _test_send(access_token: str) -> bool:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    payload = {
        "object_type": "text",
        "text": (
            "AI 투자 어시스턴트\n"
            "카카오톡 긴급 알림 연동 완료!\n\n"
            "앞으로 다음 상황에서 카카오톡으로 즉시 알림:\n"
            "  매수 기회 감지\n"
            "  시장 위험 감지\n\n"
            f"연결 시각: {now}"
        ),
        "link": {"web_url": "", "mobile_web_url": ""},
        "button_title": "확인",
    }
    data = urllib.parse.urlencode({
        "template_object": json.dumps(payload, ensure_ascii=False)
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=data,
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
        print(f"  발송 오류: {e}")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="", help="REST API 키")
    args = parser.parse_args()

    print()
    print("=" * 58)
    print("  카카오톡 API 설정 마법사")
    print("=" * 58)

    # REST API 키
    rest_api_key = args.key.strip()
    if not rest_api_key:
        import os
        rest_api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if not rest_api_key:
        rest_api_key = input("REST API 키를 입력하세요: ").strip()
    if not rest_api_key:
        print("REST API 키가 없습니다. 종료합니다.")
        sys.exit(1)

    print(f"  REST API 키: {rest_api_key[:8]}...")

    # 인증 URL 생성
    auth_url = (
        f"{_AUTH_URL}"
        f"?client_id={rest_api_key}"
        f"&redirect_uri={urllib.parse.quote(_REDIRECT, safe='')}"
        f"&response_type=code"
    )

    print()
    print("[1단계] 로컬 콜백 서버 시작 (포트 5000)...")
    server = HTTPServer(("localhost", _PORT), _CallbackHandler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    print("  완료")

    print()
    print("[2단계] 카카오 로그인")
    print()
    print("  아래 URL을 브라우저에서 열어 카카오 계정으로 로그인하세요:")
    print()
    print(f"  {auth_url}")
    print()

    # 브라우저 자동 오픈 시도
    try:
        webbrowser.open(auth_url)
        print("  (브라우저 자동으로 열렸습니다)")
    except Exception:
        print("  (위 URL을 직접 복사해서 브라우저에 붙여넣으세요)")

    print()
    print("  로그인 완료 대기 중 (최대 5분)", end="", flush=True)
    for i in range(300):
        time.sleep(1)
        if i % 10 == 0:
            print(".", end="", flush=True)
        if _captured["code"] or _captured["error"]:
            break
    print()
    server.server_close()

    if _captured["error"]:
        print(f"\n오류: {_captured['error']}")
        print("카카오 개발자 콘솔에서 설정을 확인하세요:")
        print("  - Redirect URI: http://localhost:5000/callback 등록됐는지")
        print("  - 동의항목: 카카오톡 메시지 전송 설정됐는지")
        sys.exit(1)

    if not _captured["code"]:
        print("\n시간 초과 (120초). 다시 실행해주세요.")
        sys.exit(1)

    auth_code = _captured["code"]
    print(f"\n  인가 코드 수신: {auth_code[:10]}...")

    # 토큰 교환
    print()
    print("[3단계] 액세스 토큰 발급...")
    try:
        token_data = _exchange_code(rest_api_key, auth_code)
    except Exception as e:
        print(f"  실패: {e}")
        sys.exit(1)

    if "access_token" not in token_data:
        print(f"  토큰 발급 실패: {token_data}")
        if token_data.get("error") == "KOE320":
            print()
            print("  오류 원인: 인가 코드가 이미 사용됐거나 만료됨")
            print("  해결: 스크립트를 다시 실행하세요")
        sys.exit(1)

    print("  완료")
    _save_tokens(rest_api_key, token_data)
    _update_env("KAKAO_REST_API_KEY", rest_api_key)

    # 테스트 발송
    print()
    print("[4단계] 테스트 메시지 발송...")
    ok = _test_send(token_data["access_token"])
    if ok:
        print("  성공! 카카오톡을 확인하세요.")
    else:
        print("  발송 실패. 동의항목을 확인하세요.")
        print("  developers.kakao.com → 앱 → 카카오 로그인 → 동의항목")
        print("  → '카카오톡 메시지 전송' → 이용 중 동의")

    print()
    print("=" * 58)
    if ok:
        print("  설정 완료! 이제 긴급 알림이 카카오톡으로 발송됩니다.")
    else:
        print("  토큰 발급은 완료됐으나 메시지 발송 권한을 확인하세요.")
    print("=" * 58)
    print()
    print("추가 테스트: python scripts/kakao_test.py --all")
    print()


if __name__ == "__main__":
    main()
