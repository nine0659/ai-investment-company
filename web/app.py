"""
AI 투자 어시스턴트 웹 앱
FastAPI 기반 — 브라우저(PC/모바일)에서 24시간 접근 가능

실행: uvicorn web.app:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import logging
import os
import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

# ── 웹 암호 보호 (환경변수 WEB_PASSWORD 설정 시 활성화) ──────────
_WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

app = FastAPI(title="AI 투자 어시스턴트", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR  = Path(__file__).parent
templates  = Jinja2Templates(directory=str(_BASE_DIR / "templates"))
_security  = HTTPBasic(auto_error=False)


def _check_auth(credentials: HTTPBasicCredentials | None = Depends(_security)):
    if not _WEB_PASSWORD:
        return  # 비밀번호 미설정 시 인증 없이 접근 허용
    if credentials is None or credentials.password != _WEB_PASSWORD:
        raise HTTPException(
            status_code=401,
            detail="인증이 필요합니다",
            headers={"WWW-Authenticate": "Basic realm='AI 투자 어시스턴트'"},
        )


# ── 유틸 ────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_via_thread(target_fn, *args) -> StreamingResponse:
    """블로킹 제너레이터를 SSE StreamingResponse로 래핑.
    target_fn(*args) 는 queue.Queue에 (type, content) 튜플을 put 해야 한다.
    """
    q: queue.Queue = queue.Queue()

    def run():
        try:
            target_fn(q, *args)
        except Exception as e:
            q.put(("error", str(e)))
        finally:
            q.put(("__done__", None))

    threading.Thread(target=run, daemon=True).start()

    async def generate():
        loop = asyncio.get_event_loop()
        while True:
            try:
                msg_type, content = await loop.run_in_executor(None, q.get, True, 0.1)
                if msg_type == "__done__":
                    break
                yield _sse({"type": msg_type, "content": content or ""})
            except queue.Empty:
                await asyncio.sleep(0.05)
            except Exception:
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 페이지 ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, _: None = Depends(_check_auth)):
    return templates.TemplateResponse(request, "index.html", {
        "has_password": bool(_WEB_PASSWORD),
    })


# ── REST API ─────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    now = datetime.now(_KST)
    try:
        from utils.market_calendar import is_krx_trading_day, get_holiday_name
        is_trading_day = is_krx_trading_day(now.date())
        holiday = get_holiday_name(now.date())
    except Exception:
        is_trading_day = now.weekday() < 5
        holiday = ""
    market_open = is_trading_day and (9, 0) <= (now.hour, now.minute) <= (15, 35)
    return {
        "ok": True,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market": "open" if market_open else "closed",
        "trading_day": is_trading_day,
        "holiday": holiday or None,
    }


@app.get("/api/briefings")
async def get_briefings(limit: int = 20):
    """최근 브리핑 목록."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT date, run_type, ceo_report, market_direction, created_at "
                    "FROM reports ORDER BY created_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).fetchall()
        briefings = [
            {
                "date": r[0], "run_type": r[1],
                "preview": (r[2] or "")[:200],
                "full": r[2] or "",
                "market_direction": r[3], "created_at": r[4],
            }
            for r in rows
        ]
        return {"briefings": briefings}
    except Exception as e:
        logger.warning("briefings DB 오류: %s", e)
        return {"briefings": []}


@app.get("/api/portfolio")
async def get_portfolio_api():
    """보유 포지션 + 현재가 손익."""
    try:
        from services.portfolio_service import calculate_pnl
        from clients.kis_client import KISClient
        try:
            kis = KISClient()
        except Exception:
            kis = None
        enriched = calculate_pnl(kis)
        return {"positions": enriched}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/api/watchlist")
async def get_watchlist_api(prices: bool = False):
    """관심종목. prices=true 일 때만 KIS 실시간 가격 조회 (기본은 빠른 응답 우선)."""
    try:
        from services.watchlist_service import get_watchlist
        items = get_watchlist()
        if prices and items:
            try:
                from clients.kis_client import KISClient
                import asyncio
                loop = asyncio.get_event_loop()
                kis = KISClient()
                def _fetch_prices():
                    for item in items:
                        try:
                            pd = kis.get_stock_price(item["code"], market=None)
                            if pd:
                                item["current_price"] = pd.get("price", 0)
                                item["change_pct"]    = pd.get("change_pct", 0)
                        except Exception:
                            pass
                await loop.run_in_executor(None, _fetch_prices)
            except Exception:
                pass
        return {"items": items}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.get("/api/price/{code}")
async def get_price_api(code: str):
    """종목 현재가·밸류에이션."""
    try:
        from clients.kis_client import KISClient
        from services.research_service import resolve_code
        name = ""
        if not code.isdigit():
            resolved, name = resolve_code(code)
            if not resolved:
                return {"error": f"'{code}' 종목을 찾지 못했습니다"}
            code = resolved
        else:
            _, name = resolve_code(code)
        kis  = KISClient()
        data = kis.get_stock_price(code, market=None)
        return {**(data or {}), "code": code, "name": name}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/thesis")
async def get_thesis_api():
    """현재 활성 투자관."""
    try:
        from services.thesis_service import get_active_thesis
        thesis = get_active_thesis()
        if not thesis:
            return {"thesis": None, "message": "투자관 없음 — python main.py --type thesis 실행 필요"}
        return {"thesis": thesis}
    except Exception as e:
        return {"thesis": None, "error": str(e)}


@app.get("/api/nav")
async def get_nav_api(days: int = 30):
    """포트폴리오 NAV 이력 + 최신 현황."""
    try:
        from services.nav_service import get_nav_history, get_latest_nav
        history = get_nav_history(days)
        latest  = get_latest_nav()
        return {"latest": latest, "history": history}
    except Exception as e:
        return {"latest": None, "history": [], "error": str(e)}


@app.get("/api/attribution")
async def get_attribution_api(limit: int = 8):
    """주간 귀인 분석 이력."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT week_end, macro_score, sector_score, stock_score,
                           timing_score, thesis_score, total_score, key_learnings
                    FROM attribution_log ORDER BY week_end DESC LIMIT :lim
                """),
                {"lim": limit},
            ).fetchall()
        items = [
            {
                "week_end": r[0], "macro": r[1], "sector": r[2], "stock": r[3],
                "timing": r[4], "thesis": r[5], "total": r[6], "key_learnings": r[7],
            }
            for r in rows
        ]
        return {"attributions": items}
    except Exception as e:
        return {"attributions": [], "error": str(e)}


@app.get("/api/strategy")
async def get_strategy_api():
    """최신 주간 전략 리포트."""
    try:
        from services.strategy_service import get_latest_strategy_report, get_latest_strategy_summary
        summary = get_latest_strategy_summary()
        report  = get_latest_strategy_report()
        return {"summary": summary, "report": report}
    except Exception as e:
        return {"summary": "", "report": "", "error": str(e)}


@app.get("/api/orders")
async def get_orders_api(limit: int = 50):
    """주문 이력 조회."""
    try:
        from services.trading_service import get_order_history
        orders = get_order_history(limit)
        return {"orders": orders, "total": len(orders)}
    except Exception as e:
        return {"orders": [], "total": 0, "error": str(e)}


@app.get("/api/balance")
async def get_balance_api():
    """KIS 계좌 잔고."""
    from config.settings import KIS_IS_REAL
    mode = "real" if KIS_IS_REAL else "paper"
    try:
        from clients.kis_client import KISClient
        kis  = KISClient()
        data = kis.get_account_balance()
        return {**data, "mode": mode}
    except Exception as e:
        return {"cash": 0, "total_eval": 0, "purchase_amt": 0, "holdings": [], "mode": mode, "error": str(e)}


# ── 스트리밍 API ─────────────────────────────────────────────────

_CHAT_SYSTEM = """당신은 AI 투자 어시스턴트입니다.
한국·미국 주식시장 전문 AI로, 투자자의 모든 질문에 명확하고 구체적으로 답합니다.

[핵심 규칙]
- 구체적 질문 → 구체적 답변 (수치, 근거 포함). 모호한 표현 금지.
- 투자 의견에는 항상 근거 명시. "좋아 보인다" 금지.
- 특정 기업 심층 분석은: "리서치 탭에서 [종목코드] 입력 후 분석하세요" 안내.
- 한국어로 응답. 숫자는 한국 단위(억, 조) 사용.
- 모르면 "모른다"고 말할 것. 추측 금지.
"""


def _chat_worker(q: queue.Queue, message: str, history: list):
    from clients.openai_client import chat_stream
    try:
        for chunk in chat_stream(_CHAT_SYSTEM, message, history=history, max_tokens=2000):
            q.put(("chunk", chunk))
    except Exception as e:
        q.put(("error", str(e)))


@app.post("/api/chat")
async def chat_endpoint(body: dict):
    """AI 채팅 스트리밍."""
    message = (body.get("message") or "").strip()
    history = body.get("history", [])
    if not message:
        async def err():
            yield _sse({"type": "error", "content": "메시지를 입력하세요"})
        return StreamingResponse(err(), media_type="text/event-stream")
    return _stream_via_thread(_chat_worker, message, history)


def _research_worker(q: queue.Queue, query: str):
    from services.research_service import resolve_code, gather_company_data
    from agents.research_agent import _SYSTEM, build_context
    from clients.openai_client import chat_stream

    q.put(("status", f"🔍 '{query}' 검색 중..."))
    code, name = resolve_code(query)
    if not code:
        q.put(("error", f"'{query}' 종목을 찾지 못했습니다. 6자리 코드로 다시 시도해 주세요."))
        return

    q.put(("status", f"📊 {name}({code}) 데이터 수집 중 (KIS·DART·yfinance)..."))
    data = gather_company_data(code, name)

    q.put(("status", "🤖 AI 분석 중... (30~60초 소요)"))
    context = build_context(data)

    q.put(("start", f"# {name}({code}) 투자 분석 리포트\n\n"))
    for chunk in chat_stream(_SYSTEM, context, max_tokens=3000):
        q.put(("chunk", chunk))


@app.post("/api/research")
async def research_endpoint(body: dict):
    """기업 리서치 스트리밍."""
    query = (body.get("query") or "").strip()
    if not query:
        async def err():
            yield _sse({"type": "error", "content": "종목코드 또는 회사명을 입력하세요"})
        return StreamingResponse(err(), media_type="text/event-stream")
    return _stream_via_thread(_research_worker, query)


@app.post("/api/search")
async def search_companies_api(body: dict):
    """회사명으로 종목 검색."""
    query = (body.get("query") or "").strip()
    if not query:
        return {"results": []}
    try:
        from services.research_service import search_companies
        return {"results": search_companies(query)}
    except Exception as e:
        return {"results": [], "error": str(e)}


@app.post("/api/portfolio/add")
async def add_portfolio_api(body: dict):
    """보유 종목 추가 또는 추가매수."""
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    qty  = body.get("quantity") or body.get("qty")
    avg  = body.get("avg_price") or body.get("price")
    if not code or not name or not qty or not avg:
        return {"ok": False, "error": "code·name·quantity·avg_price는 필수입니다"}
    try:
        from services.portfolio_service import add_position
        row_id = add_position(
            code=code, name=name,
            quantity=int(qty), avg_price=float(avg),
            timeframe=body.get("timeframe", "short"),
            sector=body.get("sector") or None,
            target_price=float(body["target_price"]) if body.get("target_price") else None,
            stop_price=float(body["stop_price"]) if body.get("stop_price") else None,
            memo=body.get("memo") or None,
        )
        return {"ok": True, "id": row_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/portfolio/close")
async def close_portfolio_api(body: dict):
    """종목 매도 (전량 또는 부분)."""
    code = (body.get("code") or "").strip()
    exit_price  = body.get("exit_price")
    partial_qty = body.get("partial_qty")
    if not code:
        return {"ok": False, "error": "code 필수"}
    try:
        from services.portfolio_service import close_position
        result = close_position(
            code=code,
            exit_price=float(exit_price) if exit_price else None,
            partial_qty=int(partial_qty) if partial_qty else None,
        )
        return {"ok": bool(result), "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.put("/api/portfolio/{code}")
async def update_portfolio_api(code: str, body: dict):
    """포지션 정보 업데이트 (목표가·손절가·메모·전략)."""
    try:
        from services.portfolio_service import update_position
        allowed = {
            "target_price": lambda v: float(v) if v else None,
            "stop_price":   lambda v: float(v) if v else None,
            "memo":         lambda v: str(v) if v else None,
            "timeframe":    lambda v: str(v) if v else None,
            "sector":       lambda v: str(v) if v else None,
        }
        updates = {}
        for k, cast in allowed.items():
            if k in body:
                updates[k] = cast(body[k])
        ok = update_position(code, **updates) if updates else False
        return {"ok": ok}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/portfolio/summary")
async def get_portfolio_summary_api():
    """포트폴리오 전체 요약 통계."""
    try:
        from services.portfolio_service import get_portfolio_summary, calculate_pnl
        from clients.kis_client import KISClient
        try:
            kis = KISClient()
        except Exception:
            kis = None
        pnl = calculate_pnl(kis)
        summary = get_portfolio_summary(pnl)
        return summary
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/portfolio/history")
async def get_portfolio_history_api(limit: int = 50):
    """매매 이력 조회."""
    try:
        from services.portfolio_service import get_portfolio_history
        return {"trades": get_portfolio_history(days=365)}
    except Exception as e:
        return {"trades": [], "error": str(e)}


@app.post("/api/watchlist")
async def add_watchlist_api(body: dict):
    """워치리스트 종목 추가."""
    code   = (body.get("code") or "").strip()
    name   = (body.get("name") or "").strip()
    if not code or not name:
        return {"ok": False, "error": "code와 name은 필수입니다"}
    try:
        from services.watchlist_service import add_to_watchlist
        row_id = add_to_watchlist(
            code=code, name=name,
            target_entry=body.get("target_entry"),
            timeframe=body.get("timeframe", "short"),
            reason=body.get("reason"),
            trigger_type=body.get("trigger_type", "price_below"),
        )
        return {"ok": True, "id": row_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/api/watchlist/{code}")
async def remove_watchlist_api(code: str):
    """워치리스트 종목 제거."""
    try:
        from services.watchlist_service import remove_from_watchlist
        removed = remove_from_watchlist(code)
        return {"ok": removed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/alerts")
async def get_alerts_api(days: int = 7):
    """최근 알림 이력 (alert_notifications 테이블)."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, date, alert_type, code, name, message, created_at "
                    "FROM alert_notifications "
                    "WHERE date >= :cutoff "
                    "ORDER BY created_at DESC LIMIT 100"
                ),
                {"cutoff": cutoff},
            ).fetchall()
        alerts = [
            {"id": r[0], "date": r[1], "alert_type": r[2],
             "code": r[3], "name": r[4], "message": r[5], "created_at": r[6]}
            for r in rows
        ]
        return {"alerts": alerts}
    except Exception as e:
        logger.warning("알림 조회 실패: %s", e)
        return {"alerts": []}


@app.get("/api/backtest")
async def backtest_api(days: int = 20):
    """AI 추천 종목 백테스트 결과."""
    try:
        from services.backtest_service import get_recommendation_backtest
        return get_recommendation_backtest(days=days)
    except Exception as e:
        return {"error": str(e), "items": [], "stats": {}}


@app.get("/api/performance")
async def performance_api():
    """실제 매매 성과 분석 (portfolio_history 기반)."""
    try:
        from services.backtest_service import get_portfolio_performance
        return get_portfolio_performance()
    except Exception as e:
        return {"error": str(e), "trades": [], "stats": {}}


@app.get("/api/tracker")
async def tracker_api(days: int = 30, _: None = Depends(_check_auth)):
    """AI 추천 종목 일별 성과 추적 데이터."""
    try:
        from services.recommendation_tracker_service import (
            get_tracking_summary, get_active_tracking_list,
        )
        summary = get_tracking_summary(days=days)
        active  = get_active_tracking_list()
        return {"summary": summary, "active": active}
    except Exception as e:
        return {"summary": {"items": [], "total": 0}, "active": [], "error": str(e)}


@app.get("/api/predictions")
async def predictions_api(days: int = 30, _: None = Depends(_check_auth)):
    """AI 시장 방향 예측 정확도 통계."""
    try:
        from services.market_prediction_service import get_prediction_stats
        stats = get_prediction_stats(days=days)
        return stats
    except Exception as e:
        return {"total": 0, "accuracy": 0.0, "items": [], "error": str(e)}


# ── 관리자 수동 실행 ──────────────────────────────────────────────

_run_lock = threading.Lock()
_run_status: dict = {"running": False, "last": None, "result": None}


_VALID_RUN_TYPES = ("pre", "close", "tracker", "thesis", "strategy", "attribution", "nav")


@app.post("/api/admin/run")
@app.get("/api/admin/run")
async def admin_run(type: str = "pre", _: None = Depends(_check_auth)):
    """수동 실행 트리거.
    type: pre|close|tracker|thesis|strategy|attribution|nav
    이미 실행 중이면 409, 202 즉시 반환 후 백그라운드 실행.
    """
    if type not in _VALID_RUN_TYPES:
        return JSONResponse({"ok": False, "msg": f"지원하지 않는 type. 가능: {_VALID_RUN_TYPES}"}, status_code=400)

    if not _run_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "msg": "이미 실행 중입니다"}, status_code=409)

    _run_status["running"] = True
    _run_status["result"]  = None

    def _worker():
        try:
            from datetime import datetime as dt
            kst  = ZoneInfo("Asia/Seoul")
            now  = dt.now(kst)
            date = now.strftime("%Y-%m-%d")

            if type == "tracker":
                from services.recommendation_tracker_service import run_daily_tracker
                from services.market_prediction_service import verify_predictions
                try:
                    from clients.kis_client import KISClient
                    kis = KISClient()
                except Exception:
                    kis = None
                result = run_daily_tracker(kis)
                verify_predictions(date)
                _run_status["result"] = f"tracker 완료: {result}"

            elif type in ("pre", "close"):
                from config.settings import RUN_TYPE_PRE, RUN_TYPE_CLOSE
                from graph.investment_graph import run_pipeline
                run_type = RUN_TYPE_PRE if type == "pre" else RUN_TYPE_CLOSE
                run_pipeline(run_type)
                _run_status["result"] = f"{type} 브리핑 완료"

            elif type == "thesis":
                from agents.thesis_agent import run_thesis
                run_thesis()
                _run_status["result"] = "월간 투자관 수립 완료"

            elif type == "strategy":
                from agents.strategy_agent import run_strategy
                run_strategy()
                _run_status["result"] = "주간 투자전략 완료"

            elif type == "attribution":
                from agents.attribution_agent import run_attribution
                run_attribution()
                _run_status["result"] = "주간 귀인분석 완료"

            elif type == "nav":
                from services.nav_service import record_nav
                try:
                    from clients.kis_client import KISClient
                    kis = KISClient()
                except Exception:
                    kis = None
                nav = record_nav(kis)
                _run_status["result"] = f"NAV 기록 완료: {nav}"

            _run_status["last"] = dt.now(kst).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error("[admin/run] 실패: %s", e)
            _run_status["result"] = f"오류: {e}"
        finally:
            _run_status["running"] = False
            _run_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return JSONResponse({"ok": True, "msg": f"{type} 실행 시작됨 — /api/admin/status 로 확인"}, status_code=202)


@app.get("/api/admin/status")
async def admin_status(_: None = Depends(_check_auth)):
    """수동 실행 진행 상태 조회."""
    return _run_status
