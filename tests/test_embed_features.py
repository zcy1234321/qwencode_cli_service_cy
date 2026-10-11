"""Offline unit tests for the embedding-hardening features (no daemon needed).

run_turn/ask_json/pool logic is exercised against a QwenServeClient whose
network methods (prompt/iter_events/permission votes/close) are stubbed.
"""

import json
import time
import unittest

from qwen_serve_client import (
    DaemonHttpError,
    JsonOutputError,
    PermissionRequest,
    QwenServeClient,
    SessionInfo,
    TurnTimeoutError,
)
from qwen_serve_client.client import _extract_json
from qwen_serve_client.errors import QwenServeError, TurnError
from qwen_serve_client.events import Event, parse_envelope


class StubClient(QwenServeClient):
    """Client with no network: prompt/iter_events/votes are stubbed."""

    def __init__(self, events):
        super().__init__("http://127.0.0.1:9")
        self._events = events
        self.votes = []
        self.cancelled = False
        self.closed_sessions = []

    def prompt(self, session_id, text="", **kwargs):
        from qwen_serve_client import PromptAck

        return PromptAck(prompt_id="p1", last_event_id=0, event_epoch=None, raw={})

    def iter_events(self, session_id, **kwargs):
        deadline = kwargs.get("deadline")
        for ev in self._events:
            if deadline is not None and time.monotonic() > deadline:
                return
            yield ev

    def respond_permission(self, session_id, request_id, option_id):
        self.votes.append(("selected", request_id, option_id))
        return {}

    def cancel_permission(self, session_id, request_id):
        self.votes.append(("cancelled", request_id, None))
        return {}

    def cancel(self, session_id):
        self.cancelled = True

    def close_session(self, session_id):
        self.closed_sessions.append(session_id)

    def close(self):
        pass


def ev(type_, data):
    return parse_envelope(json.dumps({"v": 1, "type": type_, "data": data}))


def text_chunk(text):
    return ev("session_update", {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    })


class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fenced_no_lang(self):
        self.assertEqual(_extract_json('```\n[1, 2]\n```'), [1, 2])

    def test_prose_embedded(self):
        self.assertEqual(
            _extract_json('答案是 {"a": 1} 如上'), {"a": 1}
        )

    def test_invalid(self):
        with self.assertRaises(ValueError):
            _extract_json("没有 JSON")


class TestErrorClassification(unittest.TestCase):
    def test_transient_statuses(self):
        self.assertTrue(DaemonHttpError(503, "overloaded").is_transient)
        self.assertTrue(DaemonHttpError(429, "rate limit").is_transient)
        self.assertFalse(DaemonHttpError(404, "gone").is_transient)
        self.assertFalse(DaemonHttpError(400, "bad").is_transient)

    def test_transient_codes(self):
        err = DaemonHttpError(503, "busy", code="session_limit_exceeded")
        self.assertTrue(err.is_transient)
        err = DaemonHttpError(404, "x", code="session_not_found")
        self.assertFalse(err.is_transient)

    def test_retry_after(self):
        err = DaemonHttpError(503, "x", retry_after=2.5)
        self.assertEqual(err.retry_after, 2.5)


class TestEventViews(unittest.TestCase):
    def test_usage_update(self):
        e = ev("session_update", {"sessionUpdate": "usage_update",
                                  "used": 1234, "size": 128000})
        self.assertEqual(e.usage(), {"used": 1234, "size": 128000})
        self.assertIsNone(text_chunk("x").usage())

    def test_permission_request(self):
        e = ev("permission_request", {
            "requestId": "r1", "sessionId": "s1",
            "toolCall": {"title": "Write"},
            "options": [{"optionId": "proceed_once"}, {"optionId": "reject_once"}],
        })
        preq = e.permission_request()
        self.assertIsNotNone(preq)
        self.assertEqual(preq.request_id, "r1")
        self.assertEqual(preq.tool_name, "Write")
        self.assertEqual(preq.option_ids(), ["proceed_once", "reject_once"])
        self.assertIsNone(text_chunk("x").permission_request())


class TestRunTurn(unittest.TestCase):
    def test_usage_captured_in_result(self):
        client = StubClient([
            text_chunk("hello"),
            ev("session_update", {"sessionUpdate": "usage_update",
                                  "used": 100, "size": 128000}),
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "p1"}),
        ])
        result = client.run_turn("s1", "hi")
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.usage, {"used": 100, "size": 128000})

    def test_on_permission_votes(self):
        client = StubClient([
            ev("permission_request", {
                "requestId": "r1",
                "options": [{"optionId": "proceed_once"}],
            }),
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "p1"}),
        ])
        client.run_turn("s1", "hi", on_permission=lambda p: "proceed_once")
        self.assertEqual(client.votes, [("selected", "r1", "proceed_once")])

    def test_on_permission_none_leaves_pending(self):
        client = StubClient([
            ev("permission_request", {"requestId": "r1", "options": []}),
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "p1"}),
        ])
        client.run_turn("s1", "hi", on_permission=lambda p: None)
        self.assertEqual(client.votes, [])

    def test_on_permission_exception_cancels_request(self):
        client = StubClient([
            ev("permission_request", {"requestId": "r1", "options": []}),
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "p1"}),
        ])

        def boom(p):
            raise RuntimeError("callback bug")

        with self.assertRaises(RuntimeError):
            client.run_turn("s1", "hi", on_permission=boom)
        self.assertEqual(client.votes, [("cancelled", "r1", None)])

    def test_turn_timeout_cancels(self):
        def slow_events(session_id, **kwargs):
            yield text_chunk("partial")
            time.sleep(0.1)
            yield ev("turn_complete", {"stopReason": "end_turn"})

        client = StubClient([])
        client.iter_events = slow_events
        with self.assertRaises(TurnTimeoutError):
            client.run_turn("s1", "hi", turn_timeout=0.05)
        self.assertTrue(client.cancelled)

    def test_wrong_prompt_id_ignored(self):
        client = StubClient([
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "other"}),
            ev("turn_complete", {"stopReason": "end_turn", "promptId": "p1"}),
        ])
        result = client.run_turn("s1", "hi")
        self.assertEqual(result.prompt_id, "p1")


class TestPromptBlocks(unittest.TestCase):
    def test_attachment_refs_in_prompt(self):
        client = QwenServeClient("http://127.0.0.1:9")
        captured = {}

        def fake_request(method, path, **kwargs):
            captured.update(kwargs.get("json") or {})
            return {"promptId": "p1", "lastEventId": 3}

        client.upload_attachment = lambda sid, f: {
            "type": "resource", "attachmentId": "a1",
            "mimeType": "text/plain", "size": 5,
        }
        client._request = fake_request
        ack = client.prompt("s1", "看图", attachments=["/tmp/x.txt"])
        self.assertEqual(ack.prompt_id, "p1")
        blocks = captured["prompt"]
        self.assertEqual(blocks[0], {"type": "text", "text": "看图"})
        self.assertEqual(blocks[1]["attachmentId"], "a1")

    def test_empty_prompt_rejected(self):
        client = QwenServeClient("http://127.0.0.1:9")
        with self.assertRaises(ValueError):
            client.prompt("s1", "")


class TestAskJson(unittest.TestCase):
    def test_retry_with_correction(self):
        client = StubClient([])
        outputs = ["这不是 JSON", '{"a": 1}']
        prompts = []

        class R:
            def __init__(self, t):
                self.text = t
                self.usage = None

        def fake_create(**kwargs):
            return SessionInfo("s1", None, False, {})

        def fake_run(sid, text, **kwargs):
            prompts.append(text)
            return R(outputs.pop(0))

        client.create_session = fake_create
        client.run_turn = fake_run
        value = client.ask_json("给我一个对象")
        self.assertEqual(value, {"a": 1})
        self.assertEqual(len(prompts), 2)
        self.assertIn("不满足要求", prompts[1])
        self.assertEqual(client.closed_sessions, ["s1"])

    def test_exhausted_raises(self):
        client = StubClient([])

        class R:
            text = "仍然不是 JSON"
            usage = None

        client.create_session = lambda **kw: SessionInfo("s1", None, False, {})
        client.run_turn = lambda *a, **kw: R()
        with self.assertRaises(JsonOutputError) as ctx:
            client.ask_json("给 JSON", max_attempts=2)
        self.assertEqual(ctx.exception.attempts, 2)


class TestSessionPool(unittest.TestCase):
    def test_reuse_and_close(self):
        client = StubClient([])
        created = []

        def fake_create(**kwargs):
            sid = f"s{len(created) + 1}"
            created.append(sid)
            return SessionInfo(sid, None, False, {})

        client.create_session = fake_create
        with client.session_pool(size=2) as pool:
            sid1 = pool.acquire()
            pool.release(sid1)
            sid2 = pool.acquire()
            self.assertEqual(sid1, sid2)
            self.assertEqual(created, ["s1"])
            pool.release(sid2)
        self.assertEqual(client.closed_sessions, ["s1"])

    def test_run_discards_broken_session(self):
        client = StubClient([])
        created = []
        client.create_session = lambda **kw: (
            created.append(f"s{len(created) + 1}")
            or SessionInfo(created[-1], None, False, {})
        )
        def boom(sid, text, **kw):
            raise TurnError("turn died")

        client.run_turn = boom
        with client.session_pool(size=1) as pool:
            with self.assertRaises(TurnError):
                pool.run("x")
            # 槽位被释放，下次 acquire 创建新会话
            sid = pool.acquire()
            self.assertEqual(sid, "s2")
            pool.release(sid)
        self.assertIn("s1", client.closed_sessions)


if __name__ == "__main__":
    unittest.main()
