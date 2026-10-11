"""应用接入网关的离线单元测试：policy.py 纯逻辑（无需 daemon/FastAPI）。"""

import os
import tempfile
import unittest
from pathlib import Path

from qwen_serve_ui.audit import AuditLog
from qwen_serve_ui.chatcache import ChatCache
from qwen_serve_ui.policy import (
    AppProfile,
    AppsStore,
    DaemonsStore,
    RateLimiter,
    build_vote,
    decide_vote,
    evaluate_permission,
    paths_allowed,
    tool_allowed,
)


def perm_event(title, *, options=None, raw_input=None, kind=None):
    tool_call = {"title": title}
    if raw_input is not None:
        tool_call["rawInput"] = raw_input
    if kind is not None:
        tool_call["kind"] = kind
    return {
        "requestId": "r1",
        "toolCall": tool_call,
        "options": options
        or [
            {"optionId": "proceed_once", "kind": "allow_once"},
            {"optionId": "cancel", "kind": "reject_once"},
        ],
    }


class TestToolAllowed(unittest.TestCase):
    def test_no_rules_allows_all(self):
        p = AppProfile("a", "t")
        self.assertTrue(tool_allowed(p, {"title": "Write"}))

    def test_star_allows_all(self):
        p = AppProfile("a", "t", allowed_tools=["*"])
        self.assertTrue(tool_allowed(p, {"title": "Write"}))

    def test_builtin_name_match(self):
        p = AppProfile("a", "t", allowed_tools=["read"])
        self.assertTrue(tool_allowed(p, {"title": "Read"}))
        self.assertFalse(tool_allowed(p, {"title": "Write"}))

    def test_mcp_server_rule(self):
        p = AppProfile("a", "t", allowed_tools=["mcp:github"])
        self.assertTrue(
            tool_allowed(p, {"title": "create_issue (github MCP Server)"})
        )
        self.assertTrue(tool_allowed(p, {"name": "mcp__github__create_issue"}))
        self.assertFalse(
            tool_allowed(p, {"title": "query (database MCP Server)"})
        )


class TestPathsAllowed(unittest.TestCase):
    def test_no_roots_allows_all(self):
        p = AppProfile("a", "t")
        ev = perm_event("Write", raw_input={"file_path": "/etc/passwd"})
        self.assertTrue(paths_allowed(p, ev["toolCall"]))

    def test_path_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = AppProfile("a", "t", allowed_paths=[tmp])
            inside = os.path.join(tmp, "a.txt")
            ev = perm_event("Write", raw_input={"file_path": inside})
            self.assertTrue(paths_allowed(p, ev["toolCall"]))
            ev = perm_event("Write", raw_input={"file_path": "/etc/passwd"})
            self.assertFalse(paths_allowed(p, ev["toolCall"]))

    def test_no_path_args_allowed(self):
        p = AppProfile("a", "t", allowed_paths=["/some/root"])
        ev = perm_event("WebFetch", raw_input={"url": "https://example.com"})
        self.assertTrue(paths_allowed(p, ev["toolCall"]))

    def test_shell_needs_explicit_allow(self):
        p = AppProfile("a", "t", allowed_paths=["/some/root"],
                       allowed_tools=["read"])
        ev = perm_event("Shell", kind="execute",
                        raw_input={"command": "cat /etc/passwd"})
        self.assertFalse(paths_allowed(p, ev["toolCall"]))
        p2 = AppProfile("a", "t", allowed_paths=["/some/root"],
                        allowed_tools=["read", "shell"])
        self.assertTrue(paths_allowed(p2, ev["toolCall"]))


class TestDecideVote(unittest.TestCase):
    def test_unrestricted_profile_abstains(self):
        p = AppProfile("a", "t")
        self.assertIsNone(decide_vote(p, perm_event("Write")))

    def test_allowed_picks_allow_once(self):
        p = AppProfile("a", "t", allowed_tools=["write"])
        vote = decide_vote(p, perm_event("Write"))
        self.assertEqual(
            vote, {"outcome": {"outcome": "selected", "optionId": "proceed_once"}}
        )

    def test_denied_picks_reject(self):
        p = AppProfile("a", "t", allowed_tools=["read"])
        vote = decide_vote(p, perm_event("Write"))
        self.assertEqual(
            vote, {"outcome": {"outcome": "selected", "optionId": "cancel"}}
        )

    def test_denied_without_reject_option_cancels(self):
        p = AppProfile("a", "t", allowed_tools=["read"])
        ev = perm_event("Write", options=[{"optionId": "ok", "kind": "allow_once"}])
        vote = decide_vote(p, ev)
        self.assertEqual(vote, {"outcome": {"outcome": "cancelled"}})

    def test_restricted_profile_forces_default_mode(self):
        p = AppProfile("a", "t", approval_mode="auto", allowed_tools=["read"])
        self.assertEqual(p.effective_approval_mode(), "default")
        p2 = AppProfile("a", "t", approval_mode="auto")
        self.assertEqual(p2.effective_approval_mode(), "auto")


class TestAppsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AppsStore(Path(self.tmp.name) / "ui-apps.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_token_lookup(self):
        p = self.store.create(
            "bot", model="m1", allowed_tools=["read"], note="测试"
        )
        self.assertTrue(p.token)
        loaded = self.store.load()
        self.assertIn("bot", loaded)
        self.assertEqual(loaded["bot"].model, "m1")
        self.assertEqual(loaded["bot"].allowed_tools, ["read"])
        # token 持久化且可查找
        self.assertEqual(loaded["bot"].token, p.token)
        # 列表视图脱敏
        self.assertNotEqual(self.store.load()["bot"].to_dict()["token"], p.token)
        self.assertEqual(
            self.store.load()["bot"].to_dict(reveal_token=True)["token"], p.token
        )

    def test_duplicate_name_rejected(self):
        self.store.create("bot")
        with self.assertRaises(ValueError):
            self.store.create("bot")

    def test_update_and_enable(self):
        self.store.create("bot")
        p = self.store.update("bot", allowedTools=["write"], note="n")
        self.assertEqual(p.allowed_tools, ["write"])
        p = self.store.set_enabled("bot", False)
        self.assertFalse(p.enabled)
        self.assertFalse(self.store.load()["bot"].enabled)

    def test_regenerate_token(self):
        p1 = self.store.create("bot")
        p2 = self.store.regenerate_token("bot")
        self.assertNotEqual(p1.token, p2.token)
        self.assertEqual(self.store.load()["bot"].token, p2.token)

    def test_session_tracking(self):
        self.store.create("bot")
        self.store.add_session("bot", "s1")
        self.store.add_session("bot", "s1")  # 幂等
        self.store.add_session("bot", "s2", "gpu-2")
        loaded = self.store.load()["bot"]
        self.assertEqual(loaded.session_ids(), ["s1", "s2"])
        self.assertEqual(loaded.sessions[1].daemon, "gpu-2")
        self.store.remove_session("bot", "s1")
        self.assertEqual(self.store.load()["bot"].session_ids(), ["s2"])

    def test_quota_fields_roundtrip(self):
        p = self.store.create(
            "bot", daemon="gpu-2", max_sessions=3, rate_limit_rpm=10,
            cwd="D:/work/bot",
        )
        loaded = self.store.load()["bot"]
        self.assertEqual(loaded.daemon, "gpu-2")
        self.assertEqual(loaded.cwd, "D:/work/bot")
        self.assertEqual(loaded.max_sessions, 3)
        self.assertEqual(loaded.rate_limit_rpm, 10)
        d = loaded.to_dict()
        self.assertEqual(d["maxSessions"], 3)
        self.assertEqual(d["rateLimitRpm"], 10)
        self.assertEqual(d["daemon"], "gpu-2")
        self.assertEqual(d["cwd"], "D:/work/bot")
        p2 = self.store.update("bot", maxSessions=1, rateLimitRpm=0, daemon="",
                               cwd="")
        self.assertEqual(p2.max_sessions, 1)
        self.assertEqual(p2.rate_limit_rpm, 0)
        self.assertEqual(p2.daemon, "")
        self.assertIsNone(p2.cwd)  # 空串归一为 None

    def test_legacy_string_sessions_compat(self):
        """旧版 ui-apps.json 里 sessions 是裸字符串，加载后应转成 SessionRef。"""
        self.store.create("bot")
        import json

        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        data["apps"][0]["sessions"] = ["s9"]
        self.store.path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        loaded = self.store.load()["bot"]
        self.assertEqual(loaded.session_ids(), ["s9"])
        self.assertEqual(loaded.sessions[0].daemon, "")

    def test_delete(self):
        self.store.create("bot")
        self.store.delete("bot")
        self.assertEqual(self.store.load(), {})


class TestEvaluatePermission(unittest.TestCase):
    def test_reason_on_tool_denied(self):
        p = AppProfile("a", "t", allowed_tools=["read"])
        allowed, reason = evaluate_permission(p, perm_event("Write")["toolCall"])
        self.assertFalse(allowed)
        self.assertIn("allowed_tools", reason)

    def test_reason_on_path_denied(self):
        p = AppProfile("a", "t", allowed_paths=["/some/root"])
        ev = perm_event("Write", raw_input={"file_path": "/etc/passwd"})
        allowed, reason = evaluate_permission(p, ev["toolCall"])
        self.assertFalse(allowed)
        self.assertIn("allowed_paths", reason)

    def test_allowed_reason(self):
        p = AppProfile("a", "t", allowed_tools=["write"])
        allowed, reason = evaluate_permission(p, perm_event("Write")["toolCall"])
        self.assertTrue(allowed)
        self.assertTrue(reason)

    def test_build_vote_no_allow_option_abstains(self):
        vote = build_vote([{"optionId": "no", "kind": "reject_once"}], True)
        self.assertIsNone(vote)


class TestRateLimiter(unittest.TestCase):
    def test_zero_rpm_unlimited(self):
        rl = RateLimiter()
        self.assertIsNone(rl.check("a", 0))
        self.assertIsNone(rl.check("a", 0))

    def test_limit_hit(self):
        rl = RateLimiter()
        self.assertIsNone(rl.check("a", 2, now=100.0))
        self.assertIsNone(rl.check("a", 2, now=101.0))
        retry = rl.check("a", 2, now=102.0)
        self.assertIsNotNone(retry)
        self.assertGreater(retry, 0)
        # 独立 key 互不影响
        self.assertIsNone(rl.check("b", 2, now=102.0))

    def test_window_expires(self):
        rl = RateLimiter()
        self.assertIsNone(rl.check("a", 1, now=100.0))
        self.assertIsNotNone(rl.check("a", 1, now=150.0))
        self.assertIsNone(rl.check("a", 1, now=161.0))


class TestDaemonsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DaemonsStore(Path(self.tmp.name) / "ui-daemons.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_load(self):
        ep = self.store.create("gpu-2", "http://127.0.0.1:4171/",
                               token="tok123456789", note="二号机")
        self.assertEqual(ep.url, "http://127.0.0.1:4171")  # 尾斜杠剥掉
        loaded = self.store.load()["gpu-2"]
        self.assertEqual(loaded.token, "tok123456789")
        # 列表视图脱敏
        self.assertNotEqual(loaded.to_dict()["token"], "tok123456789")
        self.assertEqual(
            loaded.to_dict(reveal_token=True)["token"], "tok123456789"
        )

    def test_duplicate_rejected(self):
        self.store.create("gpu-2", "http://x")
        with self.assertRaises(ValueError):
            self.store.create("gpu-2", "http://y")

    def test_enable_delete(self):
        self.store.create("gpu-2", "http://x")
        self.assertFalse(self.store.set_enabled("gpu-2", False).enabled)
        with self.assertRaises(KeyError):
            self.store.set_enabled("nope", True)
        self.store.delete("gpu-2")
        self.assertEqual(self.store.load(), {})


class TestAuditLog(unittest.TestCase):
    def test_write_read_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(Path(tmp) / "ui-audit.jsonl")
            self.assertEqual(log.read_tail(), [])
            log.write("first", app="a")
            log.write("second", app="b", vote="proceed_once", allowed=True)
            records = log.read_tail(10)
            self.assertEqual(len(records), 2)
            # 新的在前
            self.assertEqual(records[0]["event"], "second")
            self.assertEqual(records[0]["vote"], "proceed_once")
            self.assertEqual(records[1]["event"], "first")
            self.assertIn("ts", records[0])
            # limit 生效
            self.assertEqual(len(log.read_tail(1)), 1)


class TestChatCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = ChatCache(Path(self.tmp.name) / "ui-chat-cache")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_read_roundtrip(self):
        self.assertEqual(self.cache.read("s1"), [])
        self.cache.append("s1", {"type": "session_update", "data": {"n": 1}})
        self.cache.append("s1", {"type": "turn_complete", "data": {}})
        events = self.cache.read("s1")
        self.assertEqual([e["type"] for e in events],
                         ["session_update", "turn_complete"])
        self.assertEqual(events[0]["data"]["n"], 1)

    def test_sessions_isolated_and_sid_sanitized(self):
        self.cache.append("s1", {"type": "a"})
        self.cache.append("s2", {"type": "b"})
        self.assertEqual([e["type"] for e in self.cache.read("s1")], ["a"])
        # 路径字符被剥掉，不会逃逸出缓存目录
        self.cache.append("../evil", {"type": "c"})
        names = [p.name for p in (Path(self.tmp.name) / "ui-chat-cache").iterdir()]
        self.assertTrue(all(n.endswith(".jsonl") for n in names))
        self.assertFalse(any(".." in n or "/" in n or "\\" in n for n in names))

    def test_clear(self):
        self.cache.append("s1", {"type": "a"})
        self.cache.clear("s1")
        self.assertEqual(self.cache.read("s1"), [])
        self.cache.clear("s1")  # 幂等

    def test_rotate_keeps_tail(self):
        cache = ChatCache(Path(self.tmp.name) / "c",
                          max_bytes=2048, keep_tail_bytes=1024)
        for i in range(200):
            cache.append("s1", {"type": "session_update", "data": {"i": i}})
        events = cache.read("s1")
        # 超限后截断：丢了开头但保留了尾部，且全部是可解析记录
        self.assertLess(len(events), 200)
        self.assertGreater(len(events), 0)
        self.assertEqual(events[-1]["data"]["i"], 199)


if __name__ == "__main__":
    unittest.main()
