"""端到端冒烟：health → capabilities → ask → 多轮 → approval-mode → 并发。

前置：daemon 已启动（scripts/start-server.ps1），环境变量可选：
    QWEN_SERVE_URL     默认 http://127.0.0.1:4170
    QWEN_SERVER_TOKEN  daemon 的 bearer token
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor

from qwen_serve_client import QwenServeClient


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    base_url = os.environ.get("QWEN_SERVE_URL", "http://127.0.0.1:4170")
    client = QwenServeClient(base_url)

    print(f"[1/6] health: {client.health()}")
    caps = client.capabilities()
    features = caps.get("features") or []
    print(f"[2/6] capabilities: protocol={caps.get('protocolVersions')}, "
          f"{len(features)} features")
    for required in ("session_create", "session_prompt", "session_events"):
        assert client.has_feature(required), f"daemon 缺少能力: {required}"

    print("[3/6] ask() 一问一答 ...")
    answer = client.ask("回复两个字：你好")
    assert answer.strip(), "空回答"
    print(f"      answer: {answer.strip()[:120]}")

    print("[4/6] 多轮会话（上下文延续）...")
    session = client.create_session(approval_mode="auto")
    sid = session.session_id
    try:
        client.run_turn(sid, "记住数字 42。只回复：已记住。")
        r2 = client.run_turn(sid, "我刚才让你记住的数字是几？")
        assert "42" in r2.text, f"多轮上下文丢失: {r2.text!r}"
        print(f"      turn2: {r2.text.strip()[:120]} (stop={r2.stop_reason})")

        print("[5/6] set_approval_mode ...")
        resp = client.set_approval_mode(sid, "auto-edit")
        assert resp.get("mode") == "auto-edit", resp
        client.set_approval_mode(sid, "auto")
    finally:
        client.close_session(sid)

    print("[6/6] 并发：2 个独立会话同时跑 ...")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda i: client.ask(f"只回复数字 {i}"),
            [1, 2],
        ))
    assert "1" in results[0] and "2" in results[1], results
    print(f"      results: {[r.strip()[:40] for r in results]}")

    print("\nSMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
