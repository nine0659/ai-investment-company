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
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_DB  = Path(__file__).parent.parent / "data" / "database.sqlite3"

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


def _db_conn():
    _DB.parent.mkdir(exist_ok=True)
    return sqlite3.connect(str(_DB))


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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "has_password": bool(_WEB_PASSWORD),
    })


# ── REST API ─────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    now = datetime.now(_KST)
    # 장 운영 시간: 평일 09:00~15:35
    is_weekday = now.weekday() < 5
    market_open = is_weekday and (9, 0) <= (now.hour, now.minute) <= (15, 35)
    return {
        "ok": True,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market": "open" if market_open else "closed",
    }


@app.get("/api/briefings")
async def get_briefings(limit: int = 20):
    """최근 브리핑 목록."""
    try:
        with _db_conn() as c:
            rows = c.execute(
                "SELECT date, run_type, ceo_report, market_direction, "
                "created_at FROM reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
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
        from services.portfolio_service import get_portfolio, calculate_pnl
        from clients.kis_client import KISClient
        kis = KISClient()
        positions = get_portfolio()
        enriched = []
        for p in positions:
            try:
                pnl = calculate_pnl(p, kis)
                enriched.append({**p, **pnl})
            except Exception:
                enriched.append(p)
        return {"positions": enriched}
    except Exception as e:
        return {"positions": [], "error": str(e)}


@app.get("/api/watchlist")
async def get_watchlist_api():
    """관심종목."""
    try:
        from services.watchlist_service import get_watchlist
        from clients.kis_client import KISClient
        items = get_watchlist()
        try:
            kis = KISClient()
            for item in items:
                pd = kis.get_stock_price(item["code"], market=None)
                if pd:
                    item["current_price"] = pd.get("price", 0)
                    item["change_pct"]    = pd.get("change_pct", 0)
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


@app.get("/api/balance")
async def get_balance_api():
    """KIS 계좌 잔고."""
    try:
        from clients.kis_client import KISClient
        from config.settings import KIS_IS_REAL
        kis  = KISClient()
        data = kis.get_account_balance()
        return {**data, "mode": "real" if KIS_IS_REAL else "paper"}
    except Exception as e:
        return {"error": str(e)}


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
