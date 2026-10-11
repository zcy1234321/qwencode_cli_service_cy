"""qwen serve daemon 的 Web 管理界面后端。

一个轻量 FastAPI 代理：浏览器只连本服务，daemon 地址和 token 只存在
服务端（QWEN_SERVE_URL / QWEN_SERVER_TOKEN），浏览器永远不接触 token。

旧版 qwen（如全局安装的 0.21.5）没有 /workspace/config/mcp/* 持久化路由，
MCP 增删改回退为直接编辑 settings.json 再调 /workspace/mcp/reload（文档
认可的流程）。这要求 UI 服务与 daemon 看到同一个 QWEN_HOME 配置目录：
默认两者都不设 QWEN_HOME、共用 ~/.qwen 即可；daemon 若以自定义 QWEN_HOME
启动，UI 服务也要 export 同一个 QWEN_HOME。

启动：python -m qwen_serve_ui 或 qwen-serve-ui（默认 127.0.0.1:8080）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .audit import AuditLog
from .chatcache import ChatCache
from .policy import (
    AppProfile,
    AppsStore,
    DaemonsStore,
    RateLimiter,
    build_vote,
    evaluate_permission,
)

DEFAULT_DAEMON_URL = "http://127.0.0.1:4170"
DEFAULT_HOST = "127.0.0.1"  # 管理面默认只绑回环；对外开放需显式 --host
DEFAULT_PORT = 8080
STATIC_DIR = Path(__file__).parent / "static"


def settings_path() -> Path:
    """daemon 的 settings.json 路径（QWEN_HOME 或 ~/.qwen）。

    必须与 daemon 看到的配置目录一致，否则 reload 后 daemon 读不到改动。
    """
    home = os.environ.get("QWEN_HOME")
    base = Path(home) if home else Path.home() / ".qwen"
    return base / "settings.json"


def read_settings() -> dict[str, Any]:
    """整体读取 settings.json；文件不存在视为空配置（其它字段保持原样靠写回保证）。"""
    path = settings_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层不是 JSON 对象")
    return data


def write_settings(data: dict[str, Any]) -> None:
    """原子写回 settings.json（临时文件 + os.replace），缩进 2 空格。"""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class UiConfig:
    """UI 服务的运行时配置（全部来自环境变量或命令行参数）。"""

    def __init__(
        self,
        daemon_url: Optional[str] = None,
        token: Optional[str] = None,
        admin_token: Optional[str] = None,
    ) -> None:
        self.daemon_url = (
            daemon_url or os.environ.get("QWEN_SERVE_URL") or DEFAULT_DAEMON_URL
        ).rstrip("/")
        self.token = token or os.environ.get("QWEN_SERVER_TOKEN")
        # 设置后 /api/*（管理面）要求 Bearer 鉴权；/app/v1/*（程序面）始终用
        # 各 app 自己的 token。
        self.admin_token = (
            admin_token
            if admin_token is not None
            else os.environ.get("QWEN_UI_ADMIN_TOKEN")
        )


def create_app(config: Optional[UiConfig] = None) -> FastAPI:
    """创建 FastAPI 应用：/api/* 代理到 daemon，/ 提供静态前端。"""
    config = config or UiConfig()
    app = FastAPI(title="qwen serve 管理界面", docs_url=None, redoc_url=None)

    headers: dict[str, str] = {}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token.strip()}"
    http = httpx.AsyncClient(base_url=config.daemon_url, headers=headers, timeout=30)

    # 聊天历史本地缓存（<QWEN_HOME>/ui-chat-cache/<sid>.jsonl）：
    # SSE 旁路记录 + prompt 时合成 user 消息，供刷新后回放。
    chat_cache = ChatCache()

    @app.middleware("http")
    async def admin_auth(request: Request, call_next: Any) -> Response:
        """管理面（/api/*）可选 Bearer 鉴权：QWEN_UI_ADMIN_TOKEN 设置后启用。"""
        if config.admin_token and request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            if not secrets.compare_digest(
                auth, f"Bearer {config.admin_token}"
            ):
                return JSONResponse(
                    {"error": "需要管理员 token", "code": "admin_auth_required"},
                    status_code=401,
                )
        return await call_next(request)

    @app.on_event("shutdown")
    async def _close_http() -> None:
        await http.aclose()
        for client in daemon_clients.values():
            await client.aclose()
        daemon_clients.clear()

    async def proxy(
        method: str,
        daemon_path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> Response:
        """转发一个普通 REST 请求，daemon 的状态码和 body 原样透传。
        client 缺省用默认 daemon；多 daemon 路由时传目标 daemon 的连接。"""
        target = client or http
        try:
            resp = await target.request(method, daemon_path, params=params, json=body)
        except httpx.HTTPError as exc:
            return Response(
                content=(
                    '{"error": "无法连接 qwen serve daemon（'
                    + str(target.base_url).rstrip("/")
                    + "）："
                    + str(exc).replace('"', "'")
                    + '"}'
                ).encode("utf-8"),
                status_code=502,
                media_type="application/json",
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    async def passthrough_body(request: Request) -> Any:
        """原样读取前端请求体；空体/非法 JSON 返回 None（daemon 自行校验）。"""
        raw = await request.body()
        if not raw:
            return None
        try:
            return await request.json()
        except Exception:
            return None

    # 版本探测缓存：key -> daemon 是否有该新版路由。
    # 只缓存确定的结果（收到任何 HTTP 响应即确定）；daemon 不可达时不缓存，
    # 下次再探测。探测不到的标志在 /api/meta 里按"有"上报，真正的请求仍会在
    # 404 时走回退，两边不会打架。
    detect_cache: dict[str, bool] = {}

    # 会话级工具覆盖（_create_session_with_tools）要临时改工作区级开关，
    # 按工作区串行化，避免并发建会话互相看到对方的临时状态。
    tool_override_locks: dict[str, asyncio.Lock] = {}

    async def has_route(key: str, daemon_path: str) -> Optional[bool]:
        """探测 daemon 是否存在某路由：True/False 缓存后返回，连接失败返回 None。"""
        if key in detect_cache:
            return detect_cache[key]
        try:
            resp = await http.request("GET", daemon_path)
        except httpx.HTTPError:
            return None
        result = resp.status_code != 404
        detect_cache[key] = result
        return result

    @app.get("/api/meta")
    async def meta() -> dict[str, Any]:
        """前端展示用元信息 + 版本能力标志（不含 token）。

        mcpPersistentConfig: 是否有 /workspace/config/mcp/*（持久化增删改/启停）
        skillsConfigView:   是否有 /workspace/config/skills（配置态 skills 列表）
        toolsList:          是否有 GET /workspace/tools（工具真实状态列表）
        探测失败（daemon 未启动）时一律按 True 上报，具体请求靠 404 回退兜底。
        """
        mcp = await has_route("mcp_config", "/workspace/config/mcp/servers")
        skills_cfg = await has_route("skills_config", "/workspace/config/skills")
        tools = await has_route("tools_list", "/workspace/tools")
        return {
            "daemonUrl": config.daemon_url,
            "adminAuth": bool(config.admin_token),
            "mcpPersistentConfig": mcp is not False,
            "skillsConfigView": skills_cfg is not False,
            "toolsList": tools is not False,
        }

    @app.get("/api/health")
    async def health() -> Response:
        return await proxy("GET", "/health", params={"deep": "1"})

    @app.get("/api/capabilities")
    async def capabilities() -> Response:
        return await proxy("GET", "/capabilities")

    @app.get("/api/models")
    async def models() -> Response:
        # 新版有 GET /workspace/models；旧版（如 0.21.13，只有 DELETE）404 时
        # 回退：直接读 settings.json 的 modelProviders 拼出列表（provider 安装
        # 本就持久化在那里）。与 MCP 回退同一个 QWEN_HOME 约定。
        resp = await proxy("GET", "/workspace/models")
        if resp.status_code != 404:
            detect_cache.setdefault("models_list", True)
            return resp
        detect_cache["models_list"] = False
        try:
            settings = read_settings()
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        providers = settings.get("modelProviders")
        out: list[dict[str, Any]] = []
        if isinstance(providers, dict):
            for pid, entries in providers.items():
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if isinstance(e, dict) and e.get("id"):
                        out.append(
                            {
                                "modelId": str(e["id"]),
                                "name": str(e.get("name") or e["id"]),
                                "authType": str(pid),
                                "baseUrl": str(e.get("baseUrl") or ""),
                                "purpose": "chat",
                            }
                        )
        # 当前选中的模型（settings.model.name），前端可用于标记
        model_cfg = settings.get("model")
        selected = (
            model_cfg.get("name")
            if isinstance(model_cfg, dict) and isinstance(model_cfg.get("name"), str)
            else None
        )
        return {"models": out, "selected": selected, "via": "settings"}

    @app.post("/api/providers")
    async def install_provider(request: Request) -> Response:
        return await proxy(
            "POST", "/workspace/auth/provider", body=await passthrough_body(request)
        )

    @app.post("/api/models/fetch")
    async def fetch_provider_models(request: Request) -> Response:
        """代前端拉取 OpenAI 兼容端点的模型列表（浏览器直连会被 CORS 拦）。

        body: {baseUrl, apiKey?}；依次试 {baseUrl}/models 与
        {baseUrl}/v1/models，返回 {"models": [id, ...]}。
        """
        body = await passthrough_body(request)
        body = body if isinstance(body, dict) else {}
        base_url = str(body.get("baseUrl") or "").strip().rstrip("/")
        api_key = str(body.get("apiKey") or "").strip()
        if not base_url:
            return JSONResponse(
                {"error": "baseUrl 必填", "code": "invalid_request"},
                status_code=400,
            )
        if not base_url.startswith(("http://", "https://")):
            # 收窄 SSRF 面：只允许 http(s)，挡掉 file:// 等其它 scheme
            return JSONResponse(
                {"error": "baseUrl 必须是 http(s) 地址", "code": "invalid_request"},
                status_code=400,
            )
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        candidates = [base_url + "/models"]
        if not base_url.endswith("/v1"):
            candidates.append(base_url + "/v1/models")
        last_err = ""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for url in candidates:
                    try:
                        resp = await client.get(url, headers=headers)
                    except httpx.HTTPError as exc:
                        last_err = str(exc)
                        continue
                    if resp.status_code == 404 and url != candidates[-1]:
                        continue  # 试下一个候选路径
                    if resp.status_code != 200:
                        return JSONResponse(
                            {
                                "error": f"上游返回 HTTP {resp.status_code}："
                                + resp.text[:200],
                                "code": "upstream_error",
                            },
                            status_code=502,
                        )
                    try:
                        data = resp.json()
                    except ValueError:
                        return JSONResponse(
                            {"error": "上游响应不是 JSON", "code": "bad_response"},
                            status_code=502,
                        )
                    items = data.get("data") if isinstance(data, dict) else None
                    if not isinstance(items, list):
                        return JSONResponse(
                            {"error": "响应里没有 models 列表（非 OpenAI 格式）",
                             "code": "bad_response"},
                            status_code=502,
                        )
                    ids = sorted(
                        str(m.get("id"))
                        for m in items
                        if isinstance(m, dict) and m.get("id")
                    )
                    return {"models": ids, "via": url}
        except httpx.HTTPError as exc:
            last_err = str(exc)
        return JSONResponse(
            {"error": f"无法连接 {base_url}：{last_err}", "code": "fetch_failed"},
            status_code=502,
        )

    @app.post("/api/workspace/reload")
    async def workspace_reload() -> Response:
        return await proxy("POST", "/workspace/reload", body={})

    # ---- MCP ----
    # 新版 qwen：/workspace/config/mcp/*（持久化到 settings，可启停）。
    # 旧版（如 0.21.5）没有这些路由。运行时路由 /workspace/mcp/servers 要求
    # X-Qwen-Client-Id 已在 bridge 注册（只有会话 attach 的 client 才算），
    # UI 服务拿不到合法 id，因此 404 时回退为：直接改 settings.json 里的
    # mcpServers / mcp.excluded，再调 /workspace/mcp/reload 让 daemon 生效。

    async def reload_mcp() -> None:
        """改完 settings.json 后通知 daemon 重载；失败静默（配置已持久化）。"""
        try:
            await http.request("POST", "/workspace/mcp/reload", json={})
        except httpx.HTTPError:
            pass

    def settings_io_error(exc: BaseException) -> JSONResponse:
        return JSONResponse(
            {"error": f"读写 settings.json 失败：{exc}", "code": "settings_io"},
            status_code=502,
        )

    @app.get("/api/mcp/servers")
    async def mcp_servers() -> Response:
        resp = await proxy("GET", "/workspace/config/mcp/servers")
        if resp.status_code == 404:
            detect_cache["mcp_config"] = False
            return await proxy("GET", "/workspace/mcp")
        detect_cache.setdefault("mcp_config", True)
        return resp

    @app.put("/api/mcp/servers/{name}")
    async def mcp_put(name: str, request: Request) -> Response:
        body = await passthrough_body(request)
        resp = await proxy(
            "PUT", f"/workspace/config/mcp/servers/{name}", body=body
        )
        if resp.status_code != 404:
            detect_cache.setdefault("mcp_config", True)
            if 200 <= resp.status_code < 300:
                # 附加持久化标记，前端与旧版回退路径区分提示
                try:
                    payload = json.loads(resp.body)
                    out = payload if isinstance(payload, dict) else {"result": payload}
                    out["persisted"] = True
                    out["via"] = "api"
                    return JSONResponse(out, status_code=resp.status_code)
                except ValueError:
                    pass
            return resp
        detect_cache["mcp_config"] = False
        config_obj = body.get("config") if isinstance(body, dict) else None
        if not isinstance(config_obj, dict):
            return JSONResponse(
                {"error": "config 必填且必须是对象", "code": "invalid_request"},
                status_code=400,
            )
        try:
            settings = read_settings()
            servers = settings.get("mcpServers")
            if not isinstance(servers, dict):
                servers = {}
            servers[name] = config_obj
            settings["mcpServers"] = servers
            write_settings(settings)
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        await reload_mcp()
        return JSONResponse({"ok": True, "persisted": True, "via": "settings"})

    @app.delete("/api/mcp/servers/{name}")
    async def mcp_delete(name: str, scope: str = Query("user")) -> Response:
        resp = await proxy(
            "DELETE",
            f"/workspace/config/mcp/servers/{name}",
            params={"scope": scope},
        )
        if resp.status_code != 404:
            detect_cache.setdefault("mcp_config", True)
            return resp
        detect_cache["mcp_config"] = False
        try:
            settings = read_settings()
            servers = settings.get("mcpServers")
            if isinstance(servers, dict):
                servers.pop(name, None)  # 不存在也视为成功（幂等）
            else:
                settings["mcpServers"] = {}
            write_settings(settings)
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        await reload_mcp()
        return JSONResponse({"ok": True, "persisted": True, "via": "settings"})

    @app.post("/api/mcp/{name}/enable")
    async def mcp_enable(name: str) -> Response:
        resp = await proxy("POST", f"/workspace/config/mcp/{name}/enable", body={})
        if resp.status_code != 404:
            return resp
        # 旧版回退：从 settings.mcp.excluded（glob 列表）移除该 server
        detect_cache["mcp_config"] = False
        try:
            settings = read_settings()
            mcp_section = settings.get("mcp")
            if not isinstance(mcp_section, dict):
                mcp_section = {}
            excluded = mcp_section.get("excluded")
            if not isinstance(excluded, list):
                excluded = []
            mcp_section["excluded"] = [e for e in excluded if e != name]
            settings["mcp"] = mcp_section
            write_settings(settings)
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        await reload_mcp()
        return JSONResponse({"ok": True, "persisted": True, "via": "settings"})

    @app.post("/api/mcp/{name}/disable")
    async def mcp_disable(name: str) -> Response:
        resp = await proxy("POST", f"/workspace/config/mcp/{name}/disable", body={})
        if resp.status_code != 404:
            return resp
        # 旧版回退：把该 server 加进 settings.mcp.excluded（glob 列表）
        detect_cache["mcp_config"] = False
        try:
            settings = read_settings()
            mcp_section = settings.get("mcp")
            if not isinstance(mcp_section, dict):
                mcp_section = {}
            excluded = mcp_section.get("excluded")
            if not isinstance(excluded, list):
                excluded = []
            if name not in excluded:
                excluded.append(name)
            mcp_section["excluded"] = excluded
            settings["mcp"] = mcp_section
            write_settings(settings)
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        await reload_mcp()
        return JSONResponse({"ok": True, "persisted": True, "via": "settings"})

    @app.post("/api/mcp/reload")
    async def mcp_reload() -> Response:
        return await proxy("POST", "/workspace/mcp/reload", body={})

    # ---- Skills ----

    @app.get("/api/skills")
    async def skills() -> Response:
        # 新版有 /workspace/config/skills（配置态，带 status）；旧版（如 0.21.5）
        # 只有 /workspace/skills。404 时回退，前端再兼容两种条目 shape。
        resp = await proxy("GET", "/workspace/config/skills")
        if resp.status_code == 404:
            detect_cache["skills_config"] = False
            return await proxy("GET", "/workspace/skills")
        detect_cache.setdefault("skills_config", True)
        return resp

    @app.post("/api/skills/{name}/enable")
    async def skill_enable(name: str, request: Request) -> Response:
        return await proxy(
            "POST",
            f"/workspace/skills/{name}/enable",
            body=await passthrough_body(request),
        )

    @app.delete("/api/skills/{name}")
    async def skill_delete(name: str, scope: str = Query("global")) -> Response:
        return await proxy(
            "DELETE", f"/workspace/skills/{name}", params={"scope": scope}
        )

    @app.post("/api/skills/install")
    async def skill_install(request: Request) -> Response:
        return await proxy(
            "POST", "/workspace/skills/install", body=await passthrough_body(request)
        )

    # ---- 工具 ----

    @app.get("/api/tools")
    async def tools_list() -> Response:
        # 两个版本的 qwen 都有 GET /workspace/tools；若 404（更老的版本），
        # 返回空列表让前端回退到硬编码名单。
        resp = await proxy("GET", "/workspace/tools")
        if resp.status_code == 404:
            detect_cache["tools_list"] = False
            return JSONResponse({"tools": []})
        detect_cache.setdefault("tools_list", True)
        return resp

    @app.post("/api/tools/{name}/enable")
    async def tool_enable(name: str, request: Request) -> Response:
        return await proxy(
            "POST",
            f"/workspace/tools/{name}/enable",
            body=await passthrough_body(request),
        )

    # ---- 工作区 ----

    @app.get("/api/workspaces")
    async def workspaces() -> Response:
        """已注册工作区列表。旧版无持久化注册表（501）时回退为只报默认工作区。"""
        resp = await proxy("GET", "/workspace-registrations")
        if resp.status_code != 501:
            return resp
        caps = await proxy("GET", "/capabilities")
        primary = None
        try:
            primary = json.loads(caps.body).get("workspaceCwd")
        except ValueError:
            pass
        return {
            "primaryWorkspace": primary,
            "entries": [],
            "via": "capabilities",
        }

    @app.get("/api/fs/suggest")
    async def fs_suggest(path: str = Query("")) -> Response:
        """目录浏览：path 为空时列盘符/根目录（本机 UI 服务端计算），
        否则代理 daemon 的 /workspace-path-suggestions（列该目录下的子目录）。"""
        if not path.strip():
            if os.name == "nt":
                drives = [
                    f"{chr(c)}:\\"
                    for c in range(ord("C"), ord("Z") + 1)
                    if os.path.exists(f"{chr(c)}:\\")
                ]
                return {
                    "dir": "",
                    "sep": "\\",
                    "suggestions": [
                        {"name": d, "path": d} for d in drives
                    ],
                    "truncated": False,
                }
            return {
                "dir": "/",
                "sep": "/",
                "suggestions": [{"name": "/", "path": "/"}],
                "truncated": False,
            }
        prefix = path.strip()
        # 不带尾分隔符时 daemon 会按"前缀过滤"模式；浏览子目录要补分隔符
        if not prefix.endswith(("/", "\\")):
            prefix += "\\" if os.name == "nt" else "/"
        return await proxy(
            "GET", "/workspace-path-suggestions", params={"prefix": prefix}
        )

    # ---- 会话 ----

    def _norm_ws_path(p: str) -> str:
        return os.path.normcase(os.path.normpath(p.strip()))

    async def _primary_workspace_cwd() -> Optional[str]:
        try:
            resp = await http.request("GET", "/capabilities")
            if resp.status_code == 200:
                cwd = resp.json().get("workspaceCwd")
                return str(cwd) if cwd else None
        except (httpx.HTTPError, ValueError):
            pass
        return None

    async def _workspace_entry_for(cwd: str) -> Optional[dict[str, Any]]:
        """capabilities.workspaces[] 里按 cwd 找条目（拿 id 给限定路由用）。"""
        try:
            resp = await http.request("GET", "/capabilities")
            if resp.status_code != 200:
                return None
            for w in resp.json().get("workspaces") or []:
                if (
                    isinstance(w, dict)
                    and w.get("cwd")
                    and _norm_ws_path(str(w["cwd"])) == _norm_ws_path(cwd)
                ):
                    return w
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _read_ws_disabled_tools(cwd: str) -> list[str]:
        """工作区级 settings（<cwd>/.qwen/settings.json）的 tools.disabled。
        daemon 的工具开关路由就持久化在这里，文件即事实源。"""
        path = Path(cwd) / ".qwen" / "settings.json"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        tools = data.get("tools") if isinstance(data, dict) else None
        disabled = tools.get("disabled") if isinstance(tools, dict) else None
        if not isinstance(disabled, list):
            return []
        return [str(t) for t in disabled if isinstance(t, str)]

    async def _set_tool_enabled(
        ws_id: Optional[str], name: str, enabled: bool
    ) -> bool:
        """开/关一个工具：主工作区走 /workspace/tools/:name/enable，
        其它工作区走 /workspaces/:id/tools/:name/enable（0.21.13 起）。"""
        path = (
            f"/workspaces/{ws_id}/tools/{name}/enable"
            if ws_id
            else f"/workspace/tools/{name}/enable"
        )
        try:
            resp = await http.request("POST", path, json={"enabled": enabled})
            return 200 <= resp.status_code < 300
        except httpx.HTTPError:
            return False

    @app.get("/api/tools/state")
    async def tools_state(cwd: str = Query("")) -> Response:
        """某工作区当前全局禁用的工具列表（读 <cwd>/.qwen/settings.json）。
        cwd 为空 = daemon 主工作区。"""
        target = cwd.strip()
        if not target:
            target = (await _primary_workspace_cwd()) or ""
        if not target:
            return JSONResponse(
                {"error": "无法确定工作区", "code": "workspace_unknown"},
                status_code=502,
            )
        try:
            disabled = _read_ws_disabled_tools(target)
        except (OSError, ValueError) as exc:
            return settings_io_error(exc)
        return {"cwd": target, "disabled": disabled}

    async def _create_session_with_tools(
        body: dict[str, Any], tool_disable: list[str], tool_enable: list[str]
    ) -> Response:
        """会话级工具覆盖：daemon 没有会话级工具开关，但工具注册表只在
        会话创建（spawn）时读一次 disabled 集——所以临时改工作区级开关，
        建完会话立刻恢复。同一工作区的并发建会话用 per-workspace 锁串行化，
        避免互相看到对方的临时状态。"""
        cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else ""
        ws_cwd = cwd or (await _primary_workspace_cwd())
        if not ws_cwd:
            return JSONResponse(
                {"error": "无法确定工作区", "code": "workspace_unknown"},
                status_code=502,
            )
        ws_id: Optional[str] = None
        if cwd:
            entry = await _workspace_entry_for(cwd)
            if entry is None:
                # 未注册：先注册（与 create_session_routed 同一语义）
                reg = await proxy("POST", "/workspaces", body={"cwd": cwd})
                if not 200 <= reg.status_code < 300:
                    return reg  # scratch 冲突/嵌套等真实原因透给前端
                entry = await _workspace_entry_for(cwd)
            if entry is None or not entry.get("id"):
                return JSONResponse(
                    {
                        "error": "工作区已注册但拿不到 id（daemon 版本过旧？），"
                        "无法按会话配置工具",
                        "code": "workspace_id_unavailable",
                    },
                    status_code=502,
                )
            ws_id = str(entry["id"])
        lock = tool_override_locks.setdefault(_norm_ws_path(ws_cwd), asyncio.Lock())
        async with lock:
            try:
                current = set(_read_ws_disabled_tools(ws_cwd))
            except (OSError, ValueError) as exc:
                return settings_io_error(exc)
            to_disable = [t for t in tool_disable if t not in current]
            to_enable = [t for t in tool_enable if t in current]
            applied: list[tuple[str, bool]] = []  # (工具名, 我们设置成的 enabled 值)
            try:
                for name in to_disable:
                    if await _set_tool_enabled(ws_id, name, False):
                        applied.append((name, False))
                for name in to_enable:
                    if await _set_tool_enabled(ws_id, name, True):
                        applied.append((name, True))
                resp = await create_session_routed(http, body)
            finally:
                # 逆序恢复全局状态；恢复失败不掩盖建会话结果
                for name, enabled in reversed(applied):
                    await _set_tool_enabled(ws_id, name, not enabled)
        if resp.status_code == 200:
            try:
                payload = json.loads(resp.body)
                if isinstance(payload, dict):
                    payload["toolOverrides"] = {
                        "disabled": tool_disable,
                        "enabled": tool_enable,
                    }
                    audit.write(
                        "session_tool_overrides",
                        session=str(payload.get("sessionId") or ""),
                        workspace=ws_cwd,
                        disabled=",".join(tool_disable),
                        enabled=",".join(tool_enable),
                    )
                    return JSONResponse(payload, status_code=200)
            except ValueError:
                pass
        return resp

    async def create_session_routed(
        client: httpx.AsyncClient, body: dict[str, Any]
    ) -> Response:
        """POST /session，带工作区自动注册：daemon 拒绝未注册的 cwd
        （400 workspace_mismatch）时先 POST /workspaces 注册再重试一次。"""
        resp = await proxy("POST", "/session", body=body, client=client)
        cwd = body.get("cwd")
        if (
            resp.status_code == 400
            and isinstance(cwd, str)
            and cwd
            and b"workspace_mismatch" in resp.body
        ):
            reg = await proxy(
                "POST", "/workspaces", body={"cwd": cwd}, client=client
            )
            if 200 <= reg.status_code < 300:
                resp = await proxy("POST", "/session", body=body, client=client)
            else:
                return reg  # 注册失败，把真实原因透给调用方
        return resp

    @app.post("/api/sessions")
    async def create_session(request: Request) -> Response:
        body = await passthrough_body(request)
        body = body if isinstance(body, dict) else {}
        # 会话级工具覆盖（UI 自定义字段，剥离后再透传给 daemon）
        raw_td = body.pop("toolDisable", None)
        raw_te = body.pop("toolEnable", None)
        tool_disable = (
            [str(t) for t in raw_td if isinstance(t, str)]
            if isinstance(raw_td, list)
            else []
        )
        tool_enable = (
            [str(t) for t in raw_te if isinstance(t, str)]
            if isinstance(raw_te, list)
            else []
        )
        if tool_disable or tool_enable:
            return await _create_session_with_tools(body, tool_disable, tool_enable)
        return await create_session_routed(http, body)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        resp = await proxy("DELETE", f"/session/{sid}")
        if resp.status_code in (200, 204, 404):
            chat_cache.clear(sid)
        return resp

    @app.post("/api/sessions/{sid}/resume")
    async def resume_session(sid: str, request: Request) -> Response:
        return await proxy(
            "POST", f"/session/{sid}/resume", body=await passthrough_body(request)
        )

    @app.post("/api/sessions/{sid}/prompt")
    async def session_prompt(sid: str, request: Request) -> Response:
        body = await passthrough_body(request)
        resp = await proxy("POST", f"/session/{sid}/prompt", body=body)
        # 用户消息不进 SSE 事件流，在这里合成一条 user_message_chunk 落缓存，
        # 供刷新后的本地回放（daemon transcript 不可用时）使用
        if resp.status_code == 202 and isinstance(body, dict):
            prompt_val = body.get("prompt")
            if isinstance(prompt_val, list):
                text = "".join(
                    str(p.get("text", ""))
                    for p in prompt_val
                    if isinstance(p, dict)
                )
            else:
                text = str(prompt_val or "")
            if text:
                chat_cache.append(sid, {
                    "type": "session_update",
                    "data": {"update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                    }},
                })
        return resp

    @app.get("/api/sessions/{sid}/transcript")
    async def session_transcript(sid: str, request: Request) -> Response:
        """daemon 会话记录（session_transcript 能力），query 原样透传
        （limit/cursor/direction）。旧版 daemon 404 时前端回退本地缓存。"""
        query = request.url.query
        path = f"/session/{sid}/transcript" + (f"?{query}" if query else "")
        return await proxy("GET", path)

    @app.get("/api/sessions/{sid}/history")
    async def session_history(sid: str) -> dict[str, Any]:
        """本地聊天缓存（SSE 旁路 + 合成 user 消息），刷新恢复的退化方案。"""
        return {"events": chat_cache.read(sid), "via": "cache"}

    @app.post("/api/sessions/{sid}/approval-mode")
    async def session_approval_mode(sid: str, request: Request) -> Response:
        return await proxy(
            "POST", f"/session/{sid}/approval-mode", body=await passthrough_body(request)
        )

    @app.post("/api/sessions/{sid}/cancel")
    async def session_cancel(sid: str) -> Response:
        return await proxy("POST", f"/session/{sid}/cancel", body={})

    @app.post("/api/sessions/{sid}/permission/{request_id}")
    async def session_permission(sid: str, request_id: str, request: Request) -> Response:
        return await proxy(
            "POST",
            f"/session/{sid}/permission/{request_id}",
            body=await passthrough_body(request),
        )

    @app.get("/api/sessions/{sid}/events")
    async def session_events(
        sid: str,
        request: Request,
        last_event_id: Optional[int] = Query(default=None),
        epoch: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        """SSE 纯转发：字节流原样回给浏览器；客户端断开时关上游。
        旁路把 data: 行解析后写进聊天缓存（ui-chat-cache），供刷新回放。"""

        async def stream() -> AsyncIterator[bytes]:
            upstream_headers = {"Accept": "text/event-stream"}
            if last_event_id is not None:
                upstream_headers["Last-Event-ID"] = str(last_event_id)
            if epoch:
                upstream_headers["X-Qwen-Event-Epoch"] = epoch
            buf = b""
            try:
                async with http.stream(
                    "GET",
                    f"/session/{sid}/events",
                    headers=upstream_headers,
                    timeout=None,
                ) as upstream:
                    async for chunk in upstream.aiter_raw():
                        if await request.is_disconnected():
                            break
                        buf += chunk
                        # 旁路：逐完整行解析（不影响透传内容与时序）
                        while True:
                            nl = buf.find(b"\n")
                            if nl < 0:
                                break
                            line, buf = buf[:nl], buf[nl + 1:]
                            _tap_cache_line(sid, line)
                        yield chunk
            except httpx.HTTPError:
                # 上游断开或不可达：静默结束流，前端会按 lastEventId 重连。
                return

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _tap_cache_line(sid: str, line: bytes) -> None:
        """SSE data: 行 → 解析 envelope → 落聊天缓存。坏行静默忽略。
        user_message_chunk 跳过：用户消息由 prompt 路由合成记录（SSE 回显
        会与它重复，且旧版 daemon 不一定回显）。"""
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if not raw:
            return
        try:
            env = json.loads(raw)
        except ValueError:
            return
        if not (isinstance(env, dict) and isinstance(env.get("type"), str)):
            return
        data = env.get("data")
        if isinstance(data, dict):
            update = data.get("update")
            update = update if isinstance(update, dict) else data
            if update.get("sessionUpdate") == "user_message_chunk":
                return
        env = {k: v for k, v in env.items() if k != "id"}
        chat_cache.append(sid, env)

    # ---- 应用接入网关 ----
    # 管理面 /api/apps/* 配置 N 个接入程序（每个有独立 token、模型、工具/路径
    # 白名单、开关）；程序面 /app/v1/* 供程序接入——路径与 daemon 一致，把
    # qwen_serve_client 的 base_url 指向 http://<ui-host>:8080/app/v1 即可。
    #
    # 强制前提：daemon 必须绑 127.0.0.1 且只有网关知道 daemon token，否则
    # 程序可以绕过网关直连 daemon，策略形同虚设。
    #
    # 执行期拦截：受限 app（配了 allowed_tools/allowed_paths）的会话被强制
    # default 审批模式，网关后台订阅其事件流，对每个 permission_request 按
    # profile 投票。受限判断为纯前缀/路径匹配——shell 命令内的路径无法解析，
    # 属于已声明的粗粒度边界。

    apps_store = AppsStore()
    daemons_store = DaemonsStore()
    audit = AuditLog()
    rate_limiter = RateLimiter()
    voters: dict[str, asyncio.Task[None]] = {}  # session_id -> 投票任务
    daemon_clients: dict[str, httpx.AsyncClient] = {}  # daemon 名 -> 连接缓存

    def _resolve_daemon(
        name: str,
    ) -> Optional[tuple[httpx.AsyncClient, str, Optional[str]]]:
        """name="" → 默认 daemon（QWEN_SERVE_URL）；否则查注册表。
        返回 (client, url, token)；名称未知或已禁用 → None。"""
        if not name:
            return http, config.daemon_url, config.token
        ep = daemons_store.load().get(name)
        if ep is None or not ep.enabled:
            return None
        client = daemon_clients.get(name)
        if client is None:
            daemon_headers: dict[str, str] = {}
            if ep.token:
                daemon_headers["Authorization"] = f"Bearer {ep.token.strip()}"
            client = httpx.AsyncClient(
                base_url=ep.url, headers=daemon_headers, timeout=30
            )
            daemon_clients[name] = client
        return client, ep.url, ep.token

    def _auth_app(request: Request) -> Optional[AppProfile]:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[7:].strip()
        for profile in apps_store.load().values():
            if secrets.compare_digest(profile.token, token):
                return profile
        audit.write(
            "app_auth_failed",
            path=request.url.path,
            remote=request.client.host if request.client else "",
        )
        return None

    async def _vote_loop(app_name: str, session_id: str, daemon_name: str) -> None:
        """后台订阅受限 app 会话的事件流，按 profile 自动投权限票并审计。"""
        from qwen_serve_client import AsyncQwenServeClient
        from qwen_serve_client import QwenServeError as ClientError

        resolved = _resolve_daemon(daemon_name)
        if resolved is None:
            voters.pop(session_id, None)
            return
        _, daemon_url, daemon_token = resolved
        client = AsyncQwenServeClient(daemon_url, daemon_token)
        try:
            # 投票器订阅贯穿会话整个生命周期，不受累计重连预算限制
            async for event in client.iter_events(
                session_id, last_event_id=0, max_total_reconnects=None
            ):
                if event.type == "permission_request":
                    profile = apps_store.load().get(app_name)
                    if profile is None or not profile.enabled:
                        return
                    request_id = event.data.get("requestId")
                    if not isinstance(request_id, str):
                        continue
                    tool_call = event.data.get("toolCall")
                    tool_call = tool_call if isinstance(tool_call, dict) else {}
                    allowed, reason = evaluate_permission(profile, tool_call)
                    options = [
                        o
                        for o in (event.data.get("options") or [])
                        if isinstance(o, dict)
                    ]
                    vote = build_vote(options, allowed)
                    if vote is None:
                        audit.write(
                            "permission_vote",
                            app=app_name,
                            session=session_id,
                            requestId=request_id,
                            tool=str(tool_call.get("title") or ""),
                            allowed=allowed,
                            reason=reason,
                            vote=None,
                            detail="无匹配选项，留给其它审批方",
                        )
                        continue
                    outcome = vote["outcome"]
                    vote_desc = (
                        outcome.get("optionId")
                        if outcome["outcome"] == "selected"
                        else "cancelled"
                    )
                    try:
                        if outcome["outcome"] == "selected":
                            await client.respond_permission(
                                session_id, request_id, outcome["optionId"]
                            )
                        else:
                            await client.cancel_permission(session_id, request_id)
                        audit.write(
                            "permission_vote",
                            app=app_name,
                            session=session_id,
                            requestId=request_id,
                            tool=str(tool_call.get("title") or ""),
                            allowed=allowed,
                            reason=reason,
                            vote=vote_desc,
                        )
                    except ClientError:
                        pass  # 请求可能已被其它投票方解决
                if event.is_terminal:
                    return
        except Exception:
            return  # 会话死亡/网络断开：投票器随会话终结
        finally:
            await client.aclose()
            voters.pop(session_id, None)

    def _start_voter(profile: AppProfile, session_id: str, daemon_name: str = "") -> None:
        if not profile.has_restrictions() or session_id in voters:
            return
        voters[session_id] = asyncio.create_task(
            _vote_loop(profile.name, session_id, daemon_name or profile.daemon)
        )

    async def _shutdown_app_sessions(name: str) -> None:
        """关闭 app 的全部存量会话（禁用/删除时调用）。"""
        profile = apps_store.load().get(name)
        if profile is None:
            return
        for ref in list(profile.sessions):
            task = voters.pop(ref.id, None)
            if task is not None:
                task.cancel()
            resolved = _resolve_daemon(ref.daemon or profile.daemon)
            if resolved is not None:
                try:
                    await resolved[0].request("DELETE", f"/session/{ref.id}")
                except httpx.HTTPError:
                    pass
            apps_store.remove_session(name, ref.id)
            audit.write(
                "app_session_closed", app=name, session=ref.id, cause="shutdown"
            )

    @app.on_event("startup")
    async def _resume_voters() -> None:
        """UI 重启后为持久化的受限会话恢复投票器（daemon 会话还活着的话）。"""
        for profile in apps_store.load().values():
            if profile.enabled:
                for ref in profile.sessions:
                    _start_voter(profile, ref.id, ref.daemon or profile.daemon)

    @app.on_event("shutdown")
    async def _stop_voters() -> None:
        for task in voters.values():
            task.cancel()
        voters.clear()

    # -- 管理面：apps CRUD --

    def _app_or_404(name: str) -> AppProfile | JSONResponse:
        profile = apps_store.load().get(name)
        if profile is None:
            return JSONResponse(
                {"error": f"app 不存在: {name}", "code": "app_not_found"},
                status_code=404,
            )
        return profile

    @app.get("/api/apps")
    async def apps_list() -> dict[str, Any]:
        return {
            "apps": [p.to_dict() for p in apps_store.load().values()],
            "gatewayBase": "/app/v1",
        }

    @app.post("/api/apps")
    async def apps_create(request: Request) -> Response:
        body = await passthrough_body(request) or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse(
                {"error": "name 必填", "code": "invalid_request"}, status_code=400
            )
        daemon_name = str(body.get("daemon") or "").strip()
        if daemon_name and daemon_name not in daemons_store.load():
            return JSONResponse(
                {"error": f"daemon 未注册: {daemon_name}", "code": "daemon_unknown"},
                status_code=400,
            )
        try:
            profile = apps_store.create(
                name,
                model=body.get("model") or None,
                approval_mode=str(body.get("approvalMode") or "auto"),
                allowed_tools=body.get("allowedTools"),
                allowed_paths=body.get("allowedPaths"),
                note=str(body.get("note") or ""),
                cwd=str(body.get("cwd") or "").strip() or None,
                daemon=daemon_name,
                max_sessions=max(0, int(body.get("maxSessions") or 0)),
                rate_limit_rpm=max(0, int(body.get("rateLimitRpm") or 0)),
            )
        except (ValueError, OSError) as exc:
            return JSONResponse(
                {"error": str(exc), "code": "invalid_request"}, status_code=400
            )
        audit.write("admin_app_created", app=name, daemon=profile.daemon)
        # token 只在创建/重置时完整返回一次
        return JSONResponse(profile.to_dict(reveal_token=True), status_code=201)

    @app.put("/api/apps/{name}")
    async def apps_update(name: str, request: Request) -> Response:
        body = await passthrough_body(request) or {}
        daemon_name = str(body.get("daemon") or "").strip()
        if daemon_name and daemon_name not in daemons_store.load():
            return JSONResponse(
                {"error": f"daemon 未注册: {daemon_name}", "code": "daemon_unknown"},
                status_code=400,
            )
        try:
            profile = apps_store.update(name, **body)
        except KeyError:
            return _app_or_404(name)  # type: ignore[return-value]
        except (ValueError, OSError) as exc:
            return JSONResponse(
                {"error": str(exc), "code": "invalid_request"}, status_code=400
            )
        audit.write("admin_app_updated", app=name)
        return JSONResponse(profile.to_dict())

    @app.post("/api/apps/{name}/enable")
    async def apps_enable(name: str, enabled: bool = Query(True)) -> Response:
        try:
            profile = apps_store.set_enabled(name, enabled)
        except KeyError:
            return _app_or_404(name)  # type: ignore[return-value]
        audit.write("admin_app_enabled" if enabled else "admin_app_disabled", app=name)
        if not enabled:
            await _shutdown_app_sessions(name)
        return JSONResponse(profile.to_dict())

    @app.post("/api/apps/{name}/token")
    async def apps_token(name: str) -> Response:
        try:
            profile = apps_store.regenerate_token(name)
        except KeyError:
            return _app_or_404(name)  # type: ignore[return-value]
        audit.write("admin_app_token_reset", app=name)
        return JSONResponse(profile.to_dict(reveal_token=True))

    @app.delete("/api/apps/{name}")
    async def apps_delete(name: str) -> Response:
        await _shutdown_app_sessions(name)
        apps_store.delete(name)
        audit.write("admin_app_deleted", app=name)
        return JSONResponse({"ok": True})

    # -- 管理面：daemon 注册表 --

    @app.get("/api/daemons")
    async def daemons_list() -> dict[str, Any]:
        """全部 daemon：默认 daemon（来自 UI 服务自身配置）+ 注册表里的额外 daemon。"""
        items = [
            {
                "name": "",
                "url": config.daemon_url,
                "token": "（来自 UI 服务环境变量）" if config.token else None,
                "enabled": True,
                "note": "默认 daemon",
                "builtin": True,
            }
        ]
        items.extend(d.to_dict() for d in daemons_store.load().values())
        return {"daemons": items}

    @app.post("/api/daemons")
    async def daemons_create(request: Request) -> Response:
        body = await passthrough_body(request) or {}
        name = str(body.get("name") or "").strip()
        url = str(body.get("url") or "").strip()
        if not name or not url:
            return JSONResponse(
                {"error": "name 和 url 必填", "code": "invalid_request"},
                status_code=400,
            )
        try:
            ep = daemons_store.create(
                name,
                url,
                token=body.get("token") or None,
                note=str(body.get("note") or ""),
            )
        except (ValueError, OSError) as exc:
            return JSONResponse(
                {"error": str(exc), "code": "invalid_request"}, status_code=400
            )
        audit.write("admin_daemon_created", daemon=name, url=ep.url)
        # token 只在创建时完整返回一次
        return JSONResponse(ep.to_dict(reveal_token=True), status_code=201)

    @app.post("/api/daemons/{name}/enable")
    async def daemons_enable(name: str, enabled: bool = Query(True)) -> Response:
        try:
            ep = daemons_store.set_enabled(name, enabled)
        except KeyError:
            return JSONResponse(
                {"error": f"daemon 不存在: {name}", "code": "daemon_not_found"},
                status_code=404,
            )
        audit.write(
            "admin_daemon_enabled" if enabled else "admin_daemon_disabled",
            daemon=name,
        )
        return JSONResponse(ep.to_dict())

    @app.delete("/api/daemons/{name}")
    async def daemons_delete(name: str) -> Response:
        if name not in daemons_store.load():
            return JSONResponse(
                {"error": f"daemon 不存在: {name}", "code": "daemon_not_found"},
                status_code=404,
            )
        users = [p.name for p in apps_store.load().values() if p.daemon == name]
        if users:
            return JSONResponse(
                {
                    "error": f"daemon 仍被 app 引用: {', '.join(users)}",
                    "code": "daemon_in_use",
                },
                status_code=409,
            )
        daemons_store.delete(name)
        client = daemon_clients.pop(name, None)
        if client is not None:
            await client.aclose()
        audit.write("admin_daemon_deleted", daemon=name)
        return JSONResponse({"ok": True})

    @app.post("/api/daemons/{name}/probe")
    async def daemons_probe(name: str) -> Response:
        """健康探测：向目标 daemon 发 GET /health，报告可达性。"""
        resolved = _resolve_daemon(name)
        if resolved is None:
            return JSONResponse(
                {"error": f"daemon 不存在或已禁用: {name}", "code": "daemon_not_found"},
                status_code=404,
            )
        client, url, _token = resolved
        try:
            resp = await client.request(
                "GET", "/health", params={"deep": "1"}, timeout=5
            )
            return JSONResponse(
                {"ok": resp.status_code == 200, "url": url,
                 "statusCode": resp.status_code},
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"ok": False, "url": url, "error": str(exc)}, status_code=200
            )

    # -- 管理面：审计日志 --

    @app.get("/api/audit")
    async def audit_tail(limit: int = Query(50)) -> dict[str, Any]:
        """最近 limit 条审计记录，新的在前（上限 200）。"""
        return {"records": audit.read_tail(min(max(1, limit), 200))}

    # -- 程序面：/app/v1 网关 --

    @app.post("/app/v1/session")
    async def app_create_session(request: Request) -> Response:
        """程序建会话：注入 profile 的模型/审批模式，登记归属，起投票器。"""
        profile = _auth_app(request)
        if profile is None:
            return JSONResponse(
                {"error": "无效的 app token", "code": "app_auth_required"},
                status_code=401,
            )
        if not profile.enabled:
            return JSONResponse(
                {"error": f"app 已禁用: {profile.name}", "code": "app_disabled"},
                status_code=403,
            )
        resolved = _resolve_daemon(profile.daemon)
        if resolved is None:
            return JSONResponse(
                {
                    "error": f"app 绑定的 daemon 不存在或已禁用: {profile.daemon}",
                    "code": "daemon_unknown",
                },
                status_code=404,
            )
        daemon_client, _, _ = resolved
        if (
            profile.max_sessions > 0
            and len(profile.sessions) >= profile.max_sessions
        ):
            audit.write(
                "app_session_quota",
                app=profile.name,
                maxSessions=profile.max_sessions,
                current=len(profile.sessions),
            )
            return JSONResponse(
                {
                    "error": (
                        f"并发会话已达上限 {profile.max_sessions}，"
                        "请先关闭旧会话"
                    ),
                    "code": "app_session_quota",
                },
                status_code=429,
            )
        body = await passthrough_body(request)
        body = body if isinstance(body, dict) else {}
        body["sessionScope"] = "thread"
        body["approvalMode"] = profile.effective_approval_mode()
        if profile.cwd:
            body["cwd"] = profile.cwd
        if profile.model:
            startup = body.get("startupConfig")
            startup = startup if isinstance(startup, dict) else {}
            startup["modelServiceId"] = profile.model
            body["startupConfig"] = startup
        resp = await create_session_routed(daemon_client, body)
        if resp.status_code == 200:
            try:
                sid = json.loads(resp.body).get("sessionId")
            except ValueError:
                sid = None
            if isinstance(sid, str):
                apps_store.add_session(profile.name, sid, profile.daemon)
                _start_voter(profile, sid, profile.daemon)
                audit.write(
                    "app_session_created",
                    app=profile.name,
                    session=sid,
                    daemon=profile.daemon or "default",
                )
        return resp

    @app.api_route(
        "/app/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def app_gateway(path: str, request: Request) -> Response:
        """程序面通用代理：会话归属校验后按会话的 daemon 路由透传。
        SSE 事件流按流转发。"""
        profile = _auth_app(request)
        if profile is None:
            return JSONResponse(
                {"error": "无效的 app token", "code": "app_auth_required"},
                status_code=401,
            )
        if not profile.enabled:
            return JSONResponse(
                {"error": f"app 已禁用: {profile.name}", "code": "app_disabled"},
                status_code=403,
            )
        # 会话归属：/session/<sid>/... 里的 sid 必须属于当前 app；
        # 路由目标取会话记录里的 daemon（建会话时所在），缺省回退 profile.daemon
        parts = path.split("/")
        sid: Optional[str] = None
        daemon_name = profile.daemon
        if len(parts) >= 2 and parts[0] == "session" and parts[1]:
            sid = parts[1]
            owner = apps_store.load().get(profile.name)
            ref = (
                next((s for s in owner.sessions if s.id == sid), None)
                if owner is not None
                else None
            )
            if ref is None:
                return JSONResponse(
                    {"error": "会话不存在或不属于本 app", "code": "session_forbidden"},
                    status_code=403,
                )
            daemon_name = ref.daemon or profile.daemon
        resolved = _resolve_daemon(daemon_name)
        if resolved is None:
            return JSONResponse(
                {
                    "error": f"daemon 不存在或已禁用: {daemon_name}",
                    "code": "daemon_unknown",
                },
                status_code=404,
            )
        daemon_client = resolved[0]
        if sid and parts[-1] == "events" and request.method == "GET":
            return await _app_events_proxy(request, sid, daemon_client)
        if (
            sid
            and len(parts) == 2
            and request.method == "DELETE"
        ):
            resp = await proxy("DELETE", f"/session/{sid}", client=daemon_client)
            task = voters.pop(sid, None)
            if task is not None:
                task.cancel()
            apps_store.remove_session(profile.name, sid)
            audit.write("app_session_closed", app=profile.name, session=sid,
                        cause="app_delete")
            return resp
        # prompt 速率限制（只对会话 prompt 计数）
        is_prompt = (
            sid is not None
            and request.method == "POST"
            and parts[-1] == "prompt"
        )
        if is_prompt and profile.rate_limit_rpm > 0:
            retry_after = rate_limiter.check(profile.name, profile.rate_limit_rpm)
            if retry_after is not None:
                audit.write(
                    "app_rate_limited",
                    app=profile.name,
                    session=sid,
                    rpm=profile.rate_limit_rpm,
                    retryAfter=round(retry_after, 1),
                )
                return JSONResponse(
                    {
                        "error": (
                            f"超出每分钟 {profile.rate_limit_rpm} 次 prompt 上限"
                        ),
                        "code": "app_rate_limited",
                        "retryAfter": round(retry_after, 1),
                    },
                    status_code=429,
                    headers={"Retry-After": str(int(retry_after) or 1)},
                )
        body = await passthrough_body(request) if request.method != "GET" else None
        if is_prompt and isinstance(body, dict):
            prompt_val = body.get("prompt")
            if isinstance(prompt_val, list):
                text = "".join(
                    str(p.get("text", ""))
                    for p in prompt_val
                    if isinstance(p, dict)
                )
            else:
                text = str(prompt_val or "")
            audit.write(
                "app_prompt",
                app=profile.name,
                session=sid,
                promptChars=len(text),
                preview=text[:200],
            )
        query = request.url.query
        daemon_path = "/" + path + (f"?{query}" if query else "")
        return await proxy(request.method, daemon_path, body=body,
                           client=daemon_client)

    async def _app_events_proxy(
        request: Request, sid: str, client: httpx.AsyncClient
    ) -> StreamingResponse:
        """app 会话的 SSE 透传（复用管理面同一语义，但走 app 鉴权）。"""

        async def stream() -> AsyncIterator[bytes]:
            # 客户端库用请求头续传；浏览器手搓 SSE 时用 query 参数，两者都认
            upstream_headers = {"Accept": "text/event-stream"}
            last_event_id = request.headers.get("last-event-id") or (
                request.query_params.get("last_event_id")
            )
            if last_event_id:
                upstream_headers["Last-Event-ID"] = last_event_id
            epoch = request.headers.get("x-qwen-event-epoch") or (
                request.query_params.get("epoch")
            )
            if epoch:
                upstream_headers["X-Qwen-Event-Epoch"] = epoch
            try:
                async with client.stream(
                    "GET",
                    f"/session/{sid}/events",
                    headers=upstream_headers,
                    timeout=None,
                ) as upstream:
                    async for chunk in upstream.aiter_raw():
                        if await request.is_disconnected():
                            break
                        yield chunk
            except httpx.HTTPError:
                return

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ---- 静态前端（必须最后挂载）----
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


def main() -> None:
    """命令行入口：解析参数并启动 uvicorn。"""
    parser = argparse.ArgumentParser(description="qwen serve Web 管理界面")
    parser.add_argument(
        "--host",
        default=os.environ.get("QWEN_UI_HOST", DEFAULT_HOST),
        help="监听地址（默认 127.0.0.1，可用 QWEN_UI_HOST 覆盖）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("QWEN_UI_PORT", DEFAULT_PORT)),
        help="监听端口（默认 8080，可用 QWEN_UI_PORT 覆盖）",
    )
    parser.add_argument(
        "--daemon",
        default=None,
        help="daemon 地址（默认读 QWEN_SERVE_URL 或 http://127.0.0.1:4170）",
    )
    args = parser.parse_args()

    import sys

    import uvicorn

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    config = UiConfig(daemon_url=args.daemon)
    if (
        args.host not in ("127.0.0.1", "localhost", "::1")
        and not config.admin_token
    ):
        # 管理面裸奔在非回环地址上：任何人都能改 settings、装 provider、
        # 重置 app token、利用 /api/models/fetch 探测内网。大声警告。
        print(
            "警告：正在监听非回环地址且未设置 QWEN_UI_ADMIN_TOKEN，"
            "管理面 /api/* 对网络内任何人开放。请设置该环境变量或改回 "
            "--host 127.0.0.1。",
            file=sys.stderr,
        )
    app = create_app(config)
    print(
        f"qwen-serve-ui：http://{args.host}:{args.port} "
        f"（代理 daemon {config.daemon_url}，浏览器无需 token）"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
