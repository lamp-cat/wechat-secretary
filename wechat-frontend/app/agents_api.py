"""Settings API for agent-tool (MCP) integration. Token-gated (mounted behind require_auth).

- GET  /api/agents            which local agent tools are detected / registered
- POST /api/agents            {action: "register"|"unregister", tools: [...]} — backup first, non-destructive
- GET  /api/mcp-info          how to connect: stdio command, and the equivalent read-only HTTP endpoint
- POST /api/mcp-info/token    {action: "reveal"} → reader token, for the in-app copy button only (no-store)
Receive-only: the MCP server exposes read tools only; nothing here can send a WeChat message.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import agent_register as ar
from .config_api import READER_BIN, READER_CFG_DIR
from .settings import settings

router = APIRouter()


def _bin() -> str:
    return str(READER_BIN)


def _cfg() -> str:
    return str(READER_CFG_DIR)


class AgentAction(BaseModel):
    action: str = Field(pattern="^(register|unregister)$")
    tools: Optional[List[str]] = None


@router.get("/api/agents")
async def agents_status():
    st = await asyncio.to_thread(ar.status, _bin(), _cfg())
    st["reader_bin_exists"] = READER_BIN.is_file()
    return st


@router.post("/api/agents")
async def agents_action(body: AgentAction):
    valid = {t["id"] for t in ar.tool_specs()}
    tools = [t for t in (body.tools or []) if t in valid] or None
    if body.tools and not tools:
        raise HTTPException(400, "unknown tool id")
    fn = ar.register if body.action == "register" else ar.unregister
    results = await asyncio.to_thread(fn, _bin(), _cfg(), tools)
    st = await asyncio.to_thread(ar.status, _bin(), _cfg())
    return {"ok": all(r.get("result") != "error" for r in results), "results": results, "status": st}


@router.get("/api/mcp-info")
async def mcp_info():
    return {
        "receive_only": True,
        "stdio": {"command": _bin(), "args": ["mcp"], "env": {"WECHAT_READER_CONFIG": _cfg()},
                  "note": "本机 agent 直接启动该命令即可，命令自己读取 0600 密钥文件，配置里不含任何令牌。"},
        "http": {"url": f"{settings.reader_base}/mcp", "auth": "Authorization: Bearer <读取器令牌>",
                 "token_file": str(settings.reader_token_file),
                 "note": "只监听 127.0.0.1。令牌 = 完整读权限，请勿外传。"},
        "tools": ["list_sessions", "search_contacts", "get_messages", "search_messages", "list_chat_members",
                  "get_new_messages", "get_image", "get_video", "get_voice", "sync_status"],
    }


class Reveal(BaseModel):
    action: str = Field(pattern="^reveal$")


@router.post("/api/mcp-info/token")
async def mcp_token(body: Reveal, response: Response):
    tok = settings.reader_token
    if not tok:
        raise HTTPException(404, "reader token not created yet")
    response.headers["Cache-Control"] = "no-store"
    return {"token": tok}
