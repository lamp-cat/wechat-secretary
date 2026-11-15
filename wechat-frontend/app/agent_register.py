"""Register the read-only WeChat reader MCP server (`wechat-reader mcp`, stdio) into local agent tools.

Supported: Codex (~/.codex/config.toml), Claude Desktop, Claude Code (~/.claude.json / `claude mcp`),
Cursor (~/.cursor/mcp.json), VS Code (user mcp.json).

Rules:
- Only tools detected on this Mac are touched. Each config file is backed up before any edit.
- Merge is non-destructive: only our own server entry ("wechat-reader") is added/updated/removed.
  An entry with the same name that we did not create is left alone (reported as "user-managed").
- Idempotent: running register twice changes nothing the second time.
- No secret goes into any agent config: the stdio server reads its own 0600 key files.
- If the user removes (undoes) a tool, it is remembered and `auto` will not add it back.
- Receive-only: the MCP server only exposes read tools; there is no send path anywhere.

Stdlib only (runs with the system python3 3.9+). CLI:
  python3 agent_register.py status|auto|register|unregister [--tools codex,cursor] --bin <wechat-reader> [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER = "wechat-reader"
TOML_BEGIN = "# >>> wechat-reader (消息工作台 自动配置，只读 MCP) >>>"
TOML_END = "# <<< wechat-reader (消息工作台 自动配置) <<<"


def home() -> Path:
    return Path(os.environ.get("WD_AGENT_HOME") or os.environ.get("HOME") or str(Path.home()))


def state_dir() -> Path:
    return home() / ".config" / "wechatdesk"


def state_file() -> Path:
    return state_dir() / "agents.json"


def backups_root() -> Path:
    return state_dir() / "backups"


# ----------------------------------------------------------------- tools
def tool_specs() -> List[Dict[str, Any]]:
    h = home()
    sup = h / "Library" / "Application Support"
    return [
        {"id": "codex", "label": "Codex（ChatGPT 桌面版 / codex CLI）", "fmt": "toml",
         "path": Path(os.environ.get("CODEX_HOME") or (h / ".codex")) / "config.toml",
         "detect": [h / ".codex", Path("/Applications/ChatGPT.app"), Path("/Applications/Codex.app")]},
        {"id": "claude-desktop", "label": "Claude Desktop", "fmt": "json", "key": "mcpServers",
         "path": sup / "Claude" / "claude_desktop_config.json",
         "detect": [sup / "Claude", Path("/Applications/Claude.app")]},
        {"id": "claude-code", "label": "Claude Code", "fmt": "claude-code", "key": "mcpServers", "typed": True,
         "path": h / ".claude.json",
         "detect": [h / ".claude.json", h / ".claude"]},
        {"id": "cursor", "label": "Cursor", "fmt": "json", "key": "mcpServers",
         "path": h / ".cursor" / "mcp.json",
         "detect": [h / ".cursor", Path("/Applications/Cursor.app")]},
        {"id": "vscode", "label": "VS Code", "fmt": "json", "key": "servers", "typed": True,
         "path": sup / "Code" / "User" / "mcp.json",
         "detect": [sup / "Code" / "User", Path("/Applications/Visual Studio Code.app")]},
    ]


def find_claude_cli() -> Optional[str]:
    h = home()
    for p in [h / ".npm-global/bin/claude", h / ".claude/local/claude", h / ".local/bin/claude",
              Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")]:
        if p.exists() and os.access(str(p), os.X_OK):
            return str(p)
    return shutil.which("claude")


def detected(spec: Dict[str, Any]) -> bool:
    if spec["path"].exists():
        return True
    if spec["id"] == "claude-code" and find_claude_cli():
        return True
    return any(p.exists() for p in spec["detect"])


# ----------------------------------------------------------------- state
def load_state() -> Dict[str, Any]:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except Exception:
        return {"optout": [], "tools": {}}


def save_state(st: Dict[str, Any]) -> None:
    write_private(state_file(), json.dumps(st, ensure_ascii=False, indent=2) + "\n")


def write_private(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomic write. New files get 0600; existing files keep their mode (never loosened)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
        mode &= 0o755  # never write world/group-writable
    fd, tmp = tempfile.mkstemp(prefix=".wd-tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(path: Path, tool: str) -> Optional[str]:
    """Copy the file into ~/.config/wechatdesk/backups/<timestamp>/ (0700/0600). Never overwrites."""
    if not path.exists():
        return None
    root = backups_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(str(root), 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = root / stamp
    n = 1
    while (d / f"{tool}--{path.name}").exists():
        n += 1
        d = root / f"{stamp}-{n}"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(str(d), 0o700)
    dst = d / f"{tool}--{path.name}"
    shutil.copy2(str(path), str(dst))
    os.chmod(str(dst), 0o600)
    return str(dst)


# ----------------------------------------------------------------- entry
def server_entry(bin_path: str, cfg_dir: Optional[str]) -> Dict[str, Any]:
    e: Dict[str, Any] = {"command": bin_path, "args": ["mcp"]}
    if cfg_dir:
        e["env"] = {"WECHAT_READER_CONFIG": cfg_dir}
    return e


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_block(entry: Dict[str, Any]) -> str:
    lines = [TOML_BEGIN, f"[mcp_servers.{SERVER}]",
             f"command = {_toml_str(entry['command'])}",
             "args = [" + ", ".join(_toml_str(a) for a in entry["args"]) + "]",
             "startup_timeout_sec = 60"]
    if entry.get("env"):
        lines.append(f"[mcp_servers.{SERVER}.env]")
        for k, v in entry["env"].items():
            lines.append(f"{k} = {_toml_str(v)}")
    lines.append(TOML_END)
    return "\n".join(lines) + "\n"


_TOML_OURS = re.compile(r"\n?" + re.escape(TOML_BEGIN) + r".*?" + re.escape(TOML_END) + r"\n?", re.S)
_TOML_HDR = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:"wechat-reader"|wechat-reader)\s*\]', re.M)


def toml_verify(text: str) -> Optional[str]:
    """Return an error string if the text is not valid TOML (when a parser is available)."""
    try:
        import tomllib  # type: ignore
    except Exception:
        try:
            import tomli as tomllib  # type: ignore
        except Exception:
            return None
    try:
        tomllib.loads(text)
        return None
    except Exception as e:  # pragma: no cover
        return f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------- per-format read/write
def inspect(spec: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return {'present': bool, 'ours': bool, 'same': bool, 'error': str|None}."""
    p: Path = spec["path"]
    if not p.exists():
        return {"present": False, "ours": False, "same": False, "error": None}
    text = p.read_text(encoding="utf-8")
    if spec["fmt"] == "toml":
        if _TOML_OURS.search(text):
            same = _TOML_OURS.search(text).group(0).strip("\n") == toml_block(entry).strip("\n")
            return {"present": True, "ours": True, "same": same, "error": None}
        if _TOML_HDR.search(text):
            return {"present": True, "ours": False, "same": False, "error": None}
        return {"present": False, "ours": False, "same": False, "error": None}
    try:
        data = json.loads(text) if text.strip() else {}
    except Exception:
        return {"present": False, "ours": False, "same": False, "error": "配置文件不是标准 JSON（可能含注释），未改动"}
    servers = (data or {}).get(spec["key"]) or {}
    cur = servers.get(SERVER) if isinstance(servers, dict) else None
    if not cur:
        return {"present": False, "ours": False, "same": False, "error": None}
    want = {"type": "stdio", **entry} if spec.get("typed") else dict(entry)
    rec = load_state().get("tools", {}).get(spec["id"], {})
    cmd = cur.get("command") if isinstance(cur, dict) else None
    ours = cmd == entry["command"] or (bool(rec) and not rec.get("removed") and cmd == rec.get("command"))
    return {"present": True, "ours": ours, "same": cur == want, "error": None}


def write_entry(spec: Dict[str, Any], entry: Optional[Dict[str, Any]]) -> None:
    """entry=None removes our server. Caller backs up first."""
    p: Path = spec["path"]
    if spec["fmt"] == "toml":
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        text = _TOML_OURS.sub("", text)
        if entry is not None:
            if text and not text.endswith("\n"):
                text += "\n"
            if text and not text.endswith("\n\n"):
                text += "\n"
            text += toml_block(entry)
        err = toml_verify(text)
        if err:
            raise RuntimeError(f"写入后 TOML 校验失败，已放弃：{err}")
        write_private(p, text)
        return
    data: Dict[str, Any] = {}
    if p.exists():
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    servers = data.get(spec["key"])
    if not isinstance(servers, dict):
        servers = {}
    if entry is None:
        servers.pop(SERVER, None)
    else:
        servers[SERVER] = {"type": "stdio", **entry} if spec.get("typed") else entry
    data[spec["key"]] = servers
    write_private(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def claude_cli(args: List[str]) -> bool:
    cli = find_claude_cli()
    if not cli:
        return False
    env = dict(os.environ)
    env["HOME"] = str(home())
    env["PATH"] = os.pathsep.join([str(Path(cli).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    try:
        r = subprocess.run([cli, "mcp"] + args, capture_output=True, text=True, timeout=40, env=env)
        return r.returncode == 0
    except Exception:
        return False


# ----------------------------------------------------------------- public API
def status(bin_path: str, cfg_dir: Optional[str] = None) -> Dict[str, Any]:
    entry = server_entry(bin_path, cfg_dir)
    st = load_state()
    out = []
    for spec in tool_specs():
        info = inspect(spec, entry) if spec["path"].exists() else {"present": False, "ours": False, "same": False, "error": None}
        rec = st.get("tools", {}).get(spec["id"], {})
        out.append({
            "id": spec["id"], "label": spec["label"], "config": str(spec["path"]),
            "detected": detected(spec), "registered": bool(info["present"]),
            "managed": bool(info["ours"]), "up_to_date": bool(info["same"]),
            "user_managed": bool(info["present"] and not info["ours"]),
            "optout": spec["id"] in st.get("optout", []),
            "error": info["error"], "backup": rec.get("backup"), "updated_at": rec.get("at"),
        })
    return {"server": SERVER, "command": entry["command"], "args": entry["args"],
            "env": entry.get("env") or {}, "tools": out}


def register(bin_path: str, cfg_dir: Optional[str] = None, tools: Optional[List[str]] = None,
             auto: bool = False) -> List[Dict[str, Any]]:
    entry = server_entry(bin_path, cfg_dir)
    st = load_state()
    st.setdefault("optout", [])
    st.setdefault("tools", {})
    results = []
    for spec in tool_specs():
        tid = spec["id"]
        if tools and tid not in tools:
            continue
        if auto and (tid in st["optout"] or not detected(spec)):
            results.append({"id": tid, "result": "skipped", "why": "已撤销" if tid in st["optout"] else "未安装"})
            continue
        if not auto and tid in st["optout"]:
            st["optout"].remove(tid)  # explicit user action re-enables
        try:
            info = inspect(spec, entry)
            if info["error"]:
                results.append({"id": tid, "result": "error", "why": info["error"]})
                continue
            if info["present"] and not info["ours"]:
                results.append({"id": tid, "result": "user-managed", "why": "已存在同名配置（不是本应用写的），保持不动"})
                continue
            if info["present"] and info["same"]:
                results.append({"id": tid, "result": "unchanged"})
                continue
            bk = backup(spec["path"], tid)
            via = "file"
            if spec["fmt"] == "claude-code" and find_claude_cli():
                claude_cli(["remove", "-s", "user", SERVER])  # ours or absent at this point
                if claude_cli(["add-json", "-s", "user", SERVER, json.dumps({"type": "stdio", **entry})]) \
                        and inspect(spec, entry)["same"]:
                    via = "claude-cli"
                else:
                    write_entry(spec, entry)
            else:
                write_entry(spec, entry)
            st["tools"][tid] = {"backup": bk, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "via": via,
                                "command": entry["command"]}
            results.append({"id": tid, "result": "updated" if info["present"] else "added", "backup": bk, "via": via})
        except Exception as e:
            results.append({"id": tid, "result": "error", "why": f"{type(e).__name__}: {e}"})
    save_state(st)
    return results


def unregister(bin_path: str, cfg_dir: Optional[str] = None, tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    entry = server_entry(bin_path, cfg_dir)
    st = load_state()
    st.setdefault("optout", [])
    st.setdefault("tools", {})
    results = []
    for spec in tool_specs():
        tid = spec["id"]
        if tools and tid not in tools:
            continue
        if tid not in st["optout"]:
            st["optout"].append(tid)
        try:
            info = inspect(spec, entry)
            if not info["present"]:
                results.append({"id": tid, "result": "absent"})
                continue
            if not info["ours"] and not (spec["fmt"] == "toml" and _TOML_OURS.search(spec["path"].read_text(encoding="utf-8"))):
                results.append({"id": tid, "result": "user-managed", "why": "同名配置不是本应用写的，未删除"})
                continue
            bk = backup(spec["path"], tid)
            if not (spec["fmt"] == "claude-code" and claude_cli(["remove", "-s", "user", SERVER])
                    and not inspect(spec, entry)["present"]):
                write_entry(spec, None)
            st["tools"][tid] = {"backup": bk, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "removed": True}
            results.append({"id": tid, "result": "removed", "backup": bk})
        except Exception as e:
            results.append({"id": tid, "result": "error", "why": f"{type(e).__name__}: {e}"})
    save_state(st)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Register the read-only wechat-reader MCP server into local agent tools.")
    ap.add_argument("action", choices=["status", "auto", "register", "unregister"])
    ap.add_argument("--bin", required=True, help="absolute path of the wechat-reader binary")
    ap.add_argument("--config-dir", default=None, help="WECHAT_READER_CONFIG to pass to the server")
    ap.add_argument("--tools", default="", help="comma-separated tool ids (default: all)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    tools = [t for t in a.tools.split(",") if t] or None
    if a.action == "status":
        out: Any = status(a.bin, a.config_dir)
    elif a.action == "unregister":
        out = unregister(a.bin, a.config_dir, tools)
    else:
        out = register(a.bin, a.config_dir, tools, auto=(a.action == "auto"))
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        rows = out["tools"] if isinstance(out, dict) else out
        for r in rows:
            print("\t".join(str(r.get(k, "")) for k in ("id", "result", "registered", "why")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
