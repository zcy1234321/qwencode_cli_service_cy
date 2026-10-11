"""多轮会话 + 流式输出 + agent 工具任务。

同一个 session_id 上连续 prompt，上下文自动延续；
on_text 回调逐 chunk 收到增量文本。
"""

from qwen_serve_client import QwenServeClient


def main() -> None:
    with QwenServeClient() as client:
        session = client.create_session(approval_mode="auto")
        sid = session.session_id
        print(f"session: {sid}\n")
        try:
            print("--- turn 1 ---")
            r1 = client.run_turn(sid, "记住数字 42。只回复：已记住。", on_text=print)
            print(f"\n[stop={r1.stop_reason}]")

            print("\n--- turn 2 ---")
            r2 = client.run_turn(sid, "我刚才让你记住的数字是几？", on_text=print)
            print(f"\n[stop={r2.stop_reason}]")

            print("\n--- turn 3 (agent 任务) ---")
            r3 = client.run_turn(
                sid,
                "列出当前目录下的文件（用工具），用一句话总结这个目录。",
                on_text=print,
            )
            print(f"\n[stop={r3.stop_reason}]")
        finally:
            client.close_session(sid)


if __name__ == "__main__":
    main()
