"""Local message archive (opt-in, default OFF).

When enabled, a background task in the gateway copies messages from the local
reader into a neutral, self-describing local folder (SQLite + Markdown +
attachments) so that third-party agents can read *only that folder* and never
touch the reader or the chat client.

Rules enforced here:
- Receive-only / read-only: only GET requests to the local reader. No send path.
- Data stays on this machine (archive_dir must be under the user's home).
- Tokens/keys are never written into the archive, never logged.
- Raw account identifiers are replaced by stable hashed ids (c_xxx / p_xxx).
- Loopback httpx client uses trust_env=False (bypass system proxy).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .settings import settings

log = logging.getLogger("wechat-frontend.archive")
router = APIRouter()

HOME = Path.home()
CONFIG_FILE = HOME / ".config" / "wechat-frontend" / "archive-config.json"
DEFAULT_DIR = HOME / "MessageArchive"
MARKER = ".message-archive-root"
SCHEMA_VERSION = "1"
TZ = timezone(timedelta(hours=8))

DEFAULTS: Dict[str, Any] = {
    "enabled": False,            # master switch, OFF by default
    "mode": "all",               # all | selected
    "selected_chats": [],        # source chat ids (only used in selected mode)
    "archive_dir": str(DEFAULT_DIR),
    "backfill_history": True,    # on first sync, page back through existing history
    "backfill_days": 0,          # 0 = all available history; N = only last N days
    "copy_media": True,          # copy images / videos / voice into attachments/
    "interval_sec": 15,          # polling interval for new messages
    "confirmed_at": None,        # when the user confirmed the enable dialog
}

PAGE = 1000
MAX_BACKFILL_PAGES_PER_CHAT = 20
MEDIA_BATCH = 150
RETRY_AFTER_SEC = 600

KIND_MAP = {"chatroom": "group", "friend": "private", "stranger": "private", "official": "official"}
_RAW_PERSON = re.compile(r"wxid_[A-Za-z0-9_\-]+")
_RAW_GROUP = re.compile(r"\b\d{5,}@chatroom\b")
_BAD_NAME = re.compile(r"[\x00-\x1f/\\:*?\"<>|]+")


# ------------------------------------------------------------------ helpers
def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _hid(prefix: str, raw: str) -> str:
    return prefix + "_" + hashlib.sha256(("msgarchive:v1:" + raw).encode("utf-8")).hexdigest()[:12]


def conv_id(raw: str) -> str:
    return _hid("c", raw)


def person_id(raw: str) -> str:
    return _hid("p", raw) if raw else ""


def scrub(text: Any) -> Any:
    """Replace raw account identifiers inside free text with neutral hashed ids."""
    if not isinstance(text, str) or not text:
        return text
    text = _RAW_GROUP.sub(lambda m: conv_id(m.group(0)), text)
    return _RAW_PERSON.sub(lambda m: person_id(m.group(0)), text)


def _scrub_extra(extra: Any) -> Any:
    if isinstance(extra, dict):
        out = {}
        for k, v in extra.items():
            if k == "sender" and isinstance(v, str):
                out["sender_id"] = person_id(v)
            else:
                out[k] = _scrub_extra(v)
        return out
    if isinstance(extra, list):
        return [_scrub_extra(x) for x in extra]
    return scrub(extra)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_FILE.is_file():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULTS:
                    if k in raw:
                        cfg[k] = raw[k]
    except Exception:
        pass
    if cfg["mode"] not in ("all", "selected"):
        cfg["mode"] = "all"
    if not isinstance(cfg["selected_chats"], list):
        cfg["selected_chats"] = []
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    _atomic_write(CONFIG_FILE, (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)


def validate_dir(p: str) -> Path:
    path = Path(os.path.expanduser(str(p or "").strip())).resolve()
    home = HOME.resolve()
    if not str(path).startswith(str(home) + os.sep):
        raise HTTPException(400, "归档目录必须位于用户主目录之下（只存本机）")
    rel = path.relative_to(home).parts
    if not rel or rel[0] in ("Library", ".config", ".local", ".ssh", "wechat-platform", "Applications"):
        raise HTTPException(400, "不允许把归档放在该目录下，请选一个独立目录，例如 ~/MessageArchive")
    return path


def safe_folder(name: str, cid: str) -> str:
    n = _BAD_NAME.sub("_", (name or "").strip()).strip(" .")
    n = n[:60].strip() or cid
    return n


# ------------------------------------------------------------------ archive DB
SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  conversation_id TEXT PRIMARY KEY,
  name TEXT,
  kind TEXT,                 -- group | private | official | other
  folder TEXT UNIQUE,        -- conversations/<folder>/
  message_count INTEGER DEFAULT 0,
  first_time TEXT,
  last_time TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  local_id INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  server_id TEXT,
  time TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  sender_id TEXT,
  sender_name TEXT,
  is_self INTEGER DEFAULT 0,
  kind TEXT,
  type_code INTEGER,
  sub_type INTEGER,
  text TEXT,
  extra_json TEXT,
  archived_at TEXT,
  UNIQUE(conversation_id, seq, local_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_conv_time ON messages(conversation_id, timestamp);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  kind TEXT NOT NULL,        -- image | video | voice | file
  file_name TEXT,
  local_path TEXT,           -- relative to archive root
  preview_path TEXT,         -- thumbnail / video cover, relative to archive root
  size_bytes INTEGER,
  mime TEXT,
  status TEXT NOT NULL,      -- queued | ok | thumb_only | pending_download | file_body_unavailable | unavailable | skipped
  source_ref TEXT,
  attempts INTEGER DEFAULT 0,
  last_attempt_at INTEGER,
  updated_at TEXT,
  UNIQUE(message_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_att_status ON attachments(status);
CREATE TABLE IF NOT EXISTS sync_state(
  conversation_id TEXT PRIMARY KEY,
  last_seq INTEGER,          -- newest archived seq (NULL = never synced)
  backfill_before INTEGER,   -- cursor for older history
  backfill_done INTEGER DEFAULT 0,
  last_activity TEXT
);
CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY, value TEXT);
"""


def open_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    marker = root / MARKER
    if not marker.exists():
        marker.write_text("local message archive root\n", encoding="utf-8")
    db = sqlite3.connect(str(root / "archive.db"), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def meta_get(db: sqlite3.Connection, key: str) -> Optional[str]:
    r = db.execute("SELECT value FROM archive_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def meta_set(db: sqlite3.Connection, key: str, value: Optional[str]) -> None:
    db.execute("INSERT INTO archive_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def compute_lower_bound(cfg: Dict[str, Any]) -> Optional[str]:
    if not cfg.get("backfill_history"):
        return datetime.now(TZ).isoformat(timespec="seconds")
    days = int(cfg.get("backfill_days") or 0)
    if days > 0:
        return (datetime.now(TZ) - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    return None


README = """# 消息归档

本目录是一份**本机消息归档**（只读副本），由「消息工作台」的「消息归档」功能自动生成并持续增量更新。
数据只保存在这台电脑上，不会上传。请把它当作只读资料使用；手动修改可能在下次同步时被覆盖。

## 目录结构

```
<归档根目录>/
├── README.md                 本说明
├── archive.db                SQLite 数据库（全部会话、消息、附件索引）
└── conversations/
    └── <会话名>/
        ├── messages.md       该会话的全部消息，按日期分组，按时间正序
        └── attachments/      该会话的图片 / 视频 / 语音等附件
```

- 文件夹名就是会话名（群名或联系人备注/昵称），重名时末尾追加短 ID。
- `messages.md` 里的附件用相对路径链接到 `attachments/`。

## 数据库表结构（archive.db）

### conversations（会话）
| 字段 | 说明 |
|---|---|
| conversation_id | 会话 ID（稳定的匿名 ID，形如 `c_xxxxxxxxxxxx`） |
| name | 会话名称 |
| kind | `group` 群聊 / `private` 私聊 / `official` 公众号或服务号 / `other` |
| folder | 对应的文件夹名，`conversations/<folder>/` |
| message_count | 已归档消息数 |
| first_time / last_time | 已归档消息的最早 / 最晚时间（ISO 8601，+08:00） |
| updated_at | 本会话最近一次更新时间 |

### messages（消息）
| 字段 | 说明 |
|---|---|
| id | 自增主键 |
| conversation_id | 所属会话 |
| local_id / seq | 来源中的消息序号，(conversation_id, seq, local_id) 唯一 |
| server_id | 来源中的服务端消息号（可能为 0） |
| time / timestamp | 发送时间（ISO 8601 / Unix 秒） |
| sender_id | 发送人匿名 ID（形如 `p_xxxxxxxxxxxx`，同一人恒定） |
| sender_name | 发送人显示名 |
| is_self | 1 = 本人发出 |
| kind | 消息类型：text, image, voice, video, file, link, sticker, system, reply, location, card, chat_record, mini_program, call, other… |
| type_code / sub_type | 原始类型码 |
| text | 可读文本（图片等显示为 `[图片]` 之类的占位） |
| extra_json | 附加信息 JSON：链接标题/网址、文件名/大小、语音/视频时长、引用消息等 |

### attachments（附件）
| 字段 | 说明 |
|---|---|
| message_id | 对应 messages.id |
| kind | `image` / `video` / `voice` / `file` |
| file_name | 文件名（文件消息为原始文件名） |
| local_path | 附件相对归档根目录的路径；为空表示本体不可用 |
| preview_path | 缩略图或视频封面路径（如有） |
| size_bytes | 大小（文件消息为原始文件大小） |
| status | 见下表 |

| status | 含义 |
|---|---|
| ok | 已保存完整内容（原图 / mp4 / wav） |
| thumb_only | 只拿到缩略图，原图尚未下载到本机；之后会自动重试 |
| pending_download | 视频本体尚未下载到本机，暂存封面；之后会自动重试 |
| file_body_unavailable | 文件消息，只有文件名和大小，文件本体不可用 |
| unavailable | 暂时拿不到任何内容；之后会自动重试 |
| skipped | 未开启附件复制 |
| queued | 等待处理 |

## 常用查询示例

```sql
-- 列出所有会话
SELECT name, kind, message_count, last_time FROM conversations ORDER BY last_time DESC;
-- 某个群最近 100 条消息
SELECT m.time, m.sender_name, m.text FROM messages m JOIN conversations c USING(conversation_id)
 WHERE c.name = '某群名' ORDER BY m.timestamp DESC LIMIT 100;
-- 某会话的全部图片
SELECT a.local_path, a.status FROM attachments a JOIN conversations c USING(conversation_id)
 WHERE c.name = '某群名' AND a.kind = 'image';
```
"""


# ------------------------------------------------------------------ manager
class ArchiveManager:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._client: Optional[httpx.AsyncClient] = None
        self.status: Dict[str, Any] = {"running": False, "phase": "idle", "last_sync_at": None,
                                       "last_error": None, "last_cycle_new": 0}

    # ---- lifecycle
    async def start(self) -> None:
        cfg = load_config()
        if cfg.get("enabled") and (self._task is None or self._task.done()):
            self._wake = asyncio.Event()
            self._task = asyncio.create_task(self._run())
            self.status["running"] = True
            log.info("archive: started (mode=%s)", cfg.get("mode"))

    async def stop(self) -> None:
        t = self._task
        self._task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.status["running"] = False
        self.status["phase"] = "stopped"

    def wake(self) -> None:
        self._wake.set()

    # ---- reader access (GET only)
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0), trust_env=False, proxy=None)
        return self._client

    def _headers(self) -> Dict[str, str]:
        tok = settings.reader_token
        if not tok:
            raise RuntimeError("reader token unavailable")
        return {"Authorization": "Bearer " + tok}

    async def _get_json(self, path: str, params: Dict[str, Any]) -> Any:
        c = await self._http()
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        r = await c.get(settings.reader_base + path, params=clean, headers=self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"reader {path} HTTP {r.status_code}")
        return r.json()

    async def _get_bytes(self, path: str, params: Dict[str, Any]) -> Tuple[int, bytes, Dict[str, str]]:
        c = await self._http()
        r = await c.get(settings.reader_base + path, params=params, headers=self._headers(),
                        timeout=httpx.Timeout(180.0, connect=5.0))
        return r.status_code, r.content, {k.lower(): v for k, v in r.headers.items()}

    # ---- main loop
    async def _run(self) -> None:
        while True:
            cfg = load_config()
            if not cfg.get("enabled"):
                break
            try:
                self.status["phase"] = "syncing"
                await self._cycle(cfg)
                self._force = False
                self.status["last_error"] = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never include secrets in the message
                self.status["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("archive: cycle failed: %s", type(e).__name__)
            self.status["phase"] = "idle"
            self.status["last_sync_at"] = _now_iso()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(5, int(cfg.get("interval_sec") or 15)))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
        self.status["running"] = False

    async def _cycle(self, cfg: Dict[str, Any]) -> None:
        self._cfg = cfg
        root = validate_dir(cfg["archive_dir"])
        db = open_db(root)
        try:
            if meta_get(db, "schema_version") is None:
                meta_set(db, "schema_version", SCHEMA_VERSION)
                meta_set(db, "created_at", _now_iso())
                meta_set(db, "lower_bound", compute_lower_bound(cfg) or "")
                db.commit()
            readme = root / "README.md"
            if not readme.exists() or readme.read_text(encoding="utf-8") != README:
                _atomic_write(readme, README.encode("utf-8"), 0o644)
            lower = meta_get(db, "lower_bound") or None

            data = await self._get_json("/api/sessions", {"limit": 1000})
            sessions = data.get("sessions") or []
            self._raw_by_cid = {conv_id(s["username"]): s["username"] for s in sessions if s.get("username")}
            if cfg["mode"] == "selected":
                wanted = set(cfg.get("selected_chats") or [])
                sessions = [s for s in sessions if s.get("username") in wanted]

            dirty: set = set()
            new_total = 0
            for s in sessions:
                n = await self._sync_chat(db, root, s, lower)
                new_total += n
                cid = conv_id(s["username"])
                folder = db.execute("SELECT folder FROM conversations WHERE conversation_id=?", (cid,)).fetchone()[0]
                if n or not (root / "conversations" / folder / "messages.md").exists():
                    dirty.add(cid)
            self.status["last_cycle_new"] = new_total

            touched = await self._process_media(db, root, cfg)
            dirty |= touched
            for cid in dirty:
                self._refresh_conv_stats(db, cid)
            db.commit()
            for cid in dirty:
                await asyncio.to_thread(self._write_markdown, root, cid)
            if new_total:
                log.info("archive: +%d messages in %d conversations", new_total, len(dirty))
        finally:
            db.close()

    # ---- per-conversation sync
    def _upsert_conv(self, db: sqlite3.Connection, root: Path, s: Dict[str, Any]) -> str:
        cid = conv_id(s["username"])
        name = s.get("name") or ""
        if not name or name == s["username"] or _RAW_PERSON.search(name) or _RAW_GROUP.search(name):
            name = scrub(name) if name and name != s["username"] else cid
        kind = KIND_MAP.get(s.get("kind") or "", "other")
        row = db.execute("SELECT folder FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
        if row is None:
            folder = safe_folder(name, cid)
            if db.execute("SELECT 1 FROM conversations WHERE folder=?", (folder,)).fetchone():
                folder = f"{folder}-{cid[-6:]}"
            db.execute("INSERT INTO conversations(conversation_id,name,kind,folder,updated_at) VALUES(?,?,?,?,?)",
                       (cid, name, kind, folder, _now_iso()))
            db.execute("INSERT OR IGNORE INTO sync_state(conversation_id) VALUES(?)", (cid,))
        else:
            db.execute("UPDATE conversations SET name=?, kind=? WHERE conversation_id=?", (name, kind, cid))
        return cid

    def _insert_page(self, db: sqlite3.Connection, cid: str, msgs: List[Dict[str, Any]], copy_media_flag: bool) -> int:
        n = 0
        now = _now_iso()
        for m in msgs:
            try:
                ts = int(datetime.fromisoformat(str(m.get("time"))).timestamp())
            except Exception:
                ts = 0
            extra = _scrub_extra(m.get("extra")) if m.get("extra") else None
            sender = m.get("sender") or ""
            sender_name = m.get("sender_name") or ""
            if not sender_name or sender_name == sender or _RAW_PERSON.fullmatch(sender_name):
                sender_name = person_id(sender) if sender else ""
            cur = db.execute(
                "INSERT OR IGNORE INTO messages(conversation_id,local_id,seq,server_id,time,timestamp,sender_id,sender_name,"
                "is_self,kind,type_code,sub_type,text,extra_json,archived_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, int(m.get("local_id") or 0), int(m.get("seq") or 0), str(m.get("server_id") or ""),
                 str(m.get("time") or ""), ts, person_id(sender), sender_name, 1 if m.get("is_self") else 0,
                 m.get("kind") or "other", int(m.get("type") or 0), int(m.get("sub_type") or 0),
                 scrub(m.get("text") or ""), json.dumps(extra, ensure_ascii=False) if extra else None, now))
            if cur.rowcount != 1:
                continue
            n += 1
            mid = cur.lastrowid
            att = self._attachment_for(m, extra or {})
            if att:
                kind, fname, size, status = att
                if status == "queued" and not copy_media_flag:
                    status = "skipped"
                db.execute(
                    "INSERT OR IGNORE INTO attachments(message_id,conversation_id,kind,file_name,size_bytes,status,source_ref,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (mid, cid, kind, fname, size, status, json.dumps(
                        {"media_id": m.get("media_id") or "", "local_id": int(m.get("local_id") or 0), "t": ts}), now))
        return n

    @staticmethod
    def _attachment_for(m: Dict[str, Any], extra: Dict[str, Any]) -> Optional[Tuple[str, Optional[str], Optional[int], str]]:
        t = int(m.get("type") or 0)
        kind = m.get("kind")
        if t == 3:
            return ("image", None, None, "queued")
        if t == 43:
            return ("video", None, None, "queued")
        if t == 34:
            return ("voice", None, None, "queued")
        if kind == "file":
            size = extra.get("file_size")
            return ("file", extra.get("title") or None, int(size) if size else None, "file_body_unavailable")
        return None

    async def _fetch_msgs(self, raw: str, lower: Optional[str], before: Optional[int] = None,
                          after: Optional[int] = None) -> Dict[str, Any]:
        return await self._get_json("/api/messages", {"chat": raw, "limit": PAGE, "since": lower,
                                                      "before": before or None, "after": after or None})

    async def _sync_chat(self, db: sqlite3.Connection, root: Path, s: Dict[str, Any], lower: Optional[str]) -> int:
        raw = s["username"]
        cid = self._upsert_conv(db, root, s)
        st = db.execute("SELECT * FROM sync_state WHERE conversation_id=?", (cid,)).fetchone()
        copy_flag = bool((self._cfg or {}).get("copy_media", True))
        activity = str(s.get("last_time") or "")
        added = 0
        if st["last_seq"] is None:
            # first sync of this conversation: newest page, then older pages via backfill cursor
            page = await self._fetch_msgs(raw, lower)
            msgs = page.get("messages") or []
            added += self._insert_page(db, cid, msgs, copy_flag)
            top = max([int(m.get("seq") or 0) for m in msgs] or [0])
            db.execute("UPDATE sync_state SET last_seq=?, backfill_before=?, backfill_done=?, last_activity=? WHERE conversation_id=?",
                       (top, page.get("next_before") if page.get("has_more") else None,
                        0 if page.get("has_more") else 1, activity, cid))
            db.commit()
        elif activity != (st["last_activity"] or "") or self._force:
            # incremental: everything newer than last_seq (page back until the gap is filled)
            last = int(st["last_seq"] or 0)
            before = None
            top = last
            while True:
                page = await self._fetch_msgs(raw, lower, before=before, after=last)
                msgs = page.get("messages") or []
                added += self._insert_page(db, cid, msgs, copy_flag)
                top = max([top] + [int(m.get("seq") or 0) for m in msgs])
                db.commit()
                if not page.get("has_more"):
                    break
                before = page.get("next_before")
            db.execute("UPDATE sync_state SET last_seq=?, last_activity=? WHERE conversation_id=?", (top, activity, cid))
            db.commit()
        # backfill older history (resumable: cursor committed with each page)
        st = db.execute("SELECT * FROM sync_state WHERE conversation_id=?", (cid,)).fetchone()
        pages = 0
        while not st["backfill_done"] and pages < MAX_BACKFILL_PAGES_PER_CHAT:
            page = await self._fetch_msgs(raw, lower, before=st["backfill_before"])
            msgs = page.get("messages") or []
            added += self._insert_page(db, cid, msgs, copy_flag)
            more = bool(page.get("has_more"))
            db.execute("UPDATE sync_state SET backfill_before=?, backfill_done=? WHERE conversation_id=?",
                       (page.get("next_before") if more else None, 0 if more else 1, cid))
            db.commit()
            pages += 1
            st = db.execute("SELECT * FROM sync_state WHERE conversation_id=?", (cid,)).fetchone()
        return added

    _force = False
    _cfg: Optional[Dict[str, Any]] = None
    _raw_by_cid: Dict[str, str] = {}

    # ---- media
    async def _process_media(self, db: sqlite3.Connection, root: Path, cfg: Dict[str, Any]) -> set:
        touched: set = set()
        if not cfg.get("copy_media", True):
            return touched
        cutoff = int(time.time()) - RETRY_AFTER_SEC
        rows = db.execute(
            "SELECT a.*, m.time AS mtime, m.local_id AS mlocal, c.folder FROM attachments a "
            "JOIN messages m ON m.id=a.message_id JOIN conversations c ON c.conversation_id=a.conversation_id "
            "WHERE a.status IN ('queued','skipped') OR (a.status IN ('thumb_only','pending_download','unavailable') "
            "AND IFNULL(a.last_attempt_at,0) < ? AND a.attempts < 300) ORDER BY (a.status IN ('queued','skipped')) DESC, a.id LIMIT ?", (cutoff, MEDIA_BATCH)).fetchall()
        for a in rows:
            raw = self._raw_by_cid.get(a["conversation_id"])
            if not raw:
                continue
            try:
                changed = await self._fetch_attachment(db, root, a, raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("archive: attachment fetch error: %s", type(e).__name__)
                changed = False
            db.execute("UPDATE attachments SET attempts=attempts+1, last_attempt_at=? WHERE id=?", (int(time.time()), a["id"]))
            if changed:
                touched.add(a["conversation_id"])
        db.commit()
        return touched

    @staticmethod
    def _ext(mime: str, default: str) -> str:
        mime = (mime or "").split(";")[0].strip().lower()
        return {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                "image/heic": ".heic", "video/mp4": ".mp4", "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(mime, default)

    def _save(self, root: Path, folder: str, base: str, body: bytes, mime: str, default_ext: str) -> Tuple[str, int]:
        rel = Path("conversations") / folder / "attachments" / (base + self._ext(mime, default_ext))
        _atomic_write(root / rel, body, 0o644)
        return str(rel), len(body)

    async def _fetch_attachment(self, db: sqlite3.Connection, root: Path, a: sqlite3.Row, raw: str) -> bool:
        ref = json.loads(a["source_ref"] or "{}")
        media_id, lid, ts = ref.get("media_id") or "", int(ref.get("local_id") or 0), int(ref.get("t") or 0)
        stamp = datetime.fromtimestamp(ts, TZ).strftime("%Y%m%d-%H%M%S") if ts else "0"
        base = f"{stamp}_{lid}"
        folder = a["folder"]
        now = _now_iso()
        kind = a["kind"]
        prev = a["status"]

        def upd(status: str, local: Optional[str], preview: Optional[str], size: Optional[int], mime: Optional[str]) -> bool:
            db.execute("UPDATE attachments SET status=?, local_path=?, preview_path=?, size_bytes=?, mime=?, file_name=?, updated_at=? WHERE id=?",
                       (status, local, preview, size, mime, Path(local).name if local else None, now, a["id"]))
            return status != prev or local != a["local_path"]

        if kind == "image":
            code, body, h = await self._get_bytes("/api/image", {"chat": raw, "id": media_id, "local_id": lid, "t": ts, "size": "full"})
            if code == 200 and body:
                mime = h.get("content-type", "")
                thumb = h.get("x-image-variant", "") == "thumb-only"
                rel, size = self._save(root, folder, base + ("_thumb" if thumb else ""), body, mime, ".jpg")
                if not thumb and a["local_path"] and a["local_path"] != rel:
                    try:
                        (root / a["local_path"]).unlink()
                    except OSError:
                        pass
                return upd("thumb_only" if thumb else "ok", rel, rel if thumb else None, size, mime)
            code, body, h = await self._get_bytes("/api/image", {"chat": raw, "id": media_id, "local_id": lid, "t": ts, "size": "thumb"})
            if code == 200 and body:
                rel, size = self._save(root, folder, base + "_thumb", body, h.get("content-type", ""), ".jpg")
                return upd("thumb_only", rel, rel, size, h.get("content-type"))
            return upd("unavailable", None, None, None, None)
        if kind == "video":
            if media_id:
                code, body, h = await self._get_bytes("/api/video", {"id": media_id, "t": ts})
                if code == 200 and body:
                    rel, size = self._save(root, folder, base, body, "video/mp4", ".mp4")
                    return upd("ok", rel, a["preview_path"], size, "video/mp4")
                preview = a["preview_path"]
                if not preview:
                    code, body, h = await self._get_bytes("/api/video", {"id": media_id, "t": ts, "cover": "1"})
                    if code == 200 and body:
                        preview, _ = self._save(root, folder, base + "_cover", body, h.get("content-type", ""), ".jpg")
                return upd("pending_download", None, preview, None, None)
            return upd("pending_download", None, None, None, None)
        if kind == "voice":
            code, body, h = await self._get_bytes("/api/voice", {"chat": raw, "local_id": lid, "t": ts})
            if code == 200 and body:
                rel, size = self._save(root, folder, base, body, "audio/wav", ".wav")
                return upd("ok", rel, None, size, "audio/wav")
            return upd("unavailable", None, None, None, None)
        return False

    # ---- stats / markdown
    def _refresh_conv_stats(self, db: sqlite3.Connection, cid: str) -> None:
        r = db.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM messages WHERE conversation_id=?", (cid,)).fetchone()
        db.execute("UPDATE conversations SET message_count=?, first_time=?, last_time=?, updated_at=? WHERE conversation_id=?",
                   (r[0], r[1], r[2], _now_iso(), cid))

    def _write_markdown(self, root: Path, cid: str) -> None:
        db = sqlite3.connect(str(root / "archive.db"), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            c = db.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            if c is None:
                return
            atts: Dict[int, sqlite3.Row] = {}
            for a in db.execute("SELECT * FROM attachments WHERE conversation_id=?", (cid,)):
                atts[a["message_id"]] = a
            kind_cn = {"group": "群聊", "private": "私聊", "official": "公众号/服务号"}.get(c["kind"], "其他")
            out = [f"# {c['name']}", "",
                   f"- 会话类型：{kind_cn}",
                   f"- 会话 ID：`{cid}`",
                   f"- 已归档消息：{c['message_count']} 条",
                   f"- 时间范围：{c['first_time'] or '-'} ～ {c['last_time'] or '-'}",
                   f"- 最近更新：{_now_iso()}", ""]
            day = None
            for m in db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY timestamp, seq, local_id", (cid,)):
                dt = datetime.fromtimestamp(m["timestamp"], TZ) if m["timestamp"] else None
                d = dt.strftime("%Y-%m-%d") if dt else "未知日期"
                if d != day:
                    out += ["", f"## {d}", ""]
                    day = d
                who = "我" if m["is_self"] else (m["sender_name"] or m["sender_id"] or "系统")
                hhmm = dt.strftime("%H:%M:%S") if dt else ""
                body = (m["text"] or "").replace("\r", "")
                a = atts.get(m["id"])
                if a is not None:
                    body += " " + self._md_attachment(c["folder"], a)
                lines = body.split("\n")
                out.append(f"- **{hhmm} {who}**：{lines[0]}")
                for ln in lines[1:]:
                    out.append(f"  {ln}")
            target = root / "conversations" / c["folder"] / "messages.md"
            _atomic_write(target, ("\n".join(out) + "\n").encode("utf-8"), 0o644)
        finally:
            db.close()

    @staticmethod
    def _md_attachment(folder: str, a: sqlite3.Row) -> str:
        prefix = f"conversations/{folder}/"

        def rel(p: Optional[str]) -> str:
            p = p or ""
            return p[len(prefix):] if p.startswith(prefix) else p

        st, k = a["status"], a["kind"]
        if k == "file":
            size = a["size_bytes"]
            sz = f"{size / 1024 / 1024:.2f} MB" if size and size >= 1024 * 1024 else (f"{(size or 0) / 1024:.1f} KB" if size else "大小未知")
            return f"（附件：{a['file_name'] or '文件'}，{sz}；文件本体不可用）"
        if k == "image":
            if st == "ok":
                return f"![图片]({rel(a['local_path'])})"
            if st == "thumb_only":
                return f"![图片缩略图]({rel(a['local_path'])})（仅缩略图，原图未下载）"
            return "（图片暂不可用）"
        if k == "video":
            if st == "ok":
                return f"[视频文件]({rel(a['local_path'])})"
            if a["preview_path"]:
                return f"![视频封面]({rel(a['preview_path'])})（视频本体未下载）"
            return "（视频本体未下载）"
        if k == "voice":
            if st == "ok":
                return f"[语音音频]({rel(a['local_path'])})"
            return "（语音暂不可用）"
        return ""

    # ---- status for UI
    def summary(self) -> Dict[str, Any]:
        cfg = load_config()
        out: Dict[str, Any] = {"config": cfg, "status": dict(self.status), "stats": None}
        out["status"]["running"] = bool(self._task and not self._task.done())
        try:
            root = validate_dir(cfg["archive_dir"])
        except HTTPException as e:
            out["status"]["dir_error"] = e.detail
            return out
        dbp = root / "archive.db"
        out["exists"] = dbp.is_file()
        if dbp.is_file():
            db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True, timeout=10)
            try:
                stats = {
                    "conversations": db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                    "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    "attachments": {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM attachments GROUP BY status")},
                    "backfill_pending": db.execute("SELECT COUNT(*) FROM sync_state WHERE backfill_done=0").fetchone()[0],
                    "db_bytes": dbp.stat().st_size,
                }
                out["stats"] = stats
            except sqlite3.Error as e:
                out["status"]["db_error"] = type(e).__name__
            finally:
                db.close()
        return out


manager = ArchiveManager()


# ------------------------------------------------------------------ API
class ArchiveUpdate(BaseModel):
    enabled: Optional[bool] = None
    confirm: Optional[bool] = None
    mode: Optional[str] = Field(default=None, pattern="^(all|selected)$")
    selected_chats: Optional[List[str]] = None
    archive_dir: Optional[str] = Field(default=None, max_length=500)
    backfill_history: Optional[bool] = None
    backfill_days: Optional[int] = Field(default=None, ge=0, le=3650)
    copy_media: Optional[bool] = None
    interval_sec: Optional[int] = Field(default=None, ge=5, le=3600)


class DeleteBody(BaseModel):
    confirm: str = ""


@router.get("/api/archive")
async def archive_get():
    return manager.summary()


@router.put("/api/archive")
async def archive_put(body: ArchiveUpdate):
    cfg = load_config()
    was_enabled = bool(cfg.get("enabled"))
    if body.archive_dir is not None and str(validate_dir(body.archive_dir)) != str(validate_dir(cfg["archive_dir"])):
        if was_enabled and body.enabled is not False:
            raise HTTPException(400, "请先关闭归档，再修改归档目录")
        cfg["archive_dir"] = str(validate_dir(body.archive_dir))
    for k in ("mode", "backfill_history", "backfill_days", "copy_media", "interval_sec"):
        v = getattr(body, k)
        if v is not None:
            cfg[k] = v
    if body.selected_chats is not None:
        cfg["selected_chats"] = sorted({str(x) for x in body.selected_chats if str(x).strip()})[:5000]
    if body.enabled is True and not was_enabled:
        if not body.confirm:
            raise HTTPException(400, "开启归档需要确认（confirm=true）")
        cfg["confirmed_at"] = _now_iso()
    if body.enabled is not None:
        cfg["enabled"] = bool(body.enabled)
    save_config(cfg)
    if cfg["enabled"]:
        await manager.start()
        manager.wake()
    else:
        await manager.stop()
    return manager.summary()


@router.post("/api/archive/sync")
async def archive_sync():
    cfg = load_config()
    if not cfg.get("enabled"):
        raise HTTPException(400, "归档未开启")
    await manager.start()
    manager._force = True
    manager.wake()
    return {"ok": True}


@router.post("/api/archive/delete")
async def archive_delete(body: DeleteBody):
    if body.confirm != "DELETE":
        raise HTTPException(400, "删除需要二次确认")
    cfg = load_config()
    root = validate_dir(cfg["archive_dir"])
    await manager.stop()
    cfg["enabled"] = False
    save_config(cfg)
    if not root.exists():
        return {"ok": True, "deleted": False, "detail": "目录不存在"}
    if not (root / MARKER).is_file():
        raise HTTPException(400, "该目录不是由归档功能创建的（缺少标记文件），为安全起见不删除")
    shutil.rmtree(root)
    log.info("archive: archive directory deleted by user")
    return {"ok": True, "deleted": True}
