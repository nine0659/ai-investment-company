"""
scripts/generate_telegram_session.py
텔레그램 세션 문자열 생성기 — 로컬에서 1회만 실행

사용법:
  python scripts/generate_telegram_session.py

실행 후 출력되는 SESSION_STRING을 GitHub Actions Secret에 저장:
  Secret 이름: TELEGRAM_SESSION_STRING

필요한 것:
  - TELEGRAM_API_ID    : my.telegram.org에서 발급한 숫자 ID
  - TELEGRAM_API_HASH  : my.telegram.org에서 발급한 해시 문자열
  (위 두 값을 .env 파일에 넣거나, 스크립트 실행 시 직접 입력)
"""

import asyncio
import os
import sys

try:
    from pyrogram import Client
    from pyrogram.types import User
except ImportError:
    print("pyrogram가 설치되지 않았습니다. 먼저 실행하세요:")
    print("  pip install pyrogram")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_credentials() -> tuple[str, str]:
    api_id   = os.getenv("TELEGRAM_API_ID",   "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH",  "").strip()

    if not api_id:
        print("\n=== 텔레그램 API 자격증명 입력 ===")
        print("my.telegram.org > API development tools 에서 확인")
        api_id   = input("api_id (숫자): ").strip()
        api_hash = input("api_hash (문자열): ").strip()

    if not api_id or not api_hash:
        print("api_id / api_hash 가 없습니다. 종료합니다.")
        sys.exit(1)

    return api_id, api_hash


async def generate_session(api_id: str, api_hash: str) -> str:
    print("\n전화번호 인증을 시작합니다 (텔레그램 앱에서 코드 확인).")
    print("이 과정은 1회만 필요합니다.\n")

    async with Client(
        name=":memory:",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session_string = await app.export_session_string()
        me: User = await app.get_me()
        print(f"\n로그인 성공: {me.first_name} (@{me.username})")
        return session_string


def main():
    api_id, api_hash = get_credentials()
    session_string = asyncio.run(generate_session(api_id, api_hash))

    print("\n" + "=" * 60)
    print("아래 값을 GitHub Actions Secret에 저장하세요")
    print("Secret 이름: TELEGRAM_SESSION_STRING")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("\n.env 파일에도 저장하려면 아래 줄을 추가하세요:")
    print(f"TELEGRAM_SESSION_STRING={session_string}")


if __name__ == "__main__":
    main()
