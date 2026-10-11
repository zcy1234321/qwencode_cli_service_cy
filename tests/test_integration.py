"""全链路集成测试：fake LLM + qwen serve daemon，不需要真实模型 key。

自动编排：起 scripts/fake-llm-server.mjs（node）→ 起 qwen serve
（QWEN_HOME 隔离、模型指向 fake LLM）→ 等健康检查 → 跑客户端断言。
node 或 qwen CLI 不可用则跳过。
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
FAKE_SERVER_MJS = os.path.join(SCRIPTS_DIR, "fake-llm-server.mjs")
TEST_TOKEN = "test-token-123"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _qwen_cmd() -> list[str]:
    for name in ("qwen.cmd", "qwen.exe", "qwen"):
        found = shutil.which(name)
        if found:
            return [found]
    raise RuntimeError("qwen CLI 不在 PATH 中")


class DaemonFixture:
    """起 fake LLM + qwen serve，测试结束后全部清理。"""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []
        self.tmp = tempfile.TemporaryDirectory(prefix="qwen-home-")
        self.base_url = ""
        self.token = TEST_TOKEN

    def __enter__(self) -> "DaemonFixture":
        # 1) fake LLM
        fake = subprocess.Popen(
            ["node", FAKE_SERVER_MJS],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.procs.append(fake)
        assert fake.stdout is not None
        fake_url = None
        deadline = time.time() + 30
        while time.time() < deadline:
            line = fake.stdout.readline()
            if line.startswith("READY "):
                fake_url = line.split()[1].strip()
                break
            if fake.poll() is not None:
                raise RuntimeError(f"fake LLM server 退出: {line}")
        if not fake_url:
            raise RuntimeError("fake LLM server 启动超时")

        # 2) qwen serve，QWEN_HOME 隔离，模型指向 fake LLM
        port = _free_port()
        env = {
            **os.environ,
            "QWEN_HOME": self.tmp.name,
            "OPENAI_API_KEY": "fake-key",
            "OPENAI_BASE_URL": fake_url,
            "OPENAI_MODEL": "fake-model",
        }
        daemon = subprocess.Popen(
            [
                *_qwen_cmd(),
                "serve",
                "--port", str(port),
                "--token", TEST_TOKEN,
                "--require-auth",
                "--no-web",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        self.procs.append(daemon)
        self.base_url = f"http://127.0.0.1:{port}"

        # 3) 等 /health
        deadline = time.time() + 90
        while time.time() < deadline:
            if daemon.poll() is not None:
                raise RuntimeError("qwen serve 提前退出")
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {TEST_TOKEN}"},
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        return self
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("qwen serve 健康检查超时")

    def __exit__(self, *exc: object) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.tmp.cleanup()


@unittest.skipUnless(
    shutil.which("node") and (
        shutil.which("qwen") or shutil.which("qwen.cmd") or shutil.which("qwen.exe")
    ),
    "需要 node 和 qwen CLI 在 PATH 中",
)
class TestAgainstFakeLlm(unittest.TestCase):
    fixture: DaemonFixture

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = DaemonFixture()
        cls.fixture.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.__exit__()

    def _client(self):
        from qwen_serve_client import QwenServeClient

        return QwenServeClient(self.fixture.base_url, self.fixture.token)

    def test_health_and_capabilities(self) -> None:
        with self._client() as c:
            self.assertEqual(c.health()["status"], "ok")
            caps = c.capabilities()
            for f in ("session_create", "session_prompt", "session_events"):
                self.assertIn(f, caps.get("features") or [])

    def test_auth_required(self) -> None:
        import urllib.error

        req = urllib.request.Request(f"{self.fixture.base_url}/capabilities")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_ask_one_shot(self) -> None:
        with self._client() as c:
            answer = c.ask("hello", approval_mode="auto")
        self.assertIn("fake-answer", answer)

    def test_multi_turn_and_approval_mode(self) -> None:
        with self._client() as c:
            session = c.create_session(approval_mode="auto")
            sid = session.session_id
            try:
                r1 = c.run_turn(sid, "first")
                self.assertIn("fake-answer", r1.text)
                r2 = c.run_turn(sid, "second")
                self.assertIn("fake-answer", r2.text)
                resp = c.set_approval_mode(sid, "auto-edit")
                self.assertEqual(resp["mode"], "auto-edit")
            finally:
                c.close_session(sid)
            # 关闭后再关一次应幂等
            c.close_session(sid)

    def test_async_client(self) -> None:
        import asyncio

        from qwen_serve_client import AsyncQwenServeClient

        async def run() -> str:
            async with AsyncQwenServeClient(
                self.fixture.base_url, self.fixture.token
            ) as c:
                return await c.ask("hello", approval_mode="auto")

        answer = asyncio.run(run())
        self.assertIn("fake-answer", answer)

    def test_turn_timeout_and_usage_field(self) -> None:
        # 正常完成不受 turn_timeout 影响；usage 字段存在（fake LLM 下可为 None）
        with self._client() as c:
            session = c.create_session(approval_mode="auto")
            try:
                r = c.run_turn(session.session_id, "hello", turn_timeout=60)
                self.assertIn("fake-answer", r.text)
                self.assertTrue(hasattr(r, "usage"))
            finally:
                c.close_session(session.session_id)

    def test_session_pool(self) -> None:
        with self._client() as c:
            with c.session_pool(size=2, approval_mode="auto") as pool:
                r1 = pool.run("first")
                r2 = pool.run("second")
                self.assertIn("fake-answer", r1.text)
                self.assertIn("fake-answer", r2.text)

    def test_upload_attachment_and_prompt(self) -> None:
        with self._client() as c:
            if not c.has_feature("session_attachments"):
                self.skipTest("daemon 无 session_attachments 路由（如 0.21.x）")
            session = c.create_session(approval_mode="auto")
            try:
                ref = c.upload_attachment(
                    session.session_id, b"hello attachment", name="note.txt"
                )
                self.assertEqual(ref["type"], "resource")
                self.assertTrue(ref["attachmentId"])
                self.assertEqual(ref["size"], len(b"hello attachment"))
                r = c.run_turn(
                    session.session_id, "读一下附件", attachments=[ref],
                    turn_timeout=60,
                )
                self.assertIn("fake-answer", r.text)
            finally:
                c.close_session(session.session_id)

    def test_ask_json_retries_then_raises(self) -> None:
        from qwen_serve_client import JsonOutputError

        with self._client() as c:
            # fake LLM 永远返回 "fake-answer-N"，永远不是 JSON
            with self.assertRaises(JsonOutputError) as ctx:
                c.ask_json("返回一个 JSON 对象", max_attempts=2, turn_timeout=60)
            self.assertEqual(ctx.exception.attempts, 2)
            self.assertIn("fake-answer", ctx.exception.last_output)


if __name__ == "__main__":
    unittest.main()
