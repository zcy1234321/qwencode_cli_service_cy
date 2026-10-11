"""应用接入网关的策略模型与投票决策（纯逻辑，无 FastAPI 依赖，可离线单测）。

网关定位：N 个程序各自持自己的 app token 连网关（/app/v1/*），网关把
daemon token 收在自己手里，按每个 app 的 profile 注入/强制配置：

- model          → 建会话时注入 startupConfig.modelServiceId
- approval_mode  → 有工具/路径限制时强制 "default"（auto 会绕过权限投票，
                   使执行期拦截失效）
- allowed_tools  → 工具白名单。"*"=全部；"mcp:server名"=该 MCP server 的
                   全部工具（daemon 侧命名 mcp__server__tool）；其余按内置
                   工具名匹配（如 Write、Shell、Read）
- allowed_paths  → 文件路径白名单。对工具 rawInput 里的路径参数做归属
                   检查；**shell 类工具无法可靠解析命令里的路径**，配置了
                   路径白名单时 shell 必须在 allowed_tools 里显式放行
                   （"shell"），否则一律拒绝
- enabled        → UI 上的开关；关闭即拒绝新会话并关闭存量会话

执行方式：网关为每个受限 app 会话起一个后台 SSE 订阅（daemon 支持多订阅
者），对每个 permission_request 按 profile 投票——这就是 daemon 协议里
permission_mediation 的设计用途。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# rawInput 里按路径处理的参数名（小写匹配）
_PATH_KEYS = frozenset(
    {"file_path", "filepath", "file", "path", "dir", "directory", "cwd", "root"}
)

# 内置 shell/命令执行类工具的常见名字（title 匹配，小写）
_SHELL_HINTS = ("shell", "bash", "terminal", "command", "run_command")


def default_apps_file() -> Path:
    """网关的 apps.json 路径；与 settings.json 同一个 QWEN_HOME 约定。"""
    override = os.environ.get("QWEN_UI_APPS_FILE")
    if override:
        return Path(override)
    home = os.environ.get("QWEN_HOME")
    base = Path(home) if home else Path.home() / ".qwen"
    return base / "ui-apps.json"


@dataclass
class SessionRef:
    """一个活跃会话的归属记录：id + 它所在的 daemon（空 = 默认 daemon）。"""

    id: str
    daemon: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "daemon": self.daemon}

    @classmethod
    def from_raw(cls, raw: Any) -> "SessionRef":
        # 兼容旧格式：sessions 里曾经是裸字符串
        if isinstance(raw, str):
            return cls(id=raw)
        return cls(id=str(raw["id"]), daemon=str(raw.get("daemon") or ""))


@dataclass
class AppProfile:
    name: str
    token: str
    enabled: bool = True
    model: Optional[str] = None
    approval_mode: str = "auto"
    allowed_tools: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    note: str = ""
    cwd: Optional[str] = None  # 会话工作区目录；空 = daemon 默认工作区
    daemon: str = ""  # 绑定的 daemon 名称（DaemonsStore）；空 = 默认 daemon
    max_sessions: int = 0  # 并发活跃会话上限；0 = 不限
    rate_limit_rpm: int = 0  # 每分钟 prompt 上限；0 = 不限
    sessions: list[SessionRef] = field(default_factory=list)

    def has_restrictions(self) -> bool:
        return bool(self.allowed_tools or self.allowed_paths)

    def effective_approval_mode(self) -> str:
        """有限制的 app 必须用 default，否则 auto 下不产生权限事件，
        网关无从拦截。"""
        if self.has_restrictions() and self.approval_mode in ("auto", "yolo"):
            return "default"
        return self.approval_mode

    def session_ids(self) -> list[str]:
        return [s.id for s in self.sessions]

    def to_dict(self, *, reveal_token: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "token": self.token if reveal_token else self.token[:6] + "…",
            "enabled": self.enabled,
            "model": self.model,
            "approvalMode": self.approval_mode,
            "effectiveApprovalMode": self.effective_approval_mode(),
            "allowedTools": list(self.allowed_tools),
            "allowedPaths": list(self.allowed_paths),
            "note": self.note,
            "cwd": self.cwd,
            "daemon": self.daemon,
            "maxSessions": self.max_sessions,
            "rateLimitRpm": self.rate_limit_rpm,
            "sessions": [s.to_dict() for s in self.sessions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppProfile":
        return cls(
            name=str(data["name"]),
            token=str(data["token"]),
            enabled=bool(data.get("enabled", True)),
            model=data.get("model") or None,
            approval_mode=str(data.get("approvalMode", "auto")),
            allowed_tools=[str(t) for t in data.get("allowedTools") or []],
            allowed_paths=[str(p) for p in data.get("allowedPaths") or []],
            note=str(data.get("note", "")),
            cwd=str(data["cwd"]) if data.get("cwd") else None,
            daemon=str(data.get("daemon") or ""),
            max_sessions=int(data.get("maxSessions") or 0),
            rate_limit_rpm=int(data.get("rateLimitRpm") or 0),
            sessions=[SessionRef.from_raw(s) for s in data.get("sessions") or []],
        )


class AppsStore:
    """apps.json 的读写（原子写回），与 UI 服务同机持久化。

    读-改-写整体用一把锁串行化，避免并发请求（建会话/投票器/管理操作）
    交错造成 lost update。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_apps_file()
        self._lock = threading.Lock()

    def load(self) -> dict[str, AppProfile]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(apps, list):
            return {}
        out: dict[str, AppProfile] = {}
        for item in apps:
            if isinstance(item, dict) and item.get("name") and item.get("token"):
                profile = AppProfile.from_dict(item)
                out[profile.name] = profile
        return out

    def save(self, apps: dict[str, AppProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"apps": [a.to_dict(reveal_token=True) for a in apps.values()]}
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".ui-apps-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def create(
        self,
        name: str,
        *,
        model: Optional[str] = None,
        approval_mode: str = "auto",
        allowed_tools: Optional[list[str]] = None,
        allowed_paths: Optional[list[str]] = None,
        note: str = "",
        cwd: Optional[str] = None,
        daemon: str = "",
        max_sessions: int = 0,
        rate_limit_rpm: int = 0,
    ) -> AppProfile:
        with self._lock:
            apps = self.load()
            if name in apps:
                raise ValueError(f"app 已存在: {name}")
            profile = AppProfile(
                name=name,
                token=secrets.token_urlsafe(24),
                model=model,
                approval_mode=approval_mode,
                allowed_tools=allowed_tools or [],
                allowed_paths=allowed_paths or [],
                note=note,
                cwd=cwd,
                daemon=daemon,
                max_sessions=max_sessions,
                rate_limit_rpm=rate_limit_rpm,
            )
            apps[name] = profile
            self.save(apps)
            return profile

    def update(self, name: str, **fields: Any) -> AppProfile:
        with self._lock:
            apps = self.load()
            if name not in apps:
                raise KeyError(name)
            p = apps[name]
            for key, attr in (
                ("model", "model"),
                ("approvalMode", "approval_mode"),
                ("note", "note"),
                ("daemon", "daemon"),
            ):
                if key in fields:
                    setattr(p, attr, fields[key])
            if "cwd" in fields:
                p.cwd = str(fields["cwd"]).strip() or None
            for key, attr in (
                ("maxSessions", "max_sessions"),
                ("rateLimitRpm", "rate_limit_rpm"),
            ):
                if key in fields:
                    setattr(p, attr, max(0, int(fields[key] or 0)))
            for key, attr in (
                ("allowedTools", "allowed_tools"),
                ("allowedPaths", "allowed_paths"),
            ):
                if key in fields:
                    setattr(p, attr, [str(v) for v in fields[key] or []])
            self.save(apps)
            return p

    def set_enabled(self, name: str, enabled: bool) -> AppProfile:
        with self._lock:
            apps = self.load()
            if name not in apps:
                raise KeyError(name)
            apps[name].enabled = enabled
            self.save(apps)
            return apps[name]

    def regenerate_token(self, name: str) -> AppProfile:
        with self._lock:
            apps = self.load()
            if name not in apps:
                raise KeyError(name)
            apps[name].token = secrets.token_urlsafe(24)
            self.save(apps)
            return apps[name]

    def delete(self, name: str) -> None:
        with self._lock:
            apps = self.load()
            apps.pop(name, None)
            self.save(apps)

    def add_session(self, name: str, session_id: str, daemon: str = "") -> None:
        with self._lock:
            apps = self.load()
            if name in apps and session_id not in apps[name].session_ids():
                apps[name].sessions.append(SessionRef(id=session_id, daemon=daemon))
                self.save(apps)

    def remove_session(self, name: str, session_id: str) -> None:
        with self._lock:
            apps = self.load()
            if name in apps:
                kept = [s for s in apps[name].sessions if s.id != session_id]
                if len(kept) != len(apps[name].sessions):
                    apps[name].sessions = kept
                    self.save(apps)


# ----------------------------------------------------------------------
# 权限投票决策
# ----------------------------------------------------------------------


def _tool_texts(tool_call: dict[str, Any]) -> list[str]:
    """权限事件里可用于匹配的工具标识（全小写）。"""
    texts = []
    for key in ("title", "name", "toolName"):
        value = tool_call.get(key)
        if isinstance(value, str) and value:
            texts.append(value.lower())
    return texts


def _rule_matches(rule: str, texts: list[str]) -> bool:
    rule = rule.strip().lower()
    if not rule:
        return False
    if rule.startswith("mcp:"):
        server = rule[4:]
        return any(
            t.startswith(f"mcp__{server}__") or f"({server} mcp server)" in t
            for t in texts
        )
    return any(rule in t for t in texts)


def tool_allowed(profile: AppProfile, tool_call: dict[str, Any]) -> bool:
    """allowed_tools 为空 = 不限制；"*" = 全部；其余逐条匹配。"""
    rules = [r for r in profile.allowed_tools if r.strip()]
    if not rules:
        return True
    texts = _tool_texts(tool_call)
    if not texts:
        return "*" in [r.strip() for r in rules]
    return any(
        r.strip() == "*" or _rule_matches(r, texts) for r in rules
    )


def _looks_like_path(value: str) -> bool:
    v = value.strip()
    if len(v) < 3:
        return False
    if v.startswith(("/", "~")):
        return True
    # Windows 盘符
    return len(v) > 2 and v[1] == ":" and v[0].isalpha()


def _extract_paths(tool_call: dict[str, Any]) -> list[str]:
    raw = tool_call.get("rawInput")
    if not isinstance(raw, dict):
        return []
    found: list[str] = []
    for key, value in raw.items():
        if not isinstance(value, str):
            continue
        if key.lower() in _PATH_KEYS or _looks_like_path(value):
            found.append(value)
    return found


def _is_shell_tool(tool_call: dict[str, Any]) -> bool:
    kind = str(tool_call.get("kind") or "").lower()
    if kind == "execute":
        return True
    return any(
        hint in t for t in _tool_texts(tool_call) for hint in _SHELL_HINTS
    )


def paths_allowed(profile: AppProfile, tool_call: dict[str, Any]) -> bool:
    """allowed_paths 为空 = 不限制。shell 工具无法解析命令内路径，
    必须在 allowed_tools 里显式放行 "shell"，否则拒绝。"""
    roots = [r for r in profile.allowed_paths if r.strip()]
    if not roots:
        return True
    if _is_shell_tool(tool_call):
        return any(
            r.strip().lower() in ("*", "shell") for r in profile.allowed_tools
        )
    paths = _extract_paths(tool_call)
    if not paths:
        return True  # 无路径参数，无可检查
    # realpath 解析符号链接并消除 ".." / 8.3 短名差异，两侧同口径比较
    norm_roots = [os.path.realpath(os.path.expanduser(r)).lower() for r in roots]
    for p in paths:
        try:
            norm = os.path.realpath(os.path.expanduser(p)).lower()
        except (OSError, ValueError):
            return False
        if not any(
            norm == root or norm.startswith(root + os.sep)
            for root in norm_roots
        ):
            return False
    return True


def _pick_option(options: list[dict[str, Any]], kinds: tuple[str, ...]) -> Optional[str]:
    """按 kind 精确挑选选项；**不做"第一个选项"兜底**——拒绝绝不能退化成
    选中一个 allow 选项。"""
    for kind in kinds:
        for opt in options:
            if isinstance(opt, dict) and str(opt.get("kind", "")).lower() == kind:
                option_id = opt.get("optionId")
                if isinstance(option_id, str):
                    return option_id
    return None


def evaluate_permission(
    profile: AppProfile, tool_call: dict[str, Any]
) -> tuple[bool, str]:
    """(allowed, 可读原因)，供投票和审计日志共用。"""
    if not tool_allowed(profile, tool_call):
        return False, "工具不在 allowed_tools 白名单内"
    if not paths_allowed(profile, tool_call):
        return False, "路径不在 allowed_paths 内（或 shell 未显式放行）"
    return True, "工具与路径均在白名单内"


def build_vote(
    options: list[dict[str, Any]], allowed: bool
) -> Optional[dict[str, Any]]:
    if allowed:
        option_id = _pick_option(options, ("allow_once", "allow_always"))
        if option_id is None:
            return None
        return {"outcome": {"outcome": "selected", "optionId": option_id}}
    option_id = _pick_option(options, ("reject_once", "reject_always"))
    if option_id is None:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def decide_vote(
    profile: AppProfile, permission_data: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """对一个 permission_request 事件按 profile 决策。

    返回 None = 无限制不投票（留给其它审批方）；否则返回投票请求体
    （POST /session/:id/permission/:requestId 的 body）。
    """
    if not profile.has_restrictions():
        return None
    tool_call = permission_data.get("toolCall")
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    allowed, _reason = evaluate_permission(profile, tool_call)
    options = [
        o for o in (permission_data.get("options") or []) if isinstance(o, dict)
    ]
    return build_vote(options, allowed)


# ----------------------------------------------------------------------
# 速率限制（内存态滑动窗口，进程重启清零）
# ----------------------------------------------------------------------


class RateLimiter:
    """每 app 每分钟 prompt 数上限。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, rpm: int, now: Optional[float] = None) -> Optional[float]:
        """None = 放行；否则返回建议的 Retry-After 秒数。"""
        if rpm <= 0:
            return None
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, [])
        hits[:] = [t for t in hits if now - t < 60.0]
        if len(hits) >= rpm:
            return max(1.0, 60.0 - (now - hits[0]))
        hits.append(now)
        return None


# ----------------------------------------------------------------------
# 多 daemon 注册表
# ----------------------------------------------------------------------


@dataclass
class DaemonEndpoint:
    name: str
    url: str
    token: Optional[str] = None
    enabled: bool = True
    note: str = ""

    def to_dict(self, *, reveal_token: bool = False) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "token": (
                self.token
                if reveal_token
                else ((self.token[:6] + "…") if self.token else None)
            ),
            "enabled": self.enabled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DaemonEndpoint":
        return cls(
            name=str(data["name"]),
            url=str(data["url"]).rstrip("/"),
            token=data.get("token") or None,
            enabled=bool(data.get("enabled", True)),
            note=str(data.get("note", "")),
        )


def default_daemons_file() -> Path:
    override = os.environ.get("QWEN_UI_DAEMONS_FILE")
    if override:
        return Path(override)
    home = os.environ.get("QWEN_HOME")
    base = Path(home) if home else Path.home() / ".qwen"
    return base / "ui-daemons.json"


class DaemonsStore:
    """ui-daemons.json 的读写（原子写回）。名为 "" 的默认 daemon 不入库，
    由 UI 服务的 QWEN_SERVE_URL/QWEN_SERVER_TOKEN 提供。读-改-写加锁，
    与 AppsStore 同一理由。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_daemons_file()
        self._lock = threading.Lock()

    def load(self) -> dict[str, DaemonEndpoint]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("daemons") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return {}
        out: dict[str, DaemonEndpoint] = {}
        for item in items:
            if isinstance(item, dict) and item.get("name") and item.get("url"):
                ep = DaemonEndpoint.from_dict(item)
                out[ep.name] = ep
        return out

    def save(self, daemons: dict[str, DaemonEndpoint]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "daemons": [d.to_dict(reveal_token=True) for d in daemons.values()]
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".ui-daemons-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def create(
        self,
        name: str,
        url: str,
        *,
        token: Optional[str] = None,
        note: str = "",
    ) -> DaemonEndpoint:
        with self._lock:
            daemons = self.load()
            if name in daemons:
                raise ValueError(f"daemon 已存在: {name}")
            ep = DaemonEndpoint(name=name, url=url.rstrip("/"), token=token, note=note)
            daemons[name] = ep
            self.save(daemons)
            return ep

    def set_enabled(self, name: str, enabled: bool) -> DaemonEndpoint:
        with self._lock:
            daemons = self.load()
            if name not in daemons:
                raise KeyError(name)
            daemons[name].enabled = enabled
            self.save(daemons)
            return daemons[name]

    def delete(self, name: str) -> None:
        with self._lock:
            daemons = self.load()
            daemons.pop(name, None)
            self.save(daemons)
