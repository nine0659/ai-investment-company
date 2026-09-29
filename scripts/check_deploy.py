"""Render 배포 실패 감지 — 운영 /api/status의 commit이 저장소 최신 커밋과 같은지 확인.

배경(2026-09-21 사고): Render는 배포가 실패하면 직전 성공 컨테이너를 계속 서비스하고,
이 프로젝트엔 그 사실을 알리는 경로가 없어 옛 코드가 며칠간 운영됐다.
CI green·push 성공은 "운영 반영"이 아니다.

환경변수:
  RENDER_URL          운영 URL (기본: https://ai-investment-company.onrender.com)
  EXPECTED_SHA        저장소 최신 커밋 SHA (필수)
  WAIT_MINUTES        불일치 시 재시도 총 대기(분, 기본 20) — 배포 소요시간 흡수
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  경보 발송용

종료코드: 0=일치, 1=불일치/응답없음(경보 발송 후).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

DEFAULT_URL = "https://ai-investment-company.onrender.com"


def evaluate(status: dict | None, expected_sha: str) -> tuple[bool, str]:
    """(정상 여부, 사유). 순수 함수 — 테스트 대상."""
    if not status:
        return False, "운영 서버가 응답하지 않습니다 (다운/슬립/배포 중)."
    deployed = status.get("commit")
    if not deployed:
        return False, (
            "/api/status에 commit 필드가 없습니다 — 옛 코드가 계속 운영 중이라는 신호입니다 "
            "(배포 실패 가능성)."
        )
    if deployed != expected_sha:
        return False, (
            f"운영 커밋({deployed[:7]})이 저장소 최신({expected_sha[:7]})과 다릅니다 — "
            "Render 배포 실패로 옛 컨테이너가 계속 돌고 있을 가능성이 큽니다."
        )
    return True, f"운영 커밋 일치 ({deployed[:7]})"


def fetch_status(base_url: str, timeout: int = 20) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/status", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # 네트워크·JSON 오류 모두 "응답 없음"으로 취급
        print(f"status 조회 실패: {e}")
        return None


def send_alert(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("텔레그램 시크릿 없음 — 경보 생략")
        return
    body = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"텔레그램 발송 실패: {e}")


def main() -> int:
    expected = os.getenv("EXPECTED_SHA", "").strip()
    if not expected:
        print("EXPECTED_SHA 미설정")
        return 1
    base = os.getenv("RENDER_URL") or DEFAULT_URL
    wait_min = int(os.getenv("WAIT_MINUTES", "20"))
    deadline = time.time() + wait_min * 60

    while True:
        ok, reason = evaluate(fetch_status(base), expected)
        print(reason)
        if ok:
            return 0
        if time.time() >= deadline:
            break
        time.sleep(60)

    send_alert(
        "🚨 Render 배포 미반영 감지\n\n"
        f"{reason}\n\n"
        f"{wait_min}분 대기 후에도 해결되지 않았습니다.\n"
        "Render 대시보드 → Events/Logs에서 배포 실패 원인(환경변수 누락 등)을 확인하세요."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
