# qwen serve 推理后端 + Python 客户端

> **完整使用文档：[docs/使用指南.md](docs/使用指南.md)**（启动、Python 调用、
> 模型/MCP/skills/工具配置、多语言接入、排错、安全清单）

用 qwen-code 自带的 `qwen serve` HTTP daemon 作为中心推理后端，你的所有程序
（任何语言）通过 REST + SSE 调用；本目录提供一个对齐官方 Python SDK 使用习惯
的薄客户端 `qwen_serve_client`。

> 说明：官方 Python SDK（`qwen-code-sdk`）目前只能 spawn 本地 qwen CLI 子进程
> （stdio stream-json），**不能**连接 serve daemon。本包参考官方 SDK 的使用模式
> （一次性调用 / 多轮会话），API 为本包自定义；未来官方支持 HTTP transport 后
> 可评估迁移。

## 架构

```
你的程序 ──HTTP + Bearer token──→ qwen serve (daemon) ──→ LLM provider
                                  127.0.0.1:4170          (模型 key 只配在
                                  REST + SSE               daemon 一侧)
```

- 模型凭据集中配置在 daemon 机器的 `~/.qwen/settings.json`（`modelProviders`）
  或环境变量 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`。
  客户端只需要 daemon token，永远不接触模型 key。
- 会话级权限模式（`approvalMode`）：`auto` 无人值守自动执行工具；
  `default` 则工具调用会产生 `permission_request` 事件等待审批。

## 快速开始

> 没有模型 key 想先体验全流程：`python scripts/start_demo_stack.py`
> （假模型 + daemon + Web UI 一键拉起，浏览器开 http://127.0.0.1:8080 ）。
> Windows 启动踩坑（执行策略、cmd/PowerShell 语法、脚本签名）见
> [docs/使用指南.md](docs/使用指南.md) §2.3。

### 1. 启动 daemon

```powershell
$env:QWEN_SERVER_TOKEN = "你的密钥"
./scripts/start-server.ps1          # 默认 127.0.0.1:4170，--no-web
```

跨机器访问：`./scripts/start-server.ps1 -Hostname 0.0.0.0`（务必设强 token，
生产环境加 `--tls-cert/--tls-key`）。

### 2. 安装客户端

```bash
pip install -e .
```

### 3. 调用

```python
from qwen_serve_client import ask

print(ask("用一句话介绍你自己。"))
```

多轮会话（程序内保持上下文）：

```python
from qwen_serve_client import QwenServeClient

with QwenServeClient() as client:          # token 默认读 QWEN_SERVER_TOKEN
    session = client.create_session(approval_mode="auto")
    sid = session.session_id
    try:
        r1 = client.run_turn(sid, "记住数字 42。只回复：已记住。")
        r2 = client.run_turn(sid, "我刚才让你记住的数字是几？",
                             on_text=print)   # 流式回调
        print(r2.text, r2.stop_reason)
    finally:
        client.close_session(sid)
```

异步用法完全相同（`AsyncQwenServeClient` / `async_ask`）。

### 4. Web 管理界面（可选）

在浏览器里集成管理 daemon：聊天（多会话、流式输出、权限审批、模型/权限
模式切换、按会话独立工作区、**刷新后聊天记录自动恢复**——优先拉 daemon
transcript，旧版回退 UI 服务端本地缓存 `ui-chat-cache/`）、模型/provider
安装、MCP servers、skills、工具开关和 daemon 状态，全部在一个页面上。

```bash
pip install -e ".[ui]"     # 额外安装 fastapi + uvicorn
qwen-serve-ui               # 默认监听 127.0.0.1:8080（对外开放用 --host 0.0.0.0，此时务必设 QWEN_UI_ADMIN_TOKEN）
# 或 python -m qwen_serve_ui --port 8080
```

浏览器打开 http://localhost:8080 。daemon 地址读 `QWEN_SERVE_URL`
（默认 `http://127.0.0.1:4170`），token 读 `QWEN_SERVER_TOKEN`——
**token 只存在于 UI 服务端，浏览器永远不接触**。详见
[docs/使用指南.md](docs/使用指南.md) 的「Web 管理界面」一节。

### 5. 应用接入网关（可选，v0.2）

N 个程序接入同一个 daemon 时，把 UI 服务当成**分发中转站**：在「应用
接入」页为每个程序建一个 app（独立 token、指定模型、工具/路径白名单、
开关），程序不再直连 daemon：

```
程序 A ──Bearer app-token-A──┐
程序 B ──Bearer app-token-B──┼──→ UI 网关 :8080/app/v1 ──→ qwen serve（绑 127.0.0.1）
程序 C ──Bearer app-token-C──┘      注入 profile / 归属校验 / 自动投票
```

程序侧只改 base_url，客户端 API 完全不变：

```python
QwenServeClient("http://<网关主机>:8080/app/v1", token="<app-token>")
```

- **模型/权限注入**：建会话时强制 profile 的 `modelServiceId` 和审批模式
  （配了白名单的 app 强制 `default`——`auto` 下没有权限事件可拦）。
- **执行期拦截**：网关后台订阅受限会话的事件流，对每个
  `permission_request` 按白名单自动投票（`mcp:server名` 匹配该 MCP
  server 的全部工具；路径白名单检查工具参数，shell 命令需显式放行）。
- **配额**：每 app 可配并发会话上限（超限 429 `app_session_quota`）和
  每分钟 prompt 上限（超限 429 `app_rate_limited`，带 `Retry-After`）。
- **多 daemon**：UI 上可注册多台 `qwen serve`，每个 app 绑定其中一台；
  请求按"会话建在哪台"路由，SSE 与投票器同样走目标 daemon。
- **审计日志**：鉴权失败、会话建/关、prompt（前 200 字符）、权限投票
  决策（含原因与命中选项）、配额拒绝、管理操作，全部落
  `QWEN_HOME/ui-audit.jsonl`，页面上可查最近 50 条。
- **隔离**：会话归属按 app 校验，A 的 token 访问 B 的会话返回 403；
  UI 上关停 app 即拒绝新会话并关闭存量会话。
- **管理面鉴权**：设 `QWEN_UI_ADMIN_TOKEN` 后 `/api/*` 需 Bearer（页面
  会弹窗要一次）。**daemon 务必绑 127.0.0.1**，否则策略可被绕过。

## 客户端 API 一览

| 方法 | 对应 daemon 端点 |
|---|---|
| `health(deep=False)` | `GET /health` |
| `capabilities()` | `GET /capabilities`（含 features/limits/modelServices） |
| `create_session(model=, approval_mode=, cwd=, scope="thread")` | `POST /session` |
| `prompt(sid, text, attachments=)` → `PromptAck` | `POST /session/:id/prompt`（202 异步） |
| `run_turn(sid, text, on_text=, on_event=, on_permission=, turn_timeout=)` → `TurnResult`（含 `usage`） | prompt + SSE 收完一轮 |
| `ask(text, turn_timeout=, max_retries=)` → `str` | 一次性会话封装（瞬时故障自动重试） |
| `ask_json(text, validator=, max_attempts=)` → 解析后的 JSON 值 | 同上 + 只许输出 JSON，失败自动让模型纠正 |
| `session_pool(size=, approval_mode=)` → `SessionPool` | 复用会话池，供并发任务 |
| `upload_attachment(sid, file)` → 附件引用 | `POST /session/:id/attachments`（≤8 MiB） |
| `iter_events(sid, last_event_id=, epoch=, deadline=)` | `GET /session/:id/events`（SSE，自动重连续传） |
| `set_approval_mode(sid, mode)` | `POST /session/:id/approval-mode` |
| `respond_permission(sid, request_id, option_id)` / `cancel_permission(...)` | `POST /session/:id/permission/:requestId` |
| `resume_session(sid)` | `POST /session/:id/resume`（daemon 重启后恢复） |
| `cancel(sid)` / `close_session(sid)` | `POST /session/:id/cancel` / `DELETE` |

事件模型：`Event(type, data, id)`；`event.text_chunk()` 取增量文本；
`event.usage()` 取 token 上下文占用（`usage_update` 帧）；
`event.permission_request()` 取结构化权限请求。
一轮以 `turn_complete`（成功，`stopReason`）或 `turn_error`（失败）结束。

## 嵌入生产环境（v0.2）

在程序里无人值守调用时，建议同时启用以下加固（完整示例见
`examples/task_pipeline.py`）：

```python
from qwen_serve_client import QwenServeClient, TurnTimeoutError

with QwenServeClient() as client:
    # 会话池：并发任务复用 session，避免每任务建/关会话的开销
    with client.session_pool(size=4, approval_mode="auto") as pool:
        result = pool.run("总结这篇文档", attachments=["报告.pdf"],
                          turn_timeout=120)   # 超时自动 cancel，不会挂死
        print(result.text, result.usage)      # usage: {"used": N, "size": M}

    # 结构化输出：解析失败自动让模型自我纠正，最多 3 轮
    data = client.ask_json(
        "提取这段话的收件人和截止时间",
        validator=lambda v: isinstance(v, dict) and "收件人" in v,
        turn_timeout=60,
    )
```

- **超时**：`run_turn`/`ask`/`ask_json` 均支持 `turn_timeout`，到期自动
  `cancel` 并抛 `TurnTimeoutError`；SSE 有读超时（默认 90s，daemon 15s
  心跳兜底），死连接自动重连而不是永久阻塞。客户端创建的 session 在
  进程正常退出时会尽力自动关闭（同步版）。
- **重试**：health/capabilities/会话生命周期请求遇瞬时故障（连接错误、
  429/5xx、`session_limit_exceeded` 等）自动按 Retry-After 重试；
  `ask` 还会整轮重试（每次新会话，无残留上下文）。
- **工具审批**：`approval_mode="default"` 下用 `on_permission` 回调做
  程序化审批——返回 `optionId` 即投票，返回 `None` 留给其它审批方
  （如 Web UI 上的人）；回调抛异常则自动取消该请求。
- **附件**：`attachments=[路径或引用]` 随 prompt 一起发送（需 daemon
  支持 `session_attachments`，0.21.x 没有；先 `has_feature()` 预检）。

## 其它语言接入

REST + SSE 是语言无关的。curl 示例：

```bash
TOKEN=你的密钥
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:4170/health
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"sessionScope":"thread","approvalMode":"auto"}' \
  http://127.0.0.1:4170/session
# → {"sessionId":"..."}
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"prompt":[{"type":"text","text":"你好"}]}' \
  http://127.0.0.1:4170/session/<sessionId>/prompt
# → 202 {"promptId":"...","lastEventId":N}
curl -N -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
  -H "Last-Event-ID: N" \
  http://127.0.0.1:4170/session/<sessionId>/events
```

正式客户端可用 qwen-code 仓库的 `docs/developers/daemon-rest-api.openapi.json`
经 openapi-generator 生成。

## 测试

```bash
# 离线单元测试（不需要 daemon）
python -m pytest tests/test_client.py

# 全链路集成测试：自动起 fake LLM（scripts/fake-llm-server.mjs）+ 独立
# qwen serve（QWEN_HOME 隔离，不碰你的真实配置），不需要真实模型 key
python -m pytest tests/test_integration.py

# 真实 daemon 冒烟（先启动 daemon）
set QWEN_SERVER_TOKEN=你的密钥
python scripts/smoke.py
```

## 注意事项

- `approval_mode="auto"` 意味着工具（写文件、跑 shell）无人审批执行——只用于
  可信调用方；否则用 `auto-edit` 并在 daemon 侧用 `--workspace` 限定目录。
- 同一 session 的多个 prompt 按 FIFO 排队；并发任务请各自开 session。
- serve API 仍在演进，客户端启动时调 `capabilities()` 做特性协商；
  升级 qwen 版本后跑一次 `scripts/smoke.py` 回归。


