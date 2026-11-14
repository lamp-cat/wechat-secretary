"""Load runtime settings from ~/.config/wechat-frontend (no secrets in code)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "wechat-frontend" / "config.json"


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def _read_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


class Settings:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = Path(config_path or os.environ.get("WF_CONFIG", DEFAULT_CONFIG_PATH))
        self.reload()

    def reload(self) -> None:
        raw: Dict[str, Any] = _read_json(self.config_path, {})
        self.bind_host: str = str(raw.get("bind_host", "127.0.0.1"))
        self.bind_port: int = int(raw.get("bind_port", 8088))
        self.reader_base: str = str(raw.get("reader_base", "http://127.0.0.1:5678")).rstrip("/")
        self.reader_token_file: Path = Path(
            raw.get(
                "reader_token_file",
                Path.home() / ".local/share/wechat-realtime-reader/state/web-token",
            )
        )
        self.gateway_token_file: Path = Path(
            raw.get("gateway_token_file", Path.home() / ".config/wechat-frontend/gateway-token")
        )
        self.ai_config_file: Path = Path(
            raw.get("ai_config_file", Path.home() / ".config/wechat-frontend/ai-config.json")
        )
        self.ui_config_file: Path = Path(
            raw.get("ui_config_file", Path.home() / ".local/share/wechat-frontend/ui-config.json")
        )
        self.reader_snapshot_dir: Path = Path(
            raw.get(
                "reader_snapshot_dir",
                Path.home() / ".local/share/wechat-realtime-reader/decrypted/current",
            )
        )
        origins = raw.get("cors_origins", ["*"])
        self.cors_origins: List[str] = list(origins) if isinstance(origins, list) else ["*"]
        self.require_auth_on_loopback: bool = bool(raw.get("require_auth_on_loopback", True))
        self.title: str = str(raw.get("title", "消息工作台"))

    @property
    def reader_token(self) -> str:
        return _read_secret(self.reader_token_file)

    @property
    def gateway_token(self) -> str:
        return _read_secret(self.gateway_token_file)

    def is_non_loopback(self) -> bool:
        h = self.bind_host.strip().lower()
        return h not in ("127.0.0.1", "localhost", "::1")

    def load_ai(self) -> Dict[str, Any]:
        data = _read_json(self.ai_config_file, {})
        # strip comment-only keys for callers
        return {k: v for k, v in data.items() if not str(k).startswith("_")}

    def load_ui(self) -> Dict[str, Any]:
        return _read_json(
            self.ui_config_file,
            {
                "theme": "dark",
                "refresh_interval_ms": 2500,
                "pinned": [],
                "hidden": [],
                "show_avatars": True,
                "show_unread_only": False,
                "density": "comfortable",
                "session_kinds": ["chatroom", "private", "friend", "official", "stranger"],
            },
        )

    def save_ui(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.ui_config_file.parent.mkdir(parents=True, exist_ok=True)
        allowed = {
            "theme",
            "refresh_interval_ms",
            "pinned",
            "hidden",
            "show_avatars",
            "show_unread_only",
            "density",
            "session_kinds",
        }
        cur = self.load_ui()
        for k, v in data.items():
            if k in allowed:
                cur[k] = v
        self.ui_config_file.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.ui_config_file, 0o600)
        return cur


settings = Settings()
