"""AI reply-suggestion drafts via external OpenAI-compatible API. Never sends."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from .reader_client import ReaderClient, ReaderError
from .settings import settings


NOT_CONFIGURED = {
    "error": "AI not configured",
    "howto": "打开 应用设置（/settings）→ AI 服务：填写 base_url、API Key、选择模型并启用。OpenAI-compatible Chat Completions only. Drafts never auto-send.",
}


def ai_config() -> Optional[Dict[str, Any]]:
    """Return the AI config if enabled and keyed, else None."""
    ai = settings.load_ai()
    if not ai.get("enabled") or not str(ai.get("api_key") or "").strip():
        return None
    return ai


async def build_context(
    reader: ReaderClient,
    chat: str,
    n: int = 20,
    *,
    max_n: int = 50,
    since: Optional[str] = None,
    text_cap: int = 500,
) -> List[Dict[str, Any]]:
    page = await reader.get_json(
        "/api/messages",
        {"chat": chat, "limit": max(1, min(int(n), max_n)), "since": since},
    )
    msgs = page.get("messages") or []
    # reader returns ascending (oldest first) within the page
    out = []
    for m in msgs:
        role = "self" if m.get("is_self") else "other"
        name = m.get("sender_name") or m.get("sender") or ""
        text = (m.get("text") or "").strip()
        kind = m.get("kind") or "text"
        if not text:
            text = f"[{kind}]"
        out.append({
            "role": role,
            "name": name,
            "time": m.get("time"),
            "kind": kind,
            "text": text[:text_cap],
        })
    return out


async def chat_completion(ai: Dict[str, Any], messages: List[Dict[str, str]], *, max_tokens: Optional[int] = None,
                          temperature: Optional[float] = None) -> Dict[str, Any]:
    """Call the configured OpenAI-compatible endpoint. Returns {"content","model"} or {"error",...}.
    Never logs or echoes the api_key."""
    base = str(ai.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = str(ai.get("model") or "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature if temperature is not None else ai.get("temperature", 0.7)),
        "max_tokens": int(max_tokens or ai.get("max_tokens", 800)),
    }
    headers = {
        "Authorization": f"Bearer {ai['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        kw: Dict[str, Any] = {"timeout": httpx.Timeout(90.0, connect=10.0), "trust_env": False}
        if str(ai.get("proxy") or "").strip():
            kw["proxy"] = str(ai["proxy"]).strip()  # explicit opt-in proxy for overseas providers
        async with httpx.AsyncClient(**kw) as client:
            r = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
    except httpx.HTTPError as e:
        return {"error": f"AI provider unreachable: {type(e).__name__}"}
    if r.status_code >= 400:
        detail = r.text[:300].replace(str(ai.get("api_key") or "\x00"), "***")
        return {"error": f"AI provider HTTP {r.status_code}", "detail": detail}
    try:
        data = r.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return {"error": "unexpected AI response shape"}
    return {"content": content or "", "model": model}


def _prompt(context: List[Dict[str, Any]], count: int = 3) -> List[Dict[str, str]]:
    lines = []
    for m in context:
        who = "我" if m["role"] == "self" else (m["name"] or "对方")
        lines.append(f"{who}: {m['text']}")
    transcript = "\n".join(lines) if lines else "(暂无消息)"
    lo = 2 if count >= 2 else 1
    system = (
        f"你是一个中文回复助手。根据聊天记录，给出 {lo} 到 {count} 条简短、自然、得体的建议回复草稿，"
        "彼此风格/方向有区别（例如：直接答复、追问细节、委婉推迟）。"
        "只输出 JSON：{\"suggestions\":[\"草稿1\",\"草稿2\"]}。"
        "不要编造事实，不要自动发送，不要提及你是 AI 或系统。语气匹配对话风格。"
    )
    user = f"最近聊天记录（从早到晚）：\n{transcript}\n\n请给出建议回复草稿。"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def suggest_replies(reader: ReaderClient, chat: str, n_context: int = 20, count: int = 3) -> Dict[str, Any]:
    ai = ai_config()
    if ai is None:
        return dict(NOT_CONFIGURED)
    count = max(1, min(int(count), 4))

    try:
        context = await build_context(reader, chat, n_context)
    except ReaderError as e:
        return {"error": f"failed to load messages: {e.detail}", "status": e.status}

    res = await chat_completion(ai, _prompt(context, count))
    if res.get("error"):
        return res
    suggestions = _parse_suggestions(res["content"], count)
    return {
        "suggestions": suggestions,
        "model": res["model"],
        "n_context": len(context),
        "note": "drafts only — never auto-sent",
    }


def _parse_suggestions(content: str, count: int = 3) -> List[str]:
    import json
    import re

    text = content.strip()
    # Try extract JSON object
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            arr = obj.get("suggestions") or obj.get("replies") or obj.get("drafts")
            if isinstance(arr, list):
                out = [str(x).strip() for x in arr if str(x).strip()]
                if out:
                    return out[:count]
        except Exception:
            pass
    # Fallback: split lines
    lines = [ln.strip(" -\t") for ln in text.splitlines() if ln.strip()]
    # drop markdown fences
    lines = [ln for ln in lines if not ln.startswith("```")]
    return (lines or [text])[:count]
