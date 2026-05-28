"""
scripts/kakao_setup.py
카카오톡 API 발급 및 토큰 저장 마법사

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[사전 준비 — API 발급 5단계]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. https://developers.kakao.com 접속 → 로그인

2. 상단 [내 애플리케이션] → [애플리케이션 추가하기]
   앱 이름: AI투자어시스턴트 (자유롭게)
   사업자명: 개인
   → [저장]

3. 생성된 앱 클릭 → 왼쪽 메뉴 [앱 키]
   → "REST API 키" 복사해두기

4. 왼쪽 메뉴 [카카오 로그인] → 활성화: ON
   → [Redirect URI] → [추가] → http://localhost:5000/callback → [저장]

5. 왼쪽 메뉴 [카카오 로그인] → [동의항목]
   → "카카오톡 메시지 전송" → [설정] → 이용 중 동의 → [저장]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
준비 완료 후 이 스크립트 실행:
  python scripts/kakao_setup.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import sys
import time
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────────────────────
_ROOT       = Path(__file__).parent.parent
_TOKEN_FILE = _ROOT / "data" / "kakao_tokens.json"
_ENV_FILE   = _ROOT / ".env"

_AUTH_URL    = "https://kauth.kakao.com/oauth/authorize"
_TOKEN_URL   = "https://kauth.kakao.com/oauth/token"
_REDIRECT    = "http://localhost:5000/callback"
_PORT        = 5000

# 브라우저 콜백에서 수신한 인가 코드
_captured: dict = {"code": None, "error": None}


# ── 로컬 콜백 서버 ────────────────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            _captured["code"] = params["code"][0]
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                "<h2 style='color:#3b82f6'>&#10003; KakaoTalk login OK!</h2>"
                "<p>Close this tab and return to terminal.</p>"
                "</body></html>"
            )
            body = html.encode("utf-8")
        else:
            _captured["error"] = params.get("error", ["unknown"])[0]
            html = (
                "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                "<h2 style='color:#ef4444'>&#10007; Login failed</h2>"
                "<p>Check terminal for details.</p>"
                "</body></html>"
            )
            body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # 서버 로그 숨김


# ── 토큰 교환 ─────────────────────────────────────────────────────

def _exchange_code(rest_api_key: str, auth_code: str) -> dict:
    """인가 코드 → 액세스·리프레시 토큰 교환."""
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


# ── .env 업데이트 ─────────────────────────────────────────────────

def _update_env(key: str, value: str) -> None:
    """기존 .env에 키=값 추가/덮어쓰기."""
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


# ── 메인 마법사 ───────────────────────────────────────────────────

def main():
    print()
    print("=" * 60)
    print("  카카오톡 API 설정 마법사")
    print("=" * 60)
    print()
    print("사전 준비 (developers.kakao.com):")
    print("  1. 앱 생성 → REST API 키 복사")
    print("  2. 카카오 로그인 활성화")
    print("  3. Redirect URI: http://localhost:5000/callback 등록")
    print("  4. 동의항목 → '카카오톡 메시지 전송' 설정")
    print()

    # REST API 키 입력
    rest_api_key = input("REST API 키를 붙여넣으세요: ").strip()
    if not rest_api_key:
        print("✗ REST API 키가 없습니다. 중단합니다.")
        sys.exit(1)

    print()
    print("브라우저에서 카카오 로그인 창이 열립니다.")
    print("로그인 완료 후 자동으로 돌아옵니다.")
    print()

    # 콜백 서버 시작 (백그라운드)
    server = HTTPServer(("localhost", _PORT), _CallbackHandler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    # 브라우저 열기
    auth_url = (
        f"{_AUTH_URL}"
        f"?client_id={rest_api_key}"
        f"&redirect_uri={urllib.parse.quote(_REDIRECT)}"
        f"&response_type=code"
        f"&scope=talk_message"
    )
    webbrowser.open(auth_url)

    # 최대 120초 대기
    print("로그인 대기 중", end="", flush=True)
    for _ in range(120):
        time.sleep(1)
        print(".", end="", flush=True)
        if _captured["code"] or _captured["error"]:
            break
    print()
    server.server_close()

    if _captured["error"]:
        print(f"✗ 로그인 실패: {_captured['error']}")
        sys.exit(1)

    if not _captured["code"]:
        print("✗ 시간 초과 (120초). 다시 시도하세요.")
        sys.exit(1)

    auth_code = _captured["code"]
    print(f"✓ 인가 코드 수신 완료 ({auth_code[:8]}...)")

    # 토큰 교환
    print("토큰 교환 중...", end="", flush=True)
    try:
        token_data = _exchange_code(rest_api_key, auth_code)
    except Exception as e:
        print(f"\n✗ 토큰 교환 실패: {e}")
        sys.exit(1)

    if "access_token" not in token_data:
        print(f"\n✗ 토큰 발급 실패: {token_data}")
        sys.exit(1)

    print(" ✓")
    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = token_data.get("expires_in", 21600)  # 기본 6시간

    # 토큰 파일 저장
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tokens = {
        "rest_api_key":   rest_api_key,
        "access_token":   access_token,
        "refresh_token":  refresh_token,
        "expires_at":     time.time() + expires_in,
        "issued_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _TOKEN_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 토큰 저장 완료: {_TOKEN_FILE}")

    # .env에도 REST API 키 저장
    _update_env("KAKAO_REST_API_KEY", rest_api_key)
    print(f"✓ .env 업데이트 완료")

    print()
    print("=" * 60)
    print("  설정 완료! 테스트 발송을 진행합니다...")
    print("=" * 60)
    print()

    # 즉시 테스트 발송
    _test_send(access_token)


def _test_send(access_token: str) -> None:
    """설정 직후 테스트 메시지 발송."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

    payload = {
        "object_type": "text",
        "text": (
            f"🤖 AI 투자 어시스턴트\n"
            f"카카오톡 긴급 알림 연동 완료!\n\n"
            f"이제 다음 상황에서 카카오톡으로 즉시 알림을 받습니다:\n"
            f"🚀 기회 알림 — 관심종목 복수 매수 신호 발생\n"
            f"🚨 위험 알림 — KOSPI 급락·지정학 충격·보유종목 급락\n\n"
            f"연결 시각: {now}"
        ),
        "link": {"web_url": "", "mobile_web_url": ""},
        "button_title": "확인",
    }
    import urllib.request, urllib.parse, json as _json
    data = urllib.parse.urlencode({
        "template_object": _json.dumps(payload, ensure_ascii=False)
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
            result = _json.loads(resp.read().decode())
            if result.get("result_code") == 0:
                print("✓ 테스트 메시지 발송 성공!")
                print("  카카오톡을 확인하세요 📱")
            else:
                print(f"✗ 테스트 발송 실패: {result}")
                print("  동의항목에서 '카카오톡 메시지 전송'이 설정됐는지 확인하세요.")
    except Exception as e:
        print(f"✗ 발송 오류: {e}")

    print()
    print("다음 단계:")
    print("  python scripts/kakao_test.py   ← 추가 테스트")
    print("  python scheduler.py            ← 스케줄러 실행")
    print()


if __name__ == "__main__":
    main()
