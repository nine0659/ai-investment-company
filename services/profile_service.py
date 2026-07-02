"""
services/profile_service.py
고객(투자자) 프로필 — 모든 자문의 대상이 누구인지 정의하는 단일 소스

진짜 투자 자문가와 시황 해설가의 차이는 "누구에게 조언하는지"를 아는 것이다.
이 모듈은 투자자의 목표·기간·리스크 감내·제약을 저장하고,
실보유 포트폴리오 실시간 평가와 묶어 모든 자문 접점
(CEO 브리핑·주간 전략·텔레그램 대화)에 주입할 컨텍스트 블록을 만든다.

저장소: system_settings 테이블 (key: "profile.<field>")
"""
import logging
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

# 필드 정의: (키, 한글 라벨, 기본값)
PROFILE_FIELDS: list[tuple[str, str, str]] = [
    ("goal",        "투자 목표",        "자산 증식 (구체 목표금액·기한 미설정)"),
    ("horizon",     "투자 기간",        "중장기 — 최소 6개월~3년 보유 전제"),
    ("risk",        "감내 가능 손실",   "미설정"),
    ("monthly",     "월 추가 투자 여력", "미설정"),
    ("assets_note", "주식 외 자산",     "미설정"),
    ("broker",      "실거래 방식",      "별도 증권사에서 직접 매매 — 이 시스템은 자문 전용, 주문 실행 없음"),
    ("constraints", "투자 제약",        "레버리지·미수·신용 금지"),
    ("notes",       "특이사항",         ""),
]
_FIELD_KEYS = {k for k, _, _ in PROFILE_FIELDS}


def get_profile() -> dict[str, str]:
    """저장된 프로필 반환. 미설정 필드는 기본값."""
    stored: dict[str, str] = {}
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT key, value FROM system_settings WHERE key LIKE 'profile.%'")
            ).fetchall()
        stored = {k.removeprefix("profile."): v for k, v in rows}
    except Exception as e:
        logger.warning("[프로필] 조회 실패: %s", e)
    return {k: stored.get(k) or default for k, _, default in PROFILE_FIELDS}


def set_profile_field(field: str, value: str) -> bool:
    """프로필 필드 설정. 알 수 없는 필드면 False."""
    if field not in _FIELD_KEYS:
        return False
    try:
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=CURRENT_TIMESTAMP"
                ),
                {"k": f"profile.{field}", "v": value},
            )
        logger.info("[프로필] %s 갱신", field)
        return True
    except Exception as e:
        logger.error("[프로필] 저장 실패 (%s): %s", field, e)
        return False


def _format_holdings(kis=None) -> str:
    """실보유 포트폴리오 실시간 평가 텍스트. 보유 없으면 그 사실을 명시."""
    try:
        from services.portfolio_service import calculate_pnl
        pnl = calculate_pnl(kis)
    except Exception as e:
        logger.warning("[프로필] 포트폴리오 평가 실패: %s", e)
        return "포트폴리오 조회 실패 — 보유 현황 미확인 상태로 조언하지 말 것"

    if not pnl:
        return ("등록된 보유 종목 없음 — 고객이 /holdings add 로 보유를 등록하기 전까지 "
                "특정 보유 종목을 전제로 한 조언 금지")

    total_val = sum(p["current_val"] for p in pnl) or 1
    lines = []
    sector_val: dict[str, float] = {}
    for p in pnl:
        weight = p["current_val"] / total_val * 100
        sector = p.get("sector") or "기타"
        sector_val[sector] = sector_val.get(sector, 0) + p["current_val"]
        lines.append(
            f"  {p['name']}({p['code']}): {p['quantity']:,}주 @평단 {p['avg_price']:,.0f}원 "
            f"| 현재 {p['current_price']:,.0f}원 ({p['pnl_pct']:+.1f}%) "
            f"| 평가 {p['current_val'] / 10000:,.0f}만원 | 비중 {weight:.0f}%"
        )
    total_invested = sum(p["invested"] for p in pnl) or 1
    total_pnl_pct = (total_val - total_invested) / total_invested * 100
    top_sector, top_val = max(sector_val.items(), key=lambda x: x[1])
    lines.append(
        f"  ── 총 평가 {total_val / 10000:,.0f}만원 | 총 손익 {total_pnl_pct:+.1f}% "
        f"| 최대 섹터 집중: {top_sector} {top_val / total_val * 100:.0f}%"
    )
    return "\n".join(lines)


def get_profile_context(kis=None, include_holdings: bool = True) -> str:
    """모든 자문 접점에 주입할 고객 컨텍스트 블록."""
    profile = get_profile()
    lines = ["[고객 프로필 — 이 조언의 대상. 일반론 금지, 모든 판단을 이 고객에게 연결할 것]"]
    for key, label, _ in PROFILE_FIELDS:
        v = profile.get(key, "")
        if v:
            lines.append(f"  {label}: {v}")

    if include_holdings:
        lines.append("")
        lines.append("[고객 실보유 포트폴리오 — 실시간 평가]")
        lines.append(_format_holdings(kis))

    return "\n".join(lines)
