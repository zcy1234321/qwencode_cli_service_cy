"""本地演示栈：fake LLM + qwen serve + Web UI 一键拉起（供浏览器手动体验）。

跑法：python scripts/start_demo_stack.py
停掉：Ctrl+C（三个进程一起退）。
端口：fake LLM 随机，daemon 4170，UI 8080。token 固定 demo-token。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_MJS = os.path.join(REPO_ROOT, "scripts", "fake-llm-server.mjs")
TOKEN = "demo-token"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    tmp = tempfile.TemporaryDirectory(prefix="qwen-home-demo-")
    procs: list[subprocess.Popen] = []
    try:
        fake = subprocess.Popen(
            ["node", FAKE_MJS], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        procs.append(fake)
        assert fake.stdout
        fake_url = next(
            line.split()[1] for line in fake.stdout if line.startswith("READY ")
        )

        qwen = shutil.which("qwen.cmd") or shutil.which("qwen") or "qwen"
        daemon_env = {
            **os.environ, "QWEN_HOME": tmp.name,
            "OPENAI_API_KEY": "fake-key", "OPENAI_BASE_URL": fake_url,
            "OPENAI_MODEL": "fake-model",
        }
        procs.append(subprocess.Popen(
            [qwen, "serve", "--port", "4170", "--token", TOKEN,
             "--require-auth", "--no-web",
             # 轮询式 SSE 客户端（Web UI）需要宽限窗口，否则每轮结束就
             # 拆会话进程、下一条消息冷启动重建（需要 qwen >= 0.23.0）
             "--session-prompt-settled-close-grace-ms", "300000"],
            env=daemon_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "qwen_serve_ui",
             "--host", "127.0.0.1", "--port", "8080",
             "--daemon", "http://127.0.0.1:4170"],
            env={**os.environ, "QWEN_SERVER_TOKEN": TOKEN, "QWEN_HOME": tmp.name},
        ))

        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8080/api/meta", timeout=2
                ) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)

        print("=" * 56)
        print("演示栈已就绪（fake 模型，回答固定为 fake-answer-N）")
        print(f"  Web UI:  http://127.0.0.1:8080")
        print(f"  daemon:  http://127.0.0.1:4170  (token: {TOKEN})")
        print("  Ctrl+C 停止全部")
        print("=" * 56)
        procs[1].wait()
        return 0
    finally:
        for p in procs:
            p.terminate()
        tmp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
