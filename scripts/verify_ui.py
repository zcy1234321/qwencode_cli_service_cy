"""UI 服务全链路验证：fake LLM → qwen serve → qwen-serve-ui → 浏览器侧 API。

跑法：python scripts/verify_ui.py
覆盖：静态页、meta/health/capabilities、会话+prompt+SSE 透传、
     MCP 增删查、skills 列表、tools 启停、approval-mode、permission 代理存在性。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_MJS = os.path.join(REPO_ROOT, "scripts", "fake-llm-server.mjs")
TOKEN = "ui-verify-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, token: str | None, timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            req = urllib.request.Request(f"{url}/health", headers=headers)
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"health timeout: {url}")


class Api:
    def __init__(self, base: str) -> None:
        self.base = base

    def req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw) if raw else {}
                except ValueError:
                    return r.status, {"_raw": raw.decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:
                return e.code, {"_raw": raw.decode("utf-8", "replace")}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    procs: list[subprocess.Popen] = []
    # ignore_cleanup_errors：daemon 以这些目录为 cwd，Windows 上句柄释放有延迟
    tmp = tempfile.TemporaryDirectory(prefix="qwen-home-ui-",
                                      ignore_cleanup_errors=True)
    tmp2 = tempfile.TemporaryDirectory(prefix="qwen-home-d2-",
                                       ignore_cleanup_errors=True)
    tmp_ws = tempfile.TemporaryDirectory(prefix="qwen-workspace-",
                                         ignore_cleanup_errors=True)
    try:
        # 1) fake LLM
        fake = subprocess.Popen(
            ["node", FAKE_MJS], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        procs.append(fake)
        assert fake.stdout
        fake_url = None
        for line in fake.stdout:
            if line.startswith("READY "):
                fake_url = line.split()[1]
                break
        assert fake_url, "fake LLM 未就绪"
        print(f"[ok] fake LLM: {fake_url}")

        # 2) qwen serve（cwd 用临时目录：工具开关会写 <cwd>/.qwen/settings.json，
        #    不能污染仓库）
        dport = free_port()
        qwen = shutil.which("qwen.cmd") or shutil.which("qwen") or "qwen"
        daemon = subprocess.Popen(
            [qwen, "serve", "--port", str(dport), "--token", TOKEN,
             "--require-auth", "--no-web"],
            env={**os.environ, "QWEN_HOME": tmp.name,
                 "OPENAI_API_KEY": "fake-key", "OPENAI_BASE_URL": fake_url,
                 "OPENAI_MODEL": "fake-model"},
            cwd=tmp_ws.name,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(daemon)
        daemon_url = f"http://127.0.0.1:{dport}"
        wait_health(daemon_url, TOKEN)
        print(f"[ok] daemon: {daemon_url}")

        # 3) UI server
        uport = free_port()
        ui = subprocess.Popen(
            [sys.executable, "-m", "qwen_serve_ui",
             "--host", "127.0.0.1", "--port", str(uport),
             "--daemon", daemon_url],
            env={**os.environ, "QWEN_SERVER_TOKEN": TOKEN,
                 "QWEN_HOME": tmp.name},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(ui)
        ui_url = f"http://127.0.0.1:{uport}"
        api = Api(ui_url)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                st, meta = api.req("GET", "/api/meta")
                if st == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("UI server 未就绪")
        print(f"[ok] ui: {ui_url}")

        # 静态页
        with urllib.request.urlopen(f"{ui_url}/", timeout=5) as r:
            html = r.read().decode()
        assert "聊天" in html and "MCP" in html, "index.html 内容异常"
        print("[ok] 静态页可访问")

        # meta / health / capabilities
        st, meta = api.req("GET", "/api/meta")
        assert st == 200 and meta.get("daemonUrl") == daemon_url, meta
        st, health = api.req("GET", "/api/health")
        assert st == 200 and health.get("status") == "ok", health
        st, caps = api.req("GET", "/api/capabilities")
        assert st == 200 and "features" in caps, caps
        print("[ok] meta/health/capabilities")

        # 会话 + prompt + SSE 透传
        st, sess = api.req("POST", "/api/sessions",
                           {"sessionScope": "thread", "approvalMode": "auto"})
        assert st == 200 and sess.get("sessionId"), sess
        sid = sess["sessionId"]
        st, ack = api.req("POST", f"/api/sessions/{sid}/prompt",
                          {"prompt": [{"type": "text", "text": "hello"}]})
        assert st == 202 and ack.get("promptId") is not None, ack
        print(f"[ok] 会话+prompt: sid={sid[:8]}... promptId={ack['promptId'][:8]}...")

        req = urllib.request.Request(
            f"{ui_url}/api/sessions/{sid}/events"
            f"?last_event_id={ack.get('lastEventId', 0)}",
            headers={"Accept": "text/event-stream"},
        )
        text_parts: list[str] = []
        done = None
        with urllib.request.urlopen(req, timeout=60) as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    env = json.loads(line[5:])
                except ValueError:
                    continue
                t, d = env.get("type"), env.get("data") or {}
                if t == "session_update":
                    upd = d.get("update") or {}
                    if upd.get("sessionUpdate") == "agent_message_chunk":
                        text_parts.append((upd.get("content") or {}).get("text", ""))
                elif t in ("turn_complete", "turn_error"):
                    done = (t, d)
                    break
        assert done and done[0] == "turn_complete", f"turn 未正常结束: {done}"
        assert "fake-answer" in "".join(text_parts), f"SSE 无文本: {text_parts}"
        print(f"[ok] SSE 透传+流式文本: {''.join(text_parts)[:60]}")

        # ---- 会话记忆：刷新后恢复聊天记录 ----
        # 退化方案：本地缓存应含合成的 user_message_chunk + SSE 旁路记录的应答
        st, hist = api.req("GET", f"/api/sessions/{sid}/history")
        assert st == 200 and isinstance(hist.get("events"), list), (st, hist)
        hist_text = json.dumps(hist["events"], ensure_ascii=False)
        assert "hello" in hist_text and "fake-answer" in hist_text, hist_text[:400]
        # 用户消息只记一次（prompt 合成；SSE 回显被旁路过滤，不重复落盘）
        user_chunks = [
            e for e in hist["events"]
            if (((e.get("data") or {}).get("update") or {})
                .get("sessionUpdate") == "user_message_chunk")
        ]
        assert len(user_chunks) == 1, f"user 消息重复落盘: {len(user_chunks)}"
        cache_file = os.path.join(tmp.name, "ui-chat-cache", f"{sid}.jsonl")
        assert os.path.exists(cache_file), "ui-chat-cache 未落盘"
        print(f"[ok] 聊天缓存落盘+回放（{len(hist['events'])} 条事件）")
        # 主路径：daemon transcript（0.21.13 有 session_transcript 能力）
        st, tr = api.req("GET", f"/api/sessions/{sid}/transcript?limit=500")
        if st == 404:
            print("[skip] daemon 无 transcript 路由，走本地缓存退化")
        else:
            assert st == 200 and isinstance(tr.get("events"), list), (st, tr)
            tr_text = json.dumps(tr["events"], ensure_ascii=False)
            assert "fake-answer" in tr_text, tr_text[:400]
            print(f"[ok] daemon transcript（{len(tr['events'])} 条事件"
                  f"{'，hasMore=' + str(tr.get('hasMore')) if 'hasMore' in tr else ''}）")

        # approval-mode / cancel 代理
        st, am = api.req("POST", f"/api/sessions/{sid}/approval-mode",
                         {"mode": "auto-edit", "persist": False})
        assert st == 200 and am.get("mode") == "auto-edit", am
        st, _ = api.req("POST", f"/api/sessions/{sid}/cancel", {})
        assert st in (200, 204), st
        # permission 代理路由存在性（无 pending request，应 404 而非 405/502）
        st, _ = api.req("POST", f"/api/sessions/{sid}/permission/nope",
                        {"outcome": {"outcome": "selected", "optionId": "x"}})
        assert st in (404, 400), f"permission 代理异常: {st}"
        print("[ok] approval-mode / cancel / permission 代理")

        # MCP：增 → 查（含落盘验证）→ 启停 → 删
        st, put_resp = api.req("PUT", "/api/mcp/servers/ui-test",
                               {"scope": "user",
                                "config": {"httpUrl": "http://127.0.0.1:9/mcp"}})
        assert st == 200, (st, put_resp)
        # 回退路径应写入 settings.json
        settings_file = os.path.join(tmp.name, "settings.json")
        disk = json.load(open(settings_file, encoding="utf-8"))
        assert disk.get("mcpServers", {}).get("ui-test", {}).get("httpUrl"), disk
        st, servers = api.req("GET", "/api/mcp/servers")
        assert st == 200 and "ui-test" in json.dumps(servers), servers
        st, dis = api.req("POST", "/api/mcp/ui-test/disable", {})
        assert st == 200, (st, dis)
        disk = json.load(open(settings_file, encoding="utf-8"))
        assert "ui-test" in json.dumps(disk.get("mcp", {})), disk
        st, _ = api.req("POST", "/api/mcp/ui-test/enable", {})
        assert st == 200, st
        st, _ = api.req("DELETE", "/api/mcp/servers/ui-test?scope=user")
        assert st == 200, st
        disk = json.load(open(settings_file, encoding="utf-8"))
        assert "ui-test" not in json.dumps(disk.get("mcpServers", {})), disk
        print("[ok] MCP 增/查/启停/删（settings.json 落盘已验证）")

        # Skills 列表 + tools 启停 + reload
        st, skills = api.req("GET", "/api/skills")
        assert st == 200, st
        st, _ = api.req("POST", "/api/tools/web_fetch/enable",
                        {"enabled": False})
        assert st in (200, 400, 404), f"tools enable 代理异常: {st}"
        st, _ = api.req("POST", "/api/workspace/reload", {})
        assert st == 200, st
        print("[ok] skills / tools / reload")

        # ---- 会话级工具覆盖 ----
        # 上面的用例已把 web_fetch 全局禁用（落 <tmp_ws>/.qwen/settings.json）
        st, ts = api.req("GET", "/api/tools/state")
        assert st == 200 and "web_fetch" in ts.get("disabled", []), (st, ts)
        st, sess_t = api.req("POST", "/api/sessions", {
            "sessionScope": "thread", "approvalMode": "auto",
            "toolEnable": ["web_fetch"], "toolDisable": ["write_file"],
        })
        assert st == 200 and sess_t.get("sessionId"), (st, sess_t)
        assert sess_t.get("toolOverrides", {}).get("disabled") == ["write_file"], \
            sess_t
        # 建完后全局状态必须已恢复：web_fetch 仍禁用、write_file 未被禁用
        st, ts2 = api.req("GET", "/api/tools/state")
        assert st == 200, (st, ts2)
        assert "web_fetch" in ts2["disabled"] and \
            "write_file" not in ts2["disabled"], ts2
        print("[ok] 会话级工具覆盖（临时禁用/启用 + 建后全局恢复）")

        # ---- 应用接入网关 ----
        # 建 app（带工具白名单 → 会被强制 default 审批模式）
        st, app1 = api.req("POST", "/api/apps", {
            "name": "bot1", "allowedTools": ["read"], "note": "测试",
        })
        assert st == 201 and app1.get("token"), (st, app1)
        token1 = app1["token"]
        print(f"[ok] app 创建: bot1 token={token1[:6]}...")

        # 程序面：用 app token 建会话（base_url 语义 = /app/v1）
        app_headers = {"Authorization": f"Bearer {token1}",
                       "Content-Type": "application/json"}

        def app_req(method, path, body=None, token=token1):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                f"{ui_url}/app/v1{path}", data=data, method=method,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return r.status, json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    return e.code, json.loads(raw or b"{}")
                except ValueError:
                    return e.code, {"_raw": raw.decode("utf-8", "replace")}

        st, sess1 = app_req("POST", "/session", {"approvalMode": "yolo"})
        assert st == 200 and sess1.get("sessionId"), (st, sess1)
        asid = sess1["sessionId"]
        print(f"[ok] app 会话创建（profile 注入）: {asid[:8]}...")

        # app 会话 prompt + SSE 透传
        st, ack = app_req("POST", f"/session/{asid}/prompt",
                          {"prompt": [{"type": "text", "text": "hello"}]})
        assert st == 202, (st, ack)
        req = urllib.request.Request(
            f"{ui_url}/app/v1/session/{asid}/events"
            f"?last_event_id={ack.get('lastEventId', 0)}",
            headers={**app_headers, "Accept": "text/event-stream"},
        )
        done = None
        with urllib.request.urlopen(req, timeout=60) as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    env = json.loads(line[5:])
                except ValueError:
                    continue
                if env.get("type") in ("turn_complete", "turn_error"):
                    done = env.get("type")
                    break
        assert done == "turn_complete", f"app SSE 透传异常: {done}"
        print("[ok] app prompt + SSE 透传")

        # 鉴权与归属隔离
        st, _ = app_req("POST", f"/session/{asid}/prompt",
                        {"prompt": [{"type": "text", "text": "x"}]},
                        token="wrong-token")
        assert st == 401, st
        st, app2 = api.req("POST", "/api/apps", {"name": "bot2"})
        assert st == 201, (st, app2)
        st, _ = app_req("POST", f"/session/{asid}/prompt",
                        {"prompt": [{"type": "text", "text": "x"}]},
                        token=app2["token"])
        assert st == 403, f"跨 app 访问未被拒绝: {st}"
        print("[ok] app token 鉴权 + 会话归属隔离")

        # 开关：禁用后存量会话被关闭、新会话被拒绝
        st, dis = api.req("POST", "/api/apps/bot1/enable?enabled=false", {})
        assert st == 200 and dis.get("enabled") is False, (st, dis)
        st, _ = app_req("POST", "/session", {})
        assert st == 403, f"禁用后仍可建会话: {st}"
        st, _ = api.req("DELETE", "/api/apps/bot1")
        st2, _ = api.req("DELETE", "/api/apps/bot2")
        assert st == 200 and st2 == 200, (st, st2)
        st, apps = api.req("GET", "/api/apps")
        assert st == 200 and apps.get("apps") == [], apps
        print("[ok] app 开关 / 删除")

        # ---- 多 daemon 路由 ----
        TOKEN2 = "ui-verify-token-2"
        dport2 = free_port()
        daemon2 = subprocess.Popen(
            [qwen, "serve", "--port", str(dport2), "--token", TOKEN2,
             "--require-auth", "--no-web"],
            env={**os.environ, "QWEN_HOME": tmp2.name,
                 "OPENAI_API_KEY": "fake-key", "OPENAI_BASE_URL": fake_url,
                 "OPENAI_MODEL": "fake-model"},
            cwd=tmp2.name,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(daemon2)
        daemon2_url = f"http://127.0.0.1:{dport2}"
        wait_health(daemon2_url, TOKEN2)
        print(f"[ok] daemon2: {daemon2_url}")

        st, d2 = api.req("POST", "/api/daemons", {
            "name": "d2", "url": daemon2_url, "token": TOKEN2, "note": "二号机",
        })
        assert st == 201 and d2.get("token") == TOKEN2, (st, d2)
        st, dl = api.req("GET", "/api/daemons")
        names = [d["name"] for d in dl.get("daemons", [])]
        assert st == 200 and "d2" in names and "" in names, dl
        # 列表视图 token 脱敏
        assert all(d.get("token") != TOKEN2 for d in dl["daemons"]), dl
        st, probe = api.req("POST", "/api/daemons/d2/probe", {})
        assert st == 200, (st, probe)
        deadline = time.time() + 30
        while not probe.get("ok") and time.time() < deadline:
            time.sleep(1)
            st, probe = api.req("POST", "/api/daemons/d2/probe", {})
        assert probe.get("ok") is True, probe
        print("[ok] daemon 注册 / 列表脱敏 / 探测")

        # 建绑定 d2 的 app，会话应落在 daemon2
        st, app3 = api.req("POST", "/api/apps", {"name": "bot3", "daemon": "d2"})
        assert st == 201 and app3.get("daemon") == "d2", (st, app3)
        token3 = app3["token"]
        st, sess3 = app_req("POST", "/session", {}, token=token3)
        assert st == 200 and sess3.get("sessionId"), (st, sess3)
        sid3 = sess3["sessionId"]
        # 反向证明：daemon1 上没有这个会话（直接打 daemon1 删除应 404）
        req = urllib.request.Request(
            f"{daemon_url}/session/{sid3}", method="DELETE",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("会话不应出现在 daemon1")
        except urllib.error.HTTPError as e:
            assert e.code == 404, f"daemon1 上会话状态异常: {e.code}"
        # 正向证明：经网关 prompt + SSE 走到 daemon2 且正常完成
        st, ack = app_req("POST", f"/session/{sid3}/prompt",
                          {"prompt": [{"type": "text", "text": "hello"}]},
                          token=token3)
        assert st == 202, (st, ack)
        req = urllib.request.Request(
            f"{ui_url}/app/v1/session/{sid3}/events"
            f"?last_event_id={ack.get('lastEventId', 0)}",
            headers={"Authorization": f"Bearer {token3}",
                     "Accept": "text/event-stream"},
        )
        done = None
        with urllib.request.urlopen(req, timeout=60) as stream:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    env = json.loads(line[5:])
                except ValueError:
                    continue
                if env.get("type") in ("turn_complete", "turn_error"):
                    done = env.get("type")
                    break
        assert done == "turn_complete", f"daemon2 路由 SSE 异常: {done}"
        print("[ok] 多 daemon 路由（会话落在 d2，daemon1 上 404，SSE 通）")

        # daemon 被引用时不可删
        st, _ = api.req("DELETE", "/api/daemons/d2")
        assert st == 409, f"被引用的 daemon 应 409: {st}"
        # 绑定未知 daemon 的 app 应 400
        st, _ = api.req("POST", "/api/apps", {"name": "botX", "daemon": "nope"})
        assert st == 400, f"未知 daemon 应 400: {st}"
        print("[ok] daemon 引用保护 / 未知 daemon 拒绝")

        # ---- 配额 ----
        st, app4 = api.req("POST", "/api/apps", {
            "name": "bot4", "maxSessions": 1, "rateLimitRpm": 1,
        })
        assert st == 201, (st, app4)
        token4 = app4["token"]
        st, sess4 = app_req("POST", "/session", {}, token=token4)
        assert st == 200 and sess4.get("sessionId"), (st, sess4)
        sid4 = sess4["sessionId"]
        # 并发会话上限：第二个会话 429
        st, quota_err = app_req("POST", "/session", {}, token=token4)
        assert st == 429 and quota_err.get("code") == "app_session_quota", \
            (st, quota_err)
        # 速率上限：第一次 prompt 放行，第二次 429
        st, _ = app_req("POST", f"/session/{sid4}/prompt",
                        {"prompt": [{"type": "text", "text": "one"}]}, token=token4)
        assert st == 202, st
        st, rate_err = app_req("POST", f"/session/{sid4}/prompt",
                               {"prompt": [{"type": "text", "text": "two"}]},
                               token=token4)
        assert st == 429 and rate_err.get("code") == "app_rate_limited", \
            (st, rate_err)
        # 关掉会话后配额释放，可再建
        st, _ = app_req("DELETE", f"/session/{sid4}", token=token4)
        assert st in (200, 204), st
        st, sess4b = app_req("POST", "/session", {}, token=token4)
        assert st == 200 and sess4b.get("sessionId"), (st, sess4b)
        print("[ok] 配额（并发会话 429 / 速率 429 / 释放后可再建）")

        # 工作区浏览/注册列表（下拉选择器的数据源）
        st, sug = api.req("GET", "/api/fs/suggest?path=")
        assert st == 200 and sug.get("suggestions"), (st, sug)
        st, sug2 = api.req(
            "GET",
            "/api/fs/suggest?path=" + urllib.parse.quote(tmp_ws.name),
        )
        assert st == 200 and "suggestions" in sug2, (st, sug2)
        st, wsr = api.req("GET", "/api/workspaces")
        assert st == 200 and "primaryWorkspace" in wsr, (st, wsr)
        # bot5 的 cwd 自动注册后应出现在注册表（老版本无注册表则回退为空）
        if wsr.get("entries"):
            assert any(
                e.get("cwd", "").lower() == tmp_ws.name.lower()
                for e in wsr["entries"]
            ), wsr
        print("[ok] 工作区浏览 / 注册列表")

        # 工作区：带 cwd 的 app，profile 往返 + 会话创建成功（cwd 注入）
        st, app5 = api.req("POST", "/api/apps", {
            "name": "bot5", "cwd": tmp_ws.name,
        })
        assert st == 201 and app5.get("cwd") == tmp_ws.name, (st, app5)
        st, sess5 = app_req("POST", "/session", {}, token=app5["token"])
        assert st == 200 and sess5.get("sessionId"), (st, sess5)
        st, _ = app_req("DELETE", f"/session/{sess5['sessionId']}",
                        token=app5["token"])
        assert st in (200, 204), st
        st, _ = api.req("DELETE", "/api/apps/bot5")
        assert st == 200, st
        print("[ok] app 工作区（cwd 注入 + 会话创建）")

        # ---- 模型列表回退（会破坏默认模型选择，故放在所有 prompt 之后）----
        # /api/models/fetch：fake LLM 无 /models，有 /v1/models → 走候选回退
        st, fetched = api.req("POST", "/api/models/fetch",
                              {"baseUrl": fake_url, "apiKey": "fake-key"})
        assert st == 200 and "fake-model" in fetched.get("models", []), \
            (st, fetched)
        assert fetched.get("via", "").endswith("/v1/models"), fetched
        st, bad = api.req("POST", "/api/models/fetch",
                          {"baseUrl": "http://127.0.0.1:9", "apiKey": "x"})
        assert st == 502 and bad.get("code") == "fetch_failed", (st, bad)
        print("[ok] models/fetch（OpenAI /models 代拉 + 失败兜底）")

        # 安装 provider 后，已配置模型列表（0.21.13 无 GET /workspace/models，
        # 走 settings.json 回退）应列出装上的模型
        st, inst = api.req("POST", "/api/providers", {
            "providerId": "custom-openai-compatible",
            "apiKey": "fake-key",
            # daemon 拒绝私网 baseUrl；安装只是写配置，用公网域名即可
            "baseUrl": "https://api.example.com/v1",
            "modelIds": ["fake-model"],
        })
        assert st in (200, 201), (st, inst)
        st, model_list = api.req("GET", "/api/models")
        assert st == 200, (st, model_list)
        assert "fake-model" in json.dumps(model_list), model_list
        print(f"[ok] provider 安装 + 模型列表（via={model_list.get('via', 'api')}）")

        # ---- 审计日志 ----
        st, audit_data = api.req("GET", "/api/audit?limit=100")
        assert st == 200 and audit_data.get("records"), audit_data
        events = {r.get("event") for r in audit_data["records"]}
        expected = {"admin_app_created", "app_session_created", "app_prompt",
                    "app_session_quota", "app_rate_limited", "app_session_closed",
                    "admin_daemon_created"}
        missing = expected - events
        assert not missing, f"审计缺事件: {missing}"
        # 落盘文件（UI 服务的 QWEN_HOME = tmp.name）
        audit_file = os.path.join(tmp.name, "ui-audit.jsonl")
        assert os.path.exists(audit_file), "ui-audit.jsonl 未落盘"
        disk_events = set()
        with open(audit_file, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("{"):
                    disk_events.add(json.loads(ln).get("event"))
        assert "app_prompt" in disk_events and "app_session_created" in disk_events, \
            disk_events
        print(f"[ok] 审计日志（{len(events)} 类事件，已落盘 ui-audit.jsonl）")

        # 清理 gateway 侧的 app / daemon
        st, _ = api.req("DELETE", "/api/apps/bot3")
        st2, _ = api.req("DELETE", "/api/apps/bot4")
        assert st == 200 and st2 == 200, (st, st2)
        st, _ = api.req("DELETE", "/api/daemons/d2")
        assert st == 200, st

        # 关闭会话
        st, _ = api.req("DELETE", f"/api/sessions/{sid}")
        assert st in (200, 204), st
        print("[ok] 会话关闭")

        print("\nUI VERIFY PASS")
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        tmp.cleanup()
        tmp2.cleanup()
        tmp_ws.cleanup()


if __name__ == "__main__":
    sys.exit(main())
