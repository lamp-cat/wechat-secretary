"""In-app Settings API: AI provider config, model listing/testing, gateway-token management,
WeChat data-source status and key maintenance helpers.

Security rules (enforced here):
- Every route is mounted behind require_auth (gateway token) in main.py.
- The AI api_key is NEVER returned; only a masked preview + a `set` boolean.
- The gateway token is only returned by an explicit POST {action:"reveal"} (never via GET/URL).
- Secrets are written atomically to 0600 files and never logged.
- Receive-only: nothing here can send a WeChat message.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .settings import settings

router = APIRouter()

COOKIE = "wf_gateway"
HOME = Path.home()
PLATFORM_ROOT = Path(os.environ.get("WF_PLATFORM_ROOT", str(HOME / "wechat-platform")))
READER_ROOT = PLATFORM_ROOT / "wechat-realtime-reader"
READER_BIN = READER_ROOT / "bin" / "wechat-reader"
READER_CFG_DIR = Path(os.environ.get("WECHAT_READER_CONFIG", str(HOME / ".config" / "wechat-realtime-reader")))
RECAPTURE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "recapture-keys.sh"
WECHAT_APP = Path("/Applications/WeChat.app")

_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")


# ----------------------------------------------------------------- helpers
def _write_secret_file(path: Path, text: str) -> None:
    """Atomic write with 0600 perms (dir 0700 if we create it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_ai_raw() -> Dict[str, Any]:
    try:
        p = settings.ai_config_file
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _mask(secret: str) -> str:
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) < 12:
        return "•" * 6
    return f"{s[:3]}…{s[-4:]}"


def _redact(text: str, *secrets_: str) -> str:
    out = text or ""
    for s in secrets_:
        if s and len(s) >= 4:
            out = out.replace(s, "***")
    return _HEX_RUN.sub("***", out)


def _norm_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "base_url 必须以 http:// 或 https:// 开头")
    return url


def _norm_proxy(p: Optional[str]) -> str:
    p = (p or "").strip()
    if p and not re.match(r"^(https?|socks5h?)://", p, re.I):
        raise HTTPException(400, "代理地址需形如 http://127.0.0.1:7897 或 socks5://127.0.0.1:7897")
    return p


def _client(proxy: str, timeout: float) -> httpx.AsyncClient:
    # trust_env=False: never pick up env/system proxy implicitly; an explicit proxy is opt-in.
    kw: Dict[str, Any] = {"timeout": httpx.Timeout(timeout, connect=10.0), "trust_env": False}
    if proxy:
        kw["proxy"] = proxy
    return httpx.AsyncClient(**kw)


def _public_ai(cfg: Dict[str, Any]) -> Dict[str, Any]:
    key = str(cfg.get("api_key") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": str(cfg.get("base_url") or "https://api.openai.com/v1"),
        "model": str(cfg.get("model") or ""),
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(cfg.get("max_tokens", 800)),
        "proxy": str(cfg.get("proxy") or ""),
        "api_key_set": bool(key.strip()),
        "api_key_masked": _mask(key),
        "file": str(settings.ai_config_file),
        "ready": bool(cfg.get("enabled")) and bool(key.strip()),
    }


# ----------------------------------------------------------------- AI config
@router.get("/api/ai-config")
async def get_ai_config():
    return _public_ai(_read_ai_raw())


class AIConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=1000)  # omit/null = keep
    clear_api_key: bool = False
    model: Optional[str] = Field(default=None, max_length=200)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=16, le=32000)
    proxy: Optional[str] = Field(default=None, max_length=300)


@router.put("/api/ai-config")
async def put_ai_config(body: AIConfigUpdate):
    cfg = _read_ai_raw()
    if body.enabled is not None:
        cfg["enabled"] = bool(body.enabled)
    if body.base_url is not None:
        cfg["base_url"] = _norm_base(body.base_url) or "https://api.openai.com/v1"
    if body.clear_api_key:
        cfg["api_key"] = ""
    elif body.api_key is not None and body.api_key.strip():
        cfg["api_key"] = body.api_key.strip()
    if body.model is not None:
        cfg["model"] = body.model.strip()
    if body.temperature is not None:
        cfg["temperature"] = float(body.temperature)
    if body.max_tokens is not None:
        cfg["max_tokens"] = int(body.max_tokens)
    if body.proxy is not None:
        cfg["proxy"] = _norm_proxy(body.proxy)
    cfg.setdefault(
        "_comment",
        "Managed by the in-app Settings page (/settings). OpenAI-compatible Chat Completions. Drafts only — never auto-sends.",
    )
    _write_secret_file(settings.ai_config_file, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    out = _public_ai(cfg)
    out["saved"] = True
    return out


class AIProbe(BaseModel):
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: Optional[str] = Field(default=None, max_length=1000)
    model: Optional[str] = Field(default=None, max_length=200)
    proxy: Optional[str] = Field(default=None, max_length=300)


def _resolve_probe(body: Optional[AIProbe]) -> Dict[str, str]:
    """Merge unsaved form values with the saved config. Guard: the saved key is only ever
    sent to the saved base_url/proxy — pointing it elsewhere requires re-entering the key."""
    saved = _read_ai_raw()
    s_base = str(saved.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    s_proxy = str(saved.get("proxy") or "")
    b = body or AIProbe()
    base = _norm_base(b.base_url) if b.base_url else s_base
    proxy = _norm_proxy(b.proxy) if b.proxy is not None else s_proxy
    key = (b.api_key or "").strip()
    if not key:
        if base != s_base or proxy != s_proxy:
            raise HTTPException(400, "更换了 base_url / 代理：请重新输入 API Key 再测试（已保存的 Key 只会发往已保存的地址）")
        key = str(saved.get("api_key") or "").strip()
    if not key:
        raise HTTPException(400, "尚未填写 API Key")
    model = (b.model or "").strip() or str(saved.get("model") or "")
    return {"base": base, "key": key, "model": model, "proxy": proxy}


async def _list_models(p: Dict[str, str]):
    t0 = time.monotonic()
    try:
        async with _client(p["proxy"], 20.0) as c:
            r = await c.get(f"{p['base']}/models", headers={"Authorization": f"Bearer {p['key']}"})
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"无法连接到服务商：{type(e).__name__}",
                             "hint": "国外服务商在国内通常需要填写代理，例如 http://127.0.0.1:7897"}, status_code=502)
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code >= 400:
        return JSONResponse({"ok": False, "status": r.status_code, "error": f"服务商返回 HTTP {r.status_code}",
                             "detail": _redact(r.text[:300], p["key"]),
                             "hint": "该服务商可能不提供 /models，请手动输入模型名"}, status_code=502)
    try:
        data = r.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "返回不是 JSON（该服务商可能不提供 /models），请手动输入模型名"}, status_code=502)
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and not isinstance(items, list):
        items = data.get("models")
    ids: List[str] = []
    for it in items or []:
        mid = it.get("id") or it.get("name") if isinstance(it, dict) else it
        if mid:
            ids.append(str(mid))
    ids = sorted(set(ids), key=str.lower)
    return {"ok": True, "models": ids, "count": len(ids), "latency_ms": ms, "base_url": p["base"]}


@router.get("/api/ai-models")
async def ai_models_saved():
    return await _list_models(_resolve_probe(None))


@router.post("/api/ai-models")
async def ai_models(body: AIProbe):
    return await _list_models(_resolve_probe(body))


@router.post("/api/ai-test")
async def ai_test(body: AIProbe):
    p = _resolve_probe(body)
    if not p["model"]:
        raise HTTPException(400, "请先选择或输入模型")
    payload = {"model": p["model"], "messages": [{"role": "user", "content": "连通性测试：请只回复 ok"}],
               "max_tokens": 16, "temperature": 0}
    t0 = time.monotonic()
    try:
        async with _client(p["proxy"], 45.0) as c:
            r = await c.post(f"{p['base']}/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {p['key']}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"无法连接到服务商：{type(e).__name__}",
                             "hint": "检查 base_url；国外服务商在国内通常需要填写代理"}, status_code=502)
    ms = int((time.monotonic() - t0) * 1000)
    if r.status_code >= 400:
        return JSONResponse({"ok": False, "status": r.status_code, "error": f"服务商返回 HTTP {r.status_code}",
                             "detail": _redact(r.text[:400], p["key"])}, status_code=502)
    try:
        data = r.json()
        reply = data["choices"][0]["message"].get("content") or ""
    except Exception:
        return JSONResponse({"ok": False, "error": "返回格式不是 OpenAI Chat Completions"}, status_code=502)
    return {"ok": True, "model": data.get("model") or p["model"], "latency_ms": ms,
            "reply": _redact(str(reply)[:120], p["key"])}


# ----------------------------------------------------------------- gateway token
@router.get("/api/auth/token")
async def token_info():
    tok = settings.gateway_token
    f = settings.gateway_token_file
    mode = oct(f.stat().st_mode & 0o777) if f.is_file() else None
    return {"set": bool(tok), "masked": _mask(tok), "length": len(tok), "file": str(f), "mode": mode,
            "note": "令牌 = 完整访问权限（读取全部消息）。请勿外泄；轮换后飞书秒搭等外部调用需更新 Bearer。"}


class TokenAction(BaseModel):
    action: str = Field(..., pattern="^(reveal|rotate|set)$")
    token: Optional[str] = Field(default=None, max_length=256)


@router.post("/api/auth/token")
async def token_action(body: TokenAction, response: Response):
    if body.action == "reveal":
        response.headers["Cache-Control"] = "no-store"
        return {"token": settings.gateway_token}
    if body.action == "rotate":
        new = secrets.token_urlsafe(32)
    else:
        new = (body.token or "").strip()
        if len(new) < 24 or not re.fullmatch(r"[A-Za-z0-9._~\-]+", new):
            raise HTTPException(400, "自定义令牌至少 24 位，只能包含字母、数字和 . _ ~ -")
    _write_secret_file(settings.gateway_token_file, new + "\n")
    # keep this browser/app session logged in with the new token
    response.set_cookie(COOKIE, new, httponly=True, samesite="strict", max_age=86400 * 365, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "rotated": True, "masked": _mask(new), "length": len(new),
            "note": "已生效。本窗口已自动更新登录；其它浏览器/飞书秒搭需改用新令牌。"}


# ----------------------------------------------------------------- data source / keys
def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p or ""))


def _wechat_version() -> Optional[str]:
    try:
        with open(WECHAT_APP / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
        return f"{info.get('CFBundleShortVersionString')} ({info.get('CFBundleVersion')})"
    except Exception:
        return None


def _wechat_running() -> bool:
    try:
        r = subprocess.run(["/usr/bin/pgrep", "-f", str(WECHAT_APP / "Contents/MacOS/WeChat")],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


@router.get("/api/source")
async def source_info(request: Request):
    from .reader_client import reader  # local import to avoid cycles

    src: Dict[str, Any] = {}
    try:
        src = json.loads((READER_CFG_DIR / "source-config.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    db = _expand(str(src.get("db_base_path") or ""))
    keys_count = 0
    try:
        k = json.loads((READER_CFG_DIR / "keys.json").read_text(encoding="utf-8"))
        keys_count = len(k) if isinstance(k, dict) else 0
    except Exception:
        pass
    status: Dict[str, Any] = {}
    try:
        status = await reader.get_json("/api/status")
    except Exception as e:
        status = {"error": type(e).__name__}
    return {
        "wxid": src.get("wxid"),
        "db_base_path": str(db) if src else None,
        "db_dir_exists": db.is_dir() if src else False,
        "config_dir": str(READER_CFG_DIR),
        "source_config": bool(src),
        "keys_entries": keys_count,
        "image_key": (READER_CFG_DIR / "image-key.json").is_file(),
        "reader_bin": str(READER_BIN),
        "reader": settings.reader_base,
        "reader_status": {k: status.get(k) for k in ("freshness", "self", "error") if k in status},
        "wechat_version": _wechat_version(),
        "wechat_running": _wechat_running(),
    }


@router.post("/api/keys/doctor")
async def keys_doctor():
    """Verify keys. Primary signal: the running reader is syncing fresh snapshots (it can only do that
    with valid keys). Secondary: `wechat-reader doctor` (per-DB first-page check, prints no secrets).
    doctor runs as a child of this gateway; macOS may hold it on an "access data from other apps"
    privacy prompt, so it gets a short timeout and the reader signal decides in that case."""
    from .reader_client import reader  # local import to avoid cycles

    fresh: Optional[bool] = None
    lag = None
    try:
        st = await reader.get_json("/api/status")
        f = st.get("freshness") or {}
        fresh = str(f.get("status") or "").lower() == "fresh"
        lag = f.get("lag_ms")
    except Exception:
        fresh = None
    res: Dict[str, Any] = {"reader_fresh": fresh, "reader_lag_ms": lag}
    if not READER_BIN.is_file():
        res.update(ok=bool(fresh), doctor="missing", output=f"reader binary not found: {READER_BIN}")
        return res
    try:
        r = subprocess.run([str(READER_BIN), "doctor"], cwd=str(READER_ROOT), capture_output=True,
                           text=True, timeout=25, env={**os.environ, "NO_COLOR": "1"})
        out = _redact((r.stdout or "") + ("\n" + r.stderr if r.stderr else ""))
        res.update(ok=(r.returncode == 0), doctor="ran", exit_code=r.returncode, output=out[-8000:])
    except subprocess.TimeoutExpired:
        res.update(
            ok=bool(fresh), doctor="timeout",
            output=("doctor 25 秒未返回：通常是 macOS 隐私授权挡住了（屏幕上可能有“想访问其他 App 的数据”弹窗，"
                    "可点允许后重试）。\n"
                    + ("读取服务正在实时同步（fresh），说明当前密钥有效。" if fresh else
                       "读取服务也没有处于 fresh 状态，密钥可能已失效（微信升级后常见）。")),
        )
    return res


@router.post("/api/keys/recapture")
async def keys_recapture():
    """Open Terminal with the guided, interactive Frida key-capture script.
    It asks for confirmation at every step and never runs unattended."""
    if not RECAPTURE_SCRIPT.is_file():
        raise HTTPException(404, "recapture script missing")
    try:
        subprocess.run(["/usr/bin/open", "-a", "Terminal", str(RECAPTURE_SCRIPT)], timeout=10, check=True)
    except Exception as e:
        raise HTTPException(500, f"无法打开终端：{type(e).__name__}")
    return {"ok": True, "opened": "Terminal", "script": str(RECAPTURE_SCRIPT),
            "note": "已在“终端”中打开引导脚本，请按提示操作（需要先完全退出微信）。"}
