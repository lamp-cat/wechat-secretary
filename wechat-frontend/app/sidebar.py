"""Sidebar helper (伴侣浮窗): active-chat estimate + AI history summary. Read-only, no send path."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .reader_client import ReaderClient, ReaderError
from .settings import settings
from .suggest import NOT_CONFIGURED, ai_config, build_context, chat_completion

ACTIVE_NOTE = (
    "估计值：取 session.db 中 last_clear_unread_timestamp 最新的会话（=最近一次清掉未读的会话）。"
    "只在会话有未读时才更新、可能滞后数秒、手机上读消息也会改变，不一定等于 Mac 微信窗口当前打开的会话。"
)

_names_cache: Dict[str, Any] = {"t": 0.0, "map": {}}


def _session_db() -> Path:
    return settings.reader_snapshot_dir / "session" / "session.db"


def _query_active(limit: int) -> List[Dict[str, Any]]:
    """Read the reader's already-decrypted session.db snapshot, strictly read-only."""
    path = _session_db()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    last_err: Optional[Exception] = None
    for _ in range(3):  # snapshot may be swapped by the reader mid-read; retry briefly
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = con.execute(
                    "SELECT username, last_clear_unread_timestamp, last_timestamp, unread_count "
                    "FROM SessionTable WHERE IFNULL(is_hidden,0)=0 AND IFNULL(last_clear_unread_timestamp,0)>0 "
                    "AND username NOT LIKE '@%' "
                    "ORDER BY last_clear_unread_timestamp DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            finally:
                con.close()
            return [
                {"username": r[0], "last_clear_unread_timestamp": int(r[1] or 0),
                 "last_timestamp": int(r[2] or 0), "unread": int(r[3] or 0)}
                for r in rows
            ]
        except sqlite3.Error as e:
            last_err = e
            time.sleep(0.15)
    raise last_err or RuntimeError("session.db unreadable")


async def _names(reader: ReaderClient) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    if now - _names_cache["t"] < 30 and _names_cache["map"]:
        return _names_cache["map"]
    data = await reader.get_json("/api/sessions", {"limit": 1000})
    m = {s.get("username"): s for s in (data.get("sessions") or []) if s.get("username")}
    _names_cache.update(t=now, map=m)
    return m


def _iso(ts: int) -> Optional[str]:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


async def active_chat(reader: ReaderClient, top: int = 5) -> Dict[str, Any]:
    rows = await asyncio.to_thread(_query_active, max(1, min(top, 20)))
    try:
        names = await _names(reader)
    except ReaderError:
        names = {}
    out = []
    for r in rows:
        s = names.get(r["username"]) or {}
        out.append({
            "username": r["username"],
            "name": s.get("name") or r["username"],
            "kind": s.get("kind") or ("chatroom" if r["username"].endswith("@chatroom") else "private"),
            "last_clear_unread_timestamp": r["last_clear_unread_timestamp"],
            "opened_at": _iso(r["last_clear_unread_timestamp"]),
            "last_time": s.get("last_time") or _iso(r["last_timestamp"]),
            "unread": s.get("unread", r["unread"]),
        })
    try:
        snap = datetime.fromtimestamp(_session_db().stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        snap = None
    return {
        "active": out[0] if out else None,
        "candidates": out,
        "signal": "session.last_clear_unread_timestamp",
        "snapshot_time": snap,
        "estimate": True,
        "note": ACTIVE_NOTE,
    }


SUMMARY_SYSTEM = (
    "你是我的微信聊天助理，帮我快速回顾和某人/某群的聊天并准备回复。"
    "只根据给出的聊天记录，不要编造。用简洁中文。"
    "只输出 JSON，格式："
    "{\"overview\":\"一两句话概括我们最近在聊什么\","
    "\"topics\":[\"关键话题\"],"
    "\"open_threads\":[\"对方在等我回复/未了结的事项\"],"
    "\"commitments\":[{\"what\":\"约定或承诺的事\",\"when\":\"提到的时间/日期，没有就空字符串\",\"who\":\"我/对方/双方\"}],"
    "\"tone\":\"对话语气与关系氛围\","
    "\"reply_tips\":[\"回复时值得注意的点\"]}"
    "。每个数组最多 5 条，没有就给空数组。"
)


def _transcript(context: List[Dict[str, Any]], is_group: bool, cap: int = 14000) -> str:
    lines: List[str] = []
    for m in context:
        who = "我" if m["role"] == "self" else (m["name"] or "对方")
        t = (m.get("time") or "")[5:16].replace("T", " ")
        lines.append(f"[{t}] {who}: {m['text']}")
    # keep the most recent lines within the char budget
    out: List[str] = []
    total = 0
    for ln in reversed(lines):
        total += len(ln) + 1
        if total > cap:
            break
        out.append(ln)
    return "\n".join(reversed(out)) if out else "(暂无消息)"


def _parse_summary(content: str) -> Dict[str, Any]:
    m = re.search(r"\{[\s\S]*\}", content or "")
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                def arr(k):
                    v = obj.get(k)
                    return [x for x in v if x] if isinstance(v, list) else []
                return {
                    "overview": str(obj.get("overview") or ""),
                    "topics": [str(x) for x in arr("topics")],
                    "open_threads": [str(x) for x in arr("open_threads")],
                    "commitments": [
                        x if isinstance(x, dict) else {"what": str(x), "when": "", "who": ""}
                        for x in arr("commitments")
                    ],
                    "tone": str(obj.get("tone") or ""),
                    "reply_tips": [str(x) for x in arr("reply_tips")],
                }
        except Exception:
            pass
    return {"overview": (content or "").strip()[:2000], "topics": [], "open_threads": [],
            "commitments": [], "tone": "", "reply_tips": [], "unstructured": True}


async def summarize_chat(reader: ReaderClient, chat: str, n: int = 100, days: Optional[int] = None,
                         name: Optional[str] = None) -> Dict[str, Any]:
    ai = ai_config()
    if ai is None:
        return dict(NOT_CONFIGURED)
    n = max(10, min(int(n), 200))
    since = f"{int(days)}d" if days else None
    try:
        context = await build_context(reader, chat, n, max_n=200, since=since, text_cap=300)
    except ReaderError as e:
        return {"error": f"failed to load messages: {e.detail}", "status": e.status}
    is_group = chat.endswith("@chatroom")
    label = name or chat
    user = (
        f"会话：{label}（{'群聊' if is_group else '私聊'}）\n"
        f"以下是最近 {len(context)} 条消息（从早到晚，“我”是我自己）：\n"
        f"{_transcript(context, is_group)}\n\n请按要求输出 JSON 总结。"
    )
    res = await chat_completion(
        ai,
        [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=max(int(ai.get("max_tokens", 800)), 1000),
        temperature=0.3,
    )
    if res.get("error"):
        return res
    first = context[0]["time"] if context else None
    last = context[-1]["time"] if context else None
    return {
        "chat": chat,
        "summary": _parse_summary(res["content"]),
        "model": res["model"],
        "scope": {"n": n, "days": days, "messages": len(context), "from": first, "to": last},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "summary only — read-only, nothing is sent",
    }
