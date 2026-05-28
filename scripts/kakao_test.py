"""
scripts/kakao_test.py
카카오톡 발송 테스트 — 설정 확인 및 메시지 유형별 테스트

실행:
  python scripts/kakao_test.py              # 기본 테스트
  python scripts/kakao_test.py --all        # 모든 알림 유형 테스트
  python scripts/kakao_test.py --refresh    # 토큰 갱신 강제 테스트
"""
import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

_TOKEN_FILE = _ROOT / "data" / "kakao_tokens.json"


def check_setup() -> dict:
    """설정 상태 확인."""
    print("\n[설정 상태 확인]")

    if not _TOKEN_FILE.exists():
        print("✗ 토큰 파일 없음: data/kakao_tokens.json")
        print("  → python scripts/kakao_setup.py 를 먼저 실행하세요")
        sys.exit(1)

    tokens = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    access  = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token", "")
    exp_at  = tokens.get("expires_at", 0)

    print(f"  REST API 키: {'설정됨 ✓' if tokens.get('rest_api_key') else '없음 ✗'}")
    print(f"  액세스 토큰: {access[:12]}...{'✓' if access else '✗'}")
    print(f"  리프레시 토큰: {'설정됨 ✓' if refresh else '없음 ✗'}")

    remaining = exp_at - time.time()
    if remaining > 0:
        h, m = int(remaining // 3600), int((remaining % 3600) // 60)
        print(f"  토큰 만료까지: {h}시간 {m}분 남음")
    else:
        print("  토큰 만료됨 → 자동 갱신 시도 예정")

    if tokens.get("issued_at"):
        print(f"  최초 발급: {tokens['issued_at']}")

    return tokens


def test_basic(tokens: dict) -> bool:
    """기본 텍스트 메시지 발송 테스트."""
    from clients.kakao_client import send_message
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S")

    print("\n[기본 발송 테스트]")
    print("  메시지 전송 중...", end="", flush=True)
    ok = send_message(
        f"✅ 카카오톡 테스트 성공!\n\nAI 투자 어시스턴트 정상 연결\n발송 시각: {now}\n\n"
        f"앞으로 다음 상황에서 알림이 옵니다:\n"
        f"  🚀 매수 기회 감지\n"
        f"  🚨 시장 위험 감지",
        button_title="확인"
    )
    if ok:
        print(" ✓ 성공!")
        return True
    else:
        print(" ✗ 실패")
        print("  → 토큰이 만료됐으면 python scripts/kakao_setup.py 재실행")
        return False


def test_opportunity_alert() -> bool:
    """기회 알림 유형 테스트."""
    from services.alert_service import send_alert, TYPE_OPPORTUNITY
    print("\n[기회 알림 테스트 (OPPORTUNITY)]")
    print("  발송 중...", end="", flush=True)
    send_alert(
        TYPE_OPPORTUNITY,
        "SK하이닉스 — 지금이 진입 타이밍 [테스트]",
        "종목: SK하이닉스(000660)\n"
        "현재가: 198,500원 | RSI 26 극과매도 | 볼린저밴드 하단 터치\n\n"
        "발동 신호:\n"
        "  ✅ RSI 26 극과매도 + 볼린저밴드 하단 — 강한 반등 기대\n"
        "  ✅ 거래량 280% 급증 + HBM 수주 호재\n\n"
        "⚠️ 이 메시지는 테스트입니다",
        code="000660", name="SK하이닉스"
    )
    print(" ✓ (결과는 카카오톡 확인)")
    return True


def test_risk_alert() -> bool:
    """위험 알림 유형 테스트."""
    from services.alert_service import send_alert, TYPE_RISK
    print("\n[위험 알림 테스트 (RISK)]")
    print("  발송 중...", end="", flush=True)
    send_alert(
        TYPE_RISK,
        "KOSPI 급락 -2.8% — 즉시 포지션 점검 [테스트]",
        "KOSPI: 2,540.12 (-2.8%)\n\n"
        "외국인 순매도 8,200억 / VIX 28.5 급등\n"
        "손절 조건 확인 필요. 현금 비중 확대 권고.\n\n"
        "⚠️ 이 메시지는 테스트입니다"
    )
    print(" ✓ (결과는 카카오톡 확인)")
    return True


def test_token_refresh(tokens: dict) -> None:
    """토큰 강제 갱신 테스트."""
    from clients.kakao_client import _refresh_access_token, _save_tokens
    import os

    print("\n[토큰 갱신 테스트]")
    rest_key     = os.getenv("KAKAO_REST_API_KEY", tokens.get("rest_api_key", ""))
    refresh_token = tokens.get("refresh_token", "")

    if not rest_key or not refresh_token:
        print("  ✗ REST API 키 또는 리프레시 토큰 없음")
        return

    print("  갱신 요청 중...", end="", flush=True)
    new_tok = _refresh_access_token(rest_key, refresh_token)
    if new_tok and "access_token" in new_tok:
        tokens["access_token"] = new_tok["access_token"]
        tokens["expires_at"]   = time.time() + new_tok.get("expires_in", 21600)
        if "refresh_token" in new_tok:
            tokens["refresh_token"] = new_tok["refresh_token"]
        _save_tokens(tokens)
        print(" ✓ 갱신 성공!")
        print(f"  새 액세스 토큰: {tokens['access_token'][:12]}...")
    else:
        print(f" ✗ 갱신 실패: {new_tok}")
        print("  리프레시 토큰 만료 시 kakao_setup.py 재실행 필요")


def main():
    parser = argparse.ArgumentParser(description="카카오톡 발송 테스트")
    parser.add_argument("--all",     action="store_true", help="모든 알림 유형 테스트")
    parser.add_argument("--refresh", action="store_true", help="토큰 갱신 테스트")
    args = parser.parse_args()

    print("=" * 50)
    print("  카카오톡 발송 테스트")
    print("=" * 50)

    tokens = check_setup()

    if args.refresh:
        test_token_refresh(tokens)
        return

    ok = test_basic(tokens)
    if not ok:
        print("\n✗ 기본 테스트 실패. 설정을 확인하세요.")
        sys.exit(1)

    if args.all:
        test_opportunity_alert()
        time.sleep(1)  # 연속 발송 간격
        test_risk_alert()

    print()
    print("=" * 50)
    print("  테스트 완료! 카카오톡을 확인하세요 📱")
    print("=" * 50)
    print()


if __name__ == "__main__":
    main()
