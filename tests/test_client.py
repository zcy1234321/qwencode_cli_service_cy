import json
import unittest

from qwen_serve_client.events import Event, parse_envelope
from qwen_serve_client.errors import DaemonHttpError


class TestParseEnvelope(unittest.TestCase):
    def test_regular_event(self) -> None:
        raw = json.dumps({
            "id": 7,
            "v": 1,
            "type": "session_update",
            "data": {
                "sessionId": "s-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "你好"},
                },
            },
        })
        ev = parse_envelope(raw)
        assert ev is not None
        self.assertEqual(ev.type, "session_update")
        self.assertEqual(ev.id, 7)
        self.assertEqual(ev.text_chunk(), "你好")
        self.assertFalse(ev.is_terminal)

    def test_text_chunk_accepts_unnested_shape(self) -> None:
        ev = Event(type="session_update", data={
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "x"},
        })
        self.assertEqual(ev.text_chunk(), "x")

    def test_synthetic_frame_has_no_id(self) -> None:
        ev = parse_envelope(json.dumps({"v": 1, "type": "replay_complete",
                                        "data": {"replayedCount": 0}}))
        assert ev is not None
        self.assertIsNone(ev.id)

    def test_terminal_events(self) -> None:
        for t in ("session_died", "session_closed", "client_evicted",
                  "stream_error"):
            ev = parse_envelope(json.dumps({"v": 1, "type": t, "data": {}}))
            assert ev is not None
            self.assertTrue(ev.is_terminal)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_envelope("not json"))
        self.assertIsNone(parse_envelope(json.dumps([1, 2, 3])))
        self.assertIsNone(parse_envelope(json.dumps({"data": {}})))

    def test_text_chunk_ignores_other_updates(self) -> None:
        ev = Event(type="session_update",
                   data={"sessionUpdate": "tool_call", "content": {}})
        self.assertIsNone(ev.text_chunk())
        ev = Event(type="turn_complete", data={"stopReason": "end_turn"})
        self.assertIsNone(ev.text_chunk())


class TestDaemonHttpError(unittest.TestCase):
    def test_carries_code_and_status(self) -> None:
        err = DaemonHttpError(404, "No session", code="session_not_found")
        self.assertEqual(err.status_code, 404)
        self.assertEqual(err.code, "session_not_found")
        self.assertIn("404", str(err))


if __name__ == "__main__":
    unittest.main()
