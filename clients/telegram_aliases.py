"""텔레그램 한글 명령어 별칭.

텔레그램 공식 명령 메뉴는 영문 소문자만 허용하지만, 이 봇은 수신한 텍스트를 직접 해석하므로
"/위험" 또는 그냥 "위험"처럼 한글로 입력해도 같은 동작을 하게 한다.
 - 슬래시 없이 쓸 수 있는 건 '조회성' 명령뿐이다. 주문·자동매매·취소·일시중단처럼 결과가 큰 명령은
   일반 대화("매수할까?")와 헷갈려 오작동하지 않도록 반드시 슬래시(/매수 …)가 있을 때만 인식한다.
 - 첫 단어가 별칭과 '정확히' 같을 때만 변환한다("위험이 뭐야?"는 그대로 AI 대화).
"""

# 한글 별칭 → 영문 명령
ALIASES: dict[str, str] = {
    "도움말": "/help", "도움": "/help", "시작": "/start", "메뉴": "/menu",
    "리서치": "/research", "검색": "/search", "현재가": "/price",
    "잔고": "/balance", "보유": "/holdings", "보유종목": "/holdings", "포트폴리오": "/portfolio",
    "프로필": "/profile", "발굴": "/discover", "반등": "/rebound", "관심종목": "/watchlist",
    "주문": "/orders", "이력": "/history", "인사이트": "/insight",
    "투자관": "/thesis", "전략": "/strategy", "자산": "/nav", "리포트": "/report", "성과": "/tracker",
    "노출": "/exposure",
    "위험": "/risk", "계좌성과": "/perf", "만약": "/whatif", "일정": "/calendar",
    "플레이북": "/playbook", "상한": "/limit", "집중도": "/limit", "결정": "/did", "원장": "/ledger", "더보기": "/more",
    "후보": "/ideas", "아이디어": "/ideas", "업종": "/sector", "왜": "/why",
    # 결과가 큰 명령 — 슬래시가 있을 때만
    "매수": "/buy", "매도": "/sell", "취소": "/cancel", "자동": "/auto", "중지": "/pause",
}
SLASH_ONLY = {"매수", "매도", "취소", "자동", "중지"}

# 하단 상시 버튼(눌리면 해당 한글 단어가 메시지로 전송 → 위 별칭으로 처리)
MENU_KEYBOARD = [
    ["위험", "계좌성과", "일정"],
    ["보유", "자산", "더보기"],
    ["후보", "업종", "성과"],
    ["플레이북", "도움말"],
]

# 텔레그램 '/' 메뉴에 보일 설명(명령 이름은 영문 고정, 설명은 한글)
COMMAND_MENU = [
    ("risk", "내 계좌 위험 (쏠림·변동성·시나리오 손실) — 한글: 위험"),
    ("perf", "내 평단 기준 성과 vs 지수 — 한글: 계좌성과"),
    ("whatif", "파는 경우/안 파는 경우 비교 — 한글: 만약"),
    ("calendar", "앞으로 2주 일정 — 한글: 일정"),
    ("more", "최근 브리핑 상세 근거 — 한글: 더보기"),
    ("ideas", "전 종목 스크리닝 상위 후보 — 한글: 후보"),
    ("sector", "업종 온도계 (강세·약세 업종) — 한글: 업종"),
    ("why", "종목이 후보인 이유 — 한글: 왜 종목명"),
    ("holdings", "보유종목 — 한글: 보유"),
    ("nav", "자산(NAV) 현황 — 한글: 자산"),
    ("tracker", "AI 추천 성과 — 한글: 성과"),
    ("playbook", "국면별 행동 규칙 — 한글: 플레이북"),
    ("limit", "종목/섹터 비중 상한과 위반 현황 — 한글: 상한"),
    ("ledger", "체결 문자/캡처로 원장 갱신 — 한글: 원장"),
    ("did", "AI 제안에 대한 내 결정 기록 — 한글: 결정"),
    ("menu", "한글 버튼 메뉴 열기 — 한글: 메뉴"),
    ("help", "전체 명령어 — 한글: 도움말"),
]


def resolve(text: str) -> str:
    """한글 별칭을 영문 명령으로 바꾼 텍스트를 반환. 해당 없으면 원문 그대로."""
    t = (text or "").strip()
    if not t:
        return text
    first, _, rest = t.partition(" ")
    had_slash = first.startswith("/")
    word = first[1:] if had_slash else first
    word = word.split("@")[0]
    cmd = ALIASES.get(word)
    if not cmd:
        return text
    if not had_slash and word in SLASH_ONLY:
        return text
    return (cmd + (" " + rest.strip() if rest.strip() else "")).strip()
