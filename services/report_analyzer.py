"""
services/report_analyzer.py

증권사 리포트·중앙은행 문서를 '본문까지' 읽고 구조화한다.

- 수집: 네이버 증권 리서치(본문 + PDF 전문), Fed·한국은행·ECB 원문 (clients/official_docs_client)
- 읽기: LLM이 문서별로 입장·핵심 주장·수치를 JSON으로 추출 (LLM은 추출만)
- 검증: 코드가 주장의 근거 문장(quote)과 수치가 **원문에 실제로 있는지** 대조해 없으면 버린다
        (프로젝트 원칙 2·3 — LLM이 만든 숫자·인용을 신뢰하지 않는다)
- 캐시: report_analysis 테이블. 같은 문서는 한 번만 읽는다(비용), 집계는 services/research_synthesis.py

리포트는 결론이 아니라 참고 재료다. 입장(stance)은 '그 문서가 말한 것'이지 '맞는 것'이 아니다.
"""
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 한 번 실행당 새로 읽을 문서 수 상한 (유형별) — 비용·시간 상한
CAPS = {"경제분석": 5, "투자전략": 4, "산업분석": 3, "공식문서": 6, "종목분석": 4}
PER_BROKER_MAX = 2
MIN_TEXT_CHARS = 400
LLM_INPUT_CHARS = 10000
STANCES = ("강세", "중립", "약세", "판단불가")
POLICY_TONES = ("매파", "중립", "비둘기", "해당없음")

_SYSTEM = """당신은 증권사 리포트와 중앙은행 문서를 읽고 구조화하는 리서치 보조입니다.
주어진 문서 본문에 **실제로 적힌 내용만** 추출합니다. 추측·상식 보충·외부 지식 사용 금지.

반드시 아래 형태의 JSON 하나만 출력하세요. 설명·주석·코드블록 금지:
{"stance": "...", "policy_tone": "...", "claims": [{"claim": "...", "quote": "...", "theme": "...", "kind": "...", "stance": "..."}], "figures": [{"label": "...", "value": "...", "quote": "...", "kind": "..."}], "risks": ["..."], "horizon": "..."}

필드 설명:
- stance: 문서 전체가 주식시장(위험자산)에 대해 말하는 시각. 강세|중립|약세|판단불가 중 하나.
- policy_tone: 중앙은행 문서일 때만 매파|중립|비둘기, 그 외에는 해당없음.
- claims: 핵심 주장 최대 5개. claim=한 문장 요약, quote=근거가 된 원문 문장을 글자 그대로 복사(80자 이내),
  theme=허용 테마 중 하나, kind=전망(작성자의 판단·예상·권고) 또는 사실(이미 일어난 시황·지표 서술),
  stance=kind가 전망일 때만 강세|중립|약세(그 전망이 해당 테마·시장에 우호적이면 강세), 사실이면 빈 문자열.
  시황 사실 나열보다 작성자의 판단·전망을 우선 뽑을 것.
- figures: 수치 최대 5개. label=수치의 의미, value=원문 표기 그대로, quote=그 수치가 나온 원문 구절 그대로,
  kind=전망치(목표·예상·추정 수치) 또는 실적치(이미 발표된 값·시황 종가). 전망·목표·추정 수치를 우선.
- risks: 문서가 직접 언급한 위험 요인. horizon: 단기|중기|장기|불명.
규칙: quote와 value는 한 글자도 바꾸지 말고 원문에서 복사. 원문에 없으면 그 항목을 넣지 말 것. 수치를 계산·환산하지 말 것."""


# ── 검증 (순수 함수) ───────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s ]+", "", (s or "")).lower()


OTHER_THEME = "기타"   # 시장·매크로 테마에 속하지 않는 개별 종목·산업 이야기 (집계에서 제외)


def _themes_allowed() -> tuple[str, ...]:
    from services.report_digest import THEMES
    return tuple(THEMES.keys()) + (OTHER_THEME,)


def parse_json_object(raw: str) -> dict | None:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def verify_analysis(raw: dict, source_text: str) -> dict:
    """LLM 추출 결과 중 원문에서 확인되는 것만 남긴다.

    - claim: quote가 원문에 (공백 무시하고) 포함돼야 함 — 아니면 주장째 폐기
    - figure: value가 원문에 포함되고 quote도 원문에 있어야 함 — 아니면 폐기
    - 검증된 claim이 하나도 없으면 stance를 '판단불가'로 강등 (근거 없는 입장 방지)
    """
    src = _norm(source_text)
    allowed_themes = set(_themes_allowed())

    claims = []
    for c in (raw.get("claims") or [])[:8]:
        if not isinstance(c, dict):
            continue
        claim, quote = str(c.get("claim") or "").strip(), str(c.get("quote") or "").strip()
        if not (claim and len(_norm(quote)) >= 8 and _norm(quote) in src):
            continue
        kind = c.get("kind") if c.get("kind") in ("전망", "사실") else "사실"
        theme = c.get("theme") if c.get("theme") in allowed_themes else ""
        cst = c.get("stance") if (kind == "전망" and c.get("stance") in ("강세", "중립", "약세")) else ""
        claims.append({"claim": claim[:200], "quote": quote[:120], "theme": theme, "kind": kind, "stance": cst})
    claims = claims[:5]

    figures = []
    for f in (raw.get("figures") or [])[:8]:
        if not isinstance(f, dict):
            continue
        label, value, quote = (str(f.get(k) or "").strip() for k in ("label", "value", "quote"))
        if label and value and _norm(value) in src and (not quote or _norm(quote) in src):
            fk = f.get("kind") if f.get("kind") in ("전망치", "실적치") else "실적치"
            figures.append({"label": label[:80], "value": value[:40], "kind": fk, "quote": quote[:100]})
    figures = figures[:5]

    stance = raw.get("stance") if raw.get("stance") in STANCES else "판단불가"
    if not claims:
        stance = "판단불가"
    tone = raw.get("policy_tone") if raw.get("policy_tone") in POLICY_TONES else "해당없음"
    horizon = raw.get("horizon") if raw.get("horizon") in ("단기", "중기", "장기", "불명") else "불명"

    return {
        "stance": stance,
        "policy_tone": tone,
        "themes": sorted({c["theme"] for c in claims if c["theme"]}),
        "claims": claims,
        "figures": figures,
        "risks": [str(r)[:120] for r in (raw.get("risks") or [])[:4] if r],
        "horizon": horizon,
        "n_claims_dropped": max(0, len(raw.get("claims") or []) - len(claims)),
    }


# ── 선택 (순수 함수) ───────────────────────────────────────────────

def select_pending(candidates: list[dict], analyzed_links: set[str], caps: dict | None = None,
                   per_broker: int = PER_BROKER_MAX) -> list[dict]:
    """아직 읽지 않은 문서 중 유형별 상한·증권사별 상한을 지켜 고른다 (최신순)."""
    caps = caps or CAPS
    picked: list[dict] = []
    per_type: dict[str, int] = {}
    per_firm: dict[tuple, int] = {}
    for d in sorted(candidates, key=lambda x: x.get("date") or "", reverse=True):
        link, t, firm = d.get("link"), d.get("type"), d.get("firm")
        if not link or link in analyzed_links or t not in caps:
            continue
        if per_type.get(t, 0) >= caps[t] or per_firm.get((t, firm), 0) >= per_broker:
            continue
        per_type[t] = per_type.get(t, 0) + 1
        per_firm[(t, firm)] = per_firm.get((t, firm), 0) + 1
        picked.append(d)
    return picked


# ── 본문 확보 + 추출 ───────────────────────────────────────────────

def load_document_text(doc: dict) -> tuple[str, dict]:
    """(본문 텍스트, 부가정보). 네이버는 본문+PDF, 공식문서는 원문 페이지."""
    extra: dict = {}
    if doc.get("type") == "공식문서":
        from clients.official_docs_client import fetch_official_text
        return fetch_official_text(doc), extra

    from clients.securities_report_client import fetch_report_detail, fetch_pdf_text
    det = fetch_report_detail(doc["type"], doc.get("id"))
    extra = {k: det.get(k) for k in ("opinion", "goal_price", "price_at_write")}
    body = det.get("body") or ""
    pdf = fetch_pdf_text(det.get("pdf_url") or "") if len(body) < 6000 else ""
    return (body + "\n" + pdf).strip(), extra


def extract(doc: dict, source_text: str) -> dict | None:
    """LLM 추출 + 코드 검증. LLM 실패·JSON 불량이면 None."""
    from clients.openai_client import chat
    capped = source_text[:LLM_INPUT_CHARS]
    user = (f"[문서 정보] 출처: {doc.get('firm')} | 유형: {doc.get('type')} | 제목: {doc.get('title')} | 날짜: {doc.get('date')}\n"
            f"[허용 테마] {', '.join(_themes_allowed())}\n\n[본문]\n{capped}")
    reply = chat(_SYSTEM, user, max_tokens=1700)
    raw = parse_json_object(reply)
    if raw is None:
        logger.warning("[리포트분석] JSON 파싱 실패 (%s): %.120s", doc.get("title"), (reply or "").replace("\n", " "))
        return None
    return verify_analysis(raw, capped)


# ── 저장/조회 ──────────────────────────────────────────────────────

def _source_of(doc: dict) -> str:
    return "official" if doc.get("type") == "공식문서" else "naver"


def analyzed_links() -> set[str]:
    try:
        from db.database import get_conn
        with get_conn() as conn:
            rows = conn.execute(text("SELECT link FROM report_analysis WHERE analysis_json IS NOT NULL")).fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning("[리포트분석] 캐시 조회 실패: %s", e)
        return set()


def save(doc: dict, extra: dict, analysis: dict | None, text_chars: int) -> None:
    try:
        from db.database import get_conn
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO report_analysis (source, link, rtype, firm, title, code, stock, report_date, "
                    "opinion, goal_price, price_at_write, stance, analysis_json, text_chars) "
                    "VALUES (:s,:l,:t,:f,:ti,:c,:st,:d,:o,:g,:pg,:sn,:aj,:tc) "
                    "ON CONFLICT (source, link) DO UPDATE SET stance=:sn, analysis_json=:aj, text_chars=:tc, "
                    "opinion=:o, goal_price=:g, price_at_write=:pg"
                ),
                {"s": _source_of(doc), "l": doc["link"], "t": doc.get("type"), "f": doc.get("firm"),
                 "ti": doc.get("title"), "c": doc.get("code") or "", "st": doc.get("stock") or "",
                 "d": doc.get("date"), "o": extra.get("opinion"), "g": extra.get("goal_price"),
                 "pg": extra.get("price_at_write"),
                 "sn": (analysis or {}).get("stance"),
                 "aj": json.dumps(analysis, ensure_ascii=False) if analysis else None, "tc": text_chars},
            )
    except Exception as e:
        logger.warning("[리포트분석] 저장 실패 (%s): %s", doc.get("title"), e)


def _process(doc: dict) -> tuple[dict, str, dict, dict | None]:
    txt, extra = load_document_text(doc)
    extra["_chars"] = len(txt)
    if len(txt) < MIN_TEXT_CHARS:
        return doc, "no_text", extra, None
    try:
        return doc, "ok", extra, extract(doc, txt)
    except Exception as e:
        logger.warning("[리포트분석] LLM 추출 실패 (%s): %s", doc.get("title"), e)
        return doc, "llm_fail", extra, None


def analyze_new_documents(candidates: list[dict], deadline_s: float = 150.0, workers: int = 4) -> dict:
    """새 문서를 골라 읽고 저장. 예외를 밖으로 내지 않고 통계만 돌려준다."""
    stats = {"candidates": len(candidates), "selected": 0, "analyzed": 0, "no_text": 0, "failed": 0}
    try:
        todo = select_pending(candidates, analyzed_links())
        stats["selected"] = len(todo)
        if not todo:
            return stats
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_process, d) for d in todo]
            for f in as_completed(futs, timeout=deadline_s):
                try:
                    doc, status, extra, analysis = f.result()
                except Exception as e:
                    stats["failed"] += 1
                    logger.warning("[리포트분석] 작업 실패: %s", e)
                    continue
                if status == "ok" and analysis:
                    save(doc, extra, analysis, extra.get("_chars", 0))
                    stats["analyzed"] += 1
                elif status == "no_text":
                    stats["no_text"] += 1
                    save(doc, extra, {"stance": "판단불가", "policy_tone": "해당없음", "themes": [], "claims": [],
                                      "figures": [], "risks": [], "horizon": "불명", "n_claims_dropped": 0}, 0)
                else:
                    stats["failed"] += 1
                if time.time() - t0 > deadline_s:
                    break
    except Exception as e:  # as_completed timeout 포함
        logger.warning("[리포트분석] 중단: %s", e)
        stats["failed"] += 1
    logger.info("[리포트분석] %s", stats)
    return stats


def store_company_snapshots(reports: list[dict]) -> int:
    """보유 종목 종목분석 리포트의 투자의견·목표가(구조화 필드)를 LLM 없이 저장. 신규/갱신 건수."""
    from clients.securities_report_client import fetch_report_detail
    n = 0
    known = set()
    try:
        from db.database import get_conn
        with get_conn() as conn:
            known = {r[0] for r in conn.execute(text("SELECT link FROM report_analysis WHERE rtype='종목분석'")).fetchall()}
    except Exception:
        pass
    for rep in reports:
        if rep.get("link") in known:
            continue
        det = fetch_report_detail("종목분석", rep.get("id"))
        if not det:
            continue
        save(rep, {k: det.get(k) for k in ("opinion", "goal_price", "price_at_write")}, None, len(det.get("body") or ""))
        n += 1
    return n
