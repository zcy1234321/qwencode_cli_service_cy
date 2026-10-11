"""聊天历史本地缓存：ui-chat-cache/<sid>.jsonl（JSON Lines，每条事件一行）。

用途：浏览器刷新后恢复聊天记录的退化方案。daemon 支持 transcript
（session_transcript 能力）时以 daemon 为准；拿不到时回放本地缓存。

缓存内容：管理面会话的 SSE 事件（data: 行解析后的 envelope，去掉 id）
加合成的 user_message_chunk（prompt 发出时记录，SSE 里不含用户自己的话）。
默认目录 <QWEN_HOME>/ui-chat-cache，可用 QWEN_UI_CHAT_CACHE_DIR 覆盖。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

# 单会话缓存上限：超过后保留尾部一半（聊天缓存比审计日志涨得快得多）
_MAX_FILE_BYTES = 4 * 1024 * 1024
_KEEP_TAIL_BYTES = 2 * 1024 * 1024


def default_cache_dir() -> Path:
    override = os.environ.get("QWEN_UI_CHAT_CACHE_DIR")
    if override:
        return Path(override)
    home = os.environ.get("QWEN_HOME")
    base = Path(home) if home else Path.home() / ".qwen"
    return base / "ui-chat-cache"


def _safe_name(sid: str) -> str:
    """session id → 文件名；只留字母数字和 -_，防止路径逃逸。"""
    return "".join(c for c in sid if c.isalnum() or c in "-_") or "unknown"


class ChatCache:
    """按会话追加写 JSONL 事件缓存。线程/协程安全（单锁串行化）。"""

    def __init__(
        self,
        directory: Optional[Path] = None,
        *,
        max_bytes: int = _MAX_FILE_BYTES,
        keep_tail_bytes: int = _KEEP_TAIL_BYTES,
    ) -> None:
        self.dir = directory or default_cache_dir()
        self.max_bytes = max_bytes
        self.keep_tail_bytes = keep_tail_bytes
        self._lock = threading.Lock()

    def _file(self, sid: str) -> Path:
        return self.dir / f"{_safe_name(sid)}.jsonl"

    def append(self, sid: str, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            path = self._file(sid)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                if path.stat().st_size > self.max_bytes:
                    self._rotate(path)
            except OSError:
                pass  # 缓存写失败不阻断业务请求

    def _rotate(self, path: Path) -> None:
        """超限后只保留尾部 keep_tail_bytes 里的完整 JSON 行。"""
        try:
            with path.open("rb") as fh:
                fh.seek(-self.keep_tail_bytes, os.SEEK_END)
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            return
        lines = [
            ln for ln in raw.splitlines()
            if ln.strip().startswith("{")
        ]
        tmp = path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def read(self, sid: str) -> list[dict[str, Any]]:
        """全部缓存事件，按写入顺序（老的在前）。解析失败的行跳过。"""
        path = self._file(sid)
        if not path.exists():
            return []
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return []
        out: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
        return out

    def clear(self, sid: str) -> None:
        with self._lock:
            try:
                self._file(sid).unlink()
            except OSError:
                pass
