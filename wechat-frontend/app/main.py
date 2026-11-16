"""FastAPI gateway + custom UI for local WeChat message capture (receive-only)."""
from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .reader_client import ReaderError, reader
from .settings import settings
from .sidebar import active_chat, summarize_chat
from .suggest import suggest_replies
from .config_api import router as config_router
from .archive import manager as archive_manager, router as archive_router
from .agents_api import router as agents_router

COOKIE = "wf_gateway"
STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wechat-frontend")

app = FastAPI(
    title="消息工作台 Gateway",
    description="Receive-only gateway over local wechat-realtime-reader. No send path.",
    version="1.2.2",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def _token_ok(provided: str) -> bool:
    expected = settings.gateway_token
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _extract_token(request: Request, authorization: Optional[str]) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    cookie = request.cookies.get(COOKIE)
    if cookie:
        return cookie
    return ""


async def require_auth(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    tok = _extract_token(request, authorization)
    if not _token_ok(tok):
        raise HTTPException(
            status_code=401,
            detail="unauthorized: provide Authorization: Bearer <gateway-token>",
        )


@app.on_event("startup")
async def _startup() -> None:
    settings.reload()
    if settings.is_non_loopback() and not settings.gateway_token:
        raise RuntimeError("refusing to bind non-loopback without gateway-token")
    await reader.start()
    try:
        await archive_manager.start()  # no-op unless the user enabled the archive
    except Exception as e:
        log.warning("archive start failed: %s", type(e).__name__)
    log.info(
        "gateway listening intent host=%s port=%s reader=%s non_loopback=%s",
        settings.bind_host,
        settings.bind_port,
        settings.reader_base,
        settings.is_non_loopback(),
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    await archive_manager.stop()
    await reader.stop()


def _safe_next(nxt: Optional[str]) -> str:
    nxt = (nxt or "/").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    return nxt


@app.get("/login")
async def login_page(next: Optional[str] = None):
    import html as _html
    nxt = _html.escape(_safe_next(next), quote=True)
    html = (
        "<!doctype html><meta charset=\"utf-8\"><title>登录 消息助手</title>"
        "<body style=\"font:15px system-ui;padding:40px;max-width:520px;color:#222\">"
        "<h2>消息助手</h2>"
        "<p>粘贴网关令牌（文件 <code>~/.config/wechat-frontend/gateway-token</code>）。"
        "令牌仅写入本机 HttpOnly Cookie。</p>"
        "<form method=\"post\" action=\"/auth/session\">"
        f"<input type=\"hidden\" name=\"next\" value=\"{nxt}\" />"
        "<input name=\"token\" type=\"password\" required placeholder=\"gateway-token\" "
        "style=\"width:100%;padding:10px;font:inherit;border:1px solid #ccc;border-radius:8px\" />"
        "<button style=\"margin-top:12px;padding:8px 14px\">进入</button>"
        "</form>"
        "<p style=\"color:#666;font-size:12px\">飞书秒搭请用 "
        "<code>Authorization: Bearer …</code>，无需打开此页。</p>"
        "</body>"
    )
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.post("/auth/session")
async def auth_session(request: Request):
    ctype = request.headers.get("content-type", "")
    token = ""
    nxt = "/"
    if "application/json" in ctype:
        data = await request.json()
        token = str((data or {}).get("token") or "")
        nxt = str((data or {}).get("next") or "/")
    else:
        form = await request.form()
        token = str(form.get("token") or "")
        nxt = str(form.get("next") or "/")
    if not _token_ok(token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    resp = RedirectResponse(_safe_next(nxt), status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=86400 * 365, path="/")
    return resp


@app.get("/")
async def index(request: Request):
    # The old two-pane workbench is retired; the companion panel (/sidebar) is the only UI.
    if not _token_ok(request.cookies.get(COOKIE) or ""):
        return RedirectResponse("/login?next=/sidebar", status_code=302)
    return RedirectResponse("/sidebar", status_code=302)


@app.get("/sidebar")
async def sidebar_page(request: Request):
    if not _token_ok(request.cookies.get(COOKIE) or ""):
        return RedirectResponse("/login?next=/sidebar", status_code=302)
    return FileResponse(STATIC_DIR / "sidebar.html", headers={"Cache-Control": "no-store"})


@app.get("/settings")
async def settings_page(request: Request):
    if not _token_ok(request.cookies.get(COOKIE) or ""):
        return RedirectResponse("/login?next=/settings", status_code=302)
    return FileResponse(STATIC_DIR / "settings.html", headers={"Cache-Control": "no-store"})


# In-app Settings API (AI provider / model / gateway token / data source). All token-gated.
app.include_router(config_router, dependencies=[Depends(require_auth)])
# Local message archive (opt-in, default OFF). Token-gated like everything else.
app.include_router(archive_router, dependencies=[Depends(require_auth)])
# Agent tools (MCP) auto-registration status / toggle / undo. Token-gated.
app.include_router(agents_router, dependencies=[Depends(require_auth)])


@app.get("/api/health", dependencies=[Depends(require_auth)])
async def health():
    try:
        status = await reader.get_json("/api/status")
        sessions = await reader.get_json("/api/sessions", {"limit": 1})
        freshness = sessions.get("freshness") or status.get("freshness")
        return {
            "ok": True,
            "receive_only": True,
            "reader": settings.reader_base,
            "freshness": freshness,
            "self": status.get("self"),
            "bind": {"host": settings.bind_host, "port": settings.bind_port},
            "title": settings.title,
        }
    except ReaderError as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status if e.status >= 400 else 503)
    except Exception as e:
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status():
    try:
        return await reader.get_json("/api/status")
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def sessions(keyword: Optional[str] = None, kind: Optional[str] = None, limit: int = Query(300, ge=1, le=1000)):
    try:
        return await reader.get_json("/api/sessions", {"keyword": keyword, "kind": kind, "limit": limit})
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/api/contacts", dependencies=[Depends(require_auth)])
async def contacts(keyword: Optional[str] = None, kind: Optional[str] = None, limit: int = Query(30, ge=1, le=200)):
    try:
        return await reader.get_json("/api/contacts", {"keyword": keyword, "kind": kind, "limit": limit})
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/api/messages", dependencies=[Depends(require_auth)])
async def messages(
    chat: str = Query(..., min_length=1),
    limit: int = Query(60, ge=1, le=200),
    before: Optional[str] = None,
    after: Optional[str] = None,
    before_seq: Optional[str] = None,
    sender: Optional[str] = None,
    keyword: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    types: Optional[str] = None,
    raw: Optional[str] = None,
):
    params = {
        "chat": chat,
        "limit": limit,
        "before": before or before_seq,
        "after": after,
        "sender": sender,
        "keyword": keyword,
        "since": since,
        "until": until,
        "types": types,
        "raw": raw,
    }
    try:
        return await reader.get_json("/api/messages", params)
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/api/search", dependencies=[Depends(require_auth)])
async def search(
    keyword: Optional[str] = None,
    sender: Optional[str] = None,
    chat: Optional[str] = None,
    limit: int = Query(40, ge=1, le=200),
    before: Optional[str] = None,
    after: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    if not (keyword or sender):
        raise HTTPException(400, "keyword or sender is required")
    try:
        return await reader.get_json("/api/search", {
            "keyword": keyword, "sender": sender, "chat": chat, "limit": limit,
            "before": before, "after": after, "since": since, "until": until,
        })
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/api/members", dependencies=[Depends(require_auth)])
async def members(chat: str = Query(..., min_length=1)):
    try:
        return await reader.get_json("/api/members", {"chat": chat})
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)


async def _proxy_bytes(path: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Response:
    try:
        status, body, headers = await reader.request("GET", path, params=params, timeout=timeout)
    except ReaderError as e:
        raise HTTPException(e.status, e.detail)
    if status >= 400:
        return Response(content=body, status_code=status, media_type=headers.get("content-type", "text/plain"))
    return Response(
        content=body,
        status_code=status,
        media_type=headers.get("content-type", "application/octet-stream"),
        headers={k: v for k, v in headers.items() if k.lower() != "content-type"},
    )


@app.get("/api/avatar/{user}", dependencies=[Depends(require_auth)])
async def avatar(user: str):
    return await _proxy_bytes(f"/api/avatar/{user}", {})


@app.get("/api/image", dependencies=[Depends(require_auth)])
async def image(chat: str = Query(...), id: Optional[str] = None, local_id: Optional[str] = None, t: Optional[str] = None, size: str = Query("thumb")):
    if size not in ("thumb", "full"):
        size = "thumb"
    # Bounded wait: a cache miss must fail fast (UI shows a placeholder) instead of hanging.
    return await _proxy_bytes(
        "/api/image",
        {"chat": chat, "id": id, "local_id": local_id, "t": t, "size": size},
        timeout=25.0 if size == "full" else 12.0,
    )


@app.get("/api/video", dependencies=[Depends(require_auth)])
async def video(id: str = Query(...), t: Optional[str] = None, cover: Optional[str] = None):
    return await _proxy_bytes("/api/video", {"id": id, "t": t, "cover": cover})


@app.get("/api/voice", dependencies=[Depends(require_auth)])
async def voice(chat: str = Query(...), local_id: str = Query(...), t: Optional[str] = None):
    return await _proxy_bytes("/api/voice", {"chat": chat, "local_id": local_id, "t": t})


@app.get("/api/ui-config", dependencies=[Depends(require_auth)])
async def get_ui_config():
    return settings.load_ui()


class UIConfigUpdate(BaseModel):
    theme: Optional[str] = None
    refresh_interval_ms: Optional[int] = Field(default=None, ge=800, le=60000)
    pinned: Optional[List[str]] = None
    hidden: Optional[List[str]] = None
    show_avatars: Optional[bool] = None
    show_unread_only: Optional[bool] = None
    density: Optional[str] = None
    session_kinds: Optional[List[str]] = None


@app.put("/api/ui-config", dependencies=[Depends(require_auth)])
async def put_ui_config(body: UIConfigUpdate):
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return settings.save_ui(data)


class SuggestBody(BaseModel):
    chat: str = Field(..., min_length=1)
    n_context: int = Field(default=20, ge=1, le=50)
    count: int = Field(default=3, ge=1, le=4)


def _ai_result(result: Dict[str, Any]):
    if result.get("error") == "AI not configured":
        return JSONResponse(result, status_code=501)
    if result.get("error"):
        code = int(result.get("status") or 502)
        return JSONResponse(result, status_code=code if 400 <= code < 600 else 502)
    return result


@app.get("/api/active-chat", dependencies=[Depends(require_auth)])
async def get_active_chat(top: int = Query(5, ge=1, le=20)):
    """Read-only estimate of the most recently opened chat (newest last_clear_unread_timestamp)."""
    try:
        return await active_chat(reader, top)
    except FileNotFoundError:
        raise HTTPException(503, "reader session snapshot not found (is wechat-reader serve running?)")
    except Exception as e:
        log.warning("active-chat failed: %s", type(e).__name__)
        raise HTTPException(503, f"session snapshot unreadable: {type(e).__name__}")


class SummarizeBody(BaseModel):
    chat: str = Field(..., min_length=1)
    n: int = Field(default=100, ge=10, le=200)
    days: Optional[int] = Field(default=None, ge=1, le=365)
    name: Optional[str] = Field(default=None, max_length=200)


@app.post("/api/summarize", dependencies=[Depends(require_auth)])
async def summarize(body: SummarizeBody):
    """AI summary of recent history with this chat. Read-only; returns text, never sends."""
    return _ai_result(await summarize_chat(reader, body.chat, body.n, body.days, body.name))


@app.post("/api/suggest", dependencies=[Depends(require_auth)])
async def suggest(body: SuggestBody):
    return _ai_result(await suggest_replies(reader, body.chat, body.n_context, body.count))


@app.get("/assets/{path:path}")
async def assets(path: str, request: Request, authorization: Optional[str] = Header(default=None)):
    tok = _extract_token(request, authorization)
    if not _token_ok(tok):
        raise HTTPException(401, "unauthorized")
    target = (STATIC_DIR / path).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)


@app.get("/static/{path:path}")
async def static_alias(path: str, request: Request, authorization: Optional[str] = Header(default=None)):
    return await assets(path, request, authorization)

