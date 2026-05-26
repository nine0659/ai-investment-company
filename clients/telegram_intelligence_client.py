"""
clients/telegram_intelligence_client.py
텔레그램 투자 채널 인텔리전스 수집

대상: 검증된 국내·해외 투자 정보 채널
수집 범위: 최근 N시간 이내 메시지
GitHub Actions 환경: TELEGRAM_SESSION_STRING (Secret) 사용

환경변수:
  TELEGRAM_API_ID          — my.telegram.org api_id
  TELEGRAM_API_HASH        — my.telegram.org api_hash
  TELEGRAM_SESSION_STRING  — generate_telegram_session.py 로 생성
  TELEGRAM_HOURS_BACK      — 수집 시간 범위 (기본 20시간)
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ── 수집 대상 채널 목록 ─────────────────────────────────────────────
# username: 채널 @username (@ 제외), label: 브리핑에서 표시할 이름
# category: macro / sector / korea / us / crypto (필터링 기준)
CHANNELS: list[dict] = [
    # ── 리서치·분석 ───────────────────────────────────────────────────
    {"username": "BRILLER_Research",        "label": "브릴러리서치",     "category": "research"},
    {"username": "ym_research",             "label": "YM리서치",         "category": "research"},
    {"username": "aetherjapanresearch",     "label": "에테르재팬리서치", "category": "research"},
    {"username": "rafikiresearch",          "label": "라피키리서치",     "category": "research"},
    {"username": "report_figure_by_offset", "label": "오프셋리포트",     "category": "research"},

    # ── 매크로·글로벌 ─────────────────────────────────────────────────
    {"username": "cahier_de_market",        "label": "카이에드마켓",     "category": "macro"},
    {"username": "Macrojunglemicrolens",    "label": "매크로정글",       "category": "macro"},
    {"username": "joonsungkim",             "label": "준성킴",           "category": "macro"},
    {"username": "HANAchina",               "label": "하나중국",         "category": "china"},

    # ── 퀀트·차트 ─────────────────────────────────────────────────────
    {"username": "toptownquant",            "label": "탑타운퀀트",       "category": "quant"},
    {"username": "chartbook260226",         "label": "차트북",           "category": "quant"},
    {"username": "darthacking",             "label": "다트해킹",         "category": "quant"},

    # ── 수급·공시 ─────────────────────────────────────────────────────
    {"username": "insidertracking",         "label": "인사이더트래킹",   "category": "flow"},
    {"username": "comvestment",             "label": "컴베스트먼트",     "category": "flow"},

    # ── 한국 주식 종목·테마 ───────────────────────────────────────────
    {"username": "EarlyStock1",             "label": "얼리스탁",         "category": "korea"},
    {"username": "Jstockclass",             "label": "J주식클래스",      "category": "korea"},
    {"username": "one_going",               "label": "원고잉",           "category": "korea"},
    {"username": "WoosanXNNN",              "label": "우산X",            "category": "korea"},
    {"username": "easobi",                  "label": "이아소비",         "category": "korea"},
    {"username": "huhjae",                  "label": "허재",             "category": "korea"},
    {"username": "triple_stock",            "label": "트리플스탁",       "category": "korea"},
    {"username": "bumgore",                 "label": "범고래",           "category": "korea"},
    {"username": "bornlupin",               "label": "본루팡",           "category": "korea"},
    {"username": "desperatestudycafe",      "label": "절실스터디",       "category": "korea"},
    {"username": "pikachu_aje",             "label": "피카츄아재",       "category": "korea"},
    {"username": "athletes_village",        "label": "선수촌",           "category": "korea"},
    {"username": "Joorini34",               "label": "주리니",           "category": "korea"},
    {"username": "kkkontemp",               "label": "꼰템프",           "category": "korea"},
    {"username": "tambangwang",             "label": "탐방왕",           "category": "korea"},
]

_DEFAULT_HOURS = 20  # 기본 수집 범위 (시간)
_MAX_MSG_PER_CHANNEL = 30  # 채널당 최대 메시지 수
_MIN_MSG_LENGTH = 30  # 이 글자 수 미만 메시지 제외 (단순 이모지 등)


def _hours_back() -> int:
    try:
        return int(os.getenv("TELEGRAM_HOURS_BACK", _DEFAULT_HOURS))
    except ValueError:
        return _DEFAULT_HOURS


async def _fetch_channel_messages(
    app,
    channel: dict,
    cutoff: datetime,
) -> list[dict]:
    """단일 채널에서 cutoff 이후 메시지 수집."""
    from pyrogram.errors import FloodWait, ChannelInvalid, UsernameNotOccupied

    messages: list[dict] = []
    username = channel["username"]
    label    = channel["label"]

    try:
        async for msg in app.get_chat_history(username, limit=_MAX_MSG_PER_CHANNEL):
            msg_date = msg.date
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date and msg_date < cutoff:
                break
            text = msg.text or msg.caption or ""
            if len(text) < _MIN_MSG_LENGTH:
                continue
            messages.append({
                "channel":  label,
                "category": channel.get("category", "general"),
                "text":     text[:600],
                "date":     msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "",
                "link":     f"https://t.me/{username}/{msg.id}",
            })
    except FloodWait as e:
        logger.warning("[텔레그램] 채널 %s FloodWait %ds — 건너뜀", label, e.value)
    except (ChannelInvalid, UsernameNotOccupied):
        logger.warning("[텔레그램] 채널 %s 접근 불가 (비공개 or 탈퇴)", label)
    except Exception as e:
        logger.debug("[텔레그램] 채널 %s 오류: %s", label, e)

    return messages


async def _collect_all(hours_back: int) -> list[dict]:
    """전체 채널 비동기 수집."""
    import os
    from pyrogram import Client

    api_id      = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash    = os.getenv("TELEGRAM_API_HASH", "").strip()
    session_str = os.getenv("TELEGRAM_SESSION_STRING", "").strip()

    if not all([api_id, api_hash, session_str]):
        missing = [k for k, v in {
            "TELEGRAM_API_ID": api_id,
            "TELEGRAM_API_HASH": api_hash,
            "TELEGRAM_SESSION_STRING": session_str,
        }.items() if not v]
        logger.warning("[텔레그램] 환경변수 미설정: %s - 수집 건너뜀", missing)
        return []

    active_channels = [c for c in CHANNELS if c.get("username")]
    if not active_channels:
        logger.info("[텔레그램] 수집 채널 없음 - CHANNELS 목록에 username을 추가하세요")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    all_messages: list[dict] = []

    async with Client(
        name=":memory:",
        api_id=int(api_id),
        api_hash=api_hash,
        session_string=session_str,
        in_memory=True,
    ) as app:
        for channel in active_channels:
            msgs = await _fetch_channel_messages(app, channel, cutoff)
            all_messages.extend(msgs)
            logger.debug("[텔레그램] %s: %d건", channel["label"], len(msgs))

    return all_messages


def fetch_telegram_intelligence(hours_back: int | None = None) -> list[dict]:
    """텔레그램 채널 메시지 수집 (동기 진입점).

    반환: [{"channel", "category", "text", "date", "link"}, ...]
    설정 미완료 시 빈 리스트 반환 (에러 없이 graceful 처리).
    """
    h = hours_back or _hours_back()
    try:
        return asyncio.run(_collect_all(h))
    except Exception as e:
        logger.error("[텔레그램] 수집 실패: %s", e)
        return []


def format_for_context(messages: list[dict], max_per_category: int = 10) -> str:
    """수집된 메시지를 LLM 컨텍스트용 텍스트로 포맷."""
    if not messages:
        return ""

    # 카테고리별 그룹화
    by_cat: dict[str, list[dict]] = {}
    for msg in messages:
        cat = msg.get("category", "general")
        by_cat.setdefault(cat, []).append(msg)

    lines: list[str] = ["=== 텔레그램 투자 채널 인텔리전스 ==="]
    for cat, msgs in by_cat.items():
        lines.append(f"\n[{cat}]")
        for msg in msgs[:max_per_category]:
            ch   = msg.get("channel", "")
            date = msg.get("date", "")
            text = msg.get("text", "").replace("\n", " ").strip()
            lines.append(f"  [{ch} {date}] {text[:250]}")

    return "\n".join(lines)
