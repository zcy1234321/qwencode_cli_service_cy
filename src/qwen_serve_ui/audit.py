"""应用接入网关的审计日志：ui-audit.jsonl（JSON Lines，每条一行）。

覆盖：鉴权失败、会话建/关、prompt、权限投票决策（含原因与命中的选项）、
配额拒绝、管理操作（app/daemon 增删改、开关、token 重置）。
默认路径与 ui-apps.json 同目录（QWEN_HOME 或 ~/.qwen），可用
QWEN_UI_AUDIT_FILE 覆盖。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# 读审计尾部时最多回读的字节数（防止日志文件长大后读爆内存）
_TAIL_READ_BYTES = 512 * 1024


def default_audit_file() -> Path:
    override = os.environ.get("QWEN_UI_AUDIT_FILE")
    if override:
        return Path(override)
    home = os.environ.get("QWEN_HOME")
    base = Path(home) if home else Path.home() / ".qwen"
    return base / "ui-audit.jsonl"


class AuditLog:
    """追加写 JSONL 审计日志。线程/协程安全（单锁串行化）。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_audit_file()
        self._lock = threading.Lock()

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # 审计写失败不阻断业务请求

    def read_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """最新的 limit 条，新的在前。解析失败的行跳过。"""
        if not self.path.exists():
            return []
        with self._lock:
            try:
                size = self.path.stat().st_size
                with self.path.open("rb") as fh:
                    if size > _TAIL_READ_BYTES:
                        fh.seek(-_TAIL_READ_BYTES, os.SEEK_END)
                    raw = fh.read().decode("utf-8", "replace")
            except OSError:
                return []
        out: list[dict[str, Any]] = []
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue  # 尾部截断的半行
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
            if len(out) >= limit:
                break
        return out
