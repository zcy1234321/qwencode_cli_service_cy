"""嵌入程序的工程化模式：会话池 + 超时 + 结构化输出 + 附件 + 工具审批。

前置：daemon 已启动（scripts/start-server.ps1），或跑
scripts/start_demo_stack.py 用 fake 模型体验。
"""

from concurrent.futures import ThreadPoolExecutor

from qwen_serve_client import (
    JsonOutputError,
    QwenServeClient,
    TurnTimeoutError,
)


def main() -> None:
    tasks = [
        "用一句话概括：我今天去了超市买了苹果。",
        "用一句话概括：明天的会议改到下午三点。",
    ]

    with QwenServeClient() as client:
        # --- 并发 + 会话池 + 超时：每个任务复用池里的 session ---
        with client.session_pool(size=2, approval_mode="auto") as pool:
            with ThreadPoolExecutor(max_workers=2) as ex:
                futures = [ex.submit(pool.run, t, turn_timeout=60) for t in tasks]
                for f in futures:
                    try:
                        r = f.result()
                        print(f"[ok] {r.text.strip()[:60]}  usage={r.usage}")
                    except TurnTimeoutError:
                        print("[timeout] 本轮已自动取消")

        # --- 结构化输出：失败自动纠正，超次数抛 JsonOutputError ---
        try:
            data = client.ask_json(
                "从这段话提取 action 和 deadline：周五前把报价单发给王芳",
                validator=lambda v: isinstance(v, dict) and "action" in v,
                turn_timeout=60,
            )
            print(f"[json] {data}")
        except JsonOutputError as e:
            print(f"[json 失败] {e.attempts} 次尝试后放弃: {e.last_output[:60]}")

        # --- 程序化工具审批：default 模式下由你的代码决定工具是否放行 ---
        session = client.create_session(approval_mode="default")
        try:
            def approve(req):
                print(f"[审批] {req.tool_name} 选项: {req.option_ids()}")
                return req.option_ids()[0] if req.option_ids() else None

            r = client.run_turn(
                session.session_id, "你好", on_permission=approve,
                turn_timeout=60,
            )
            print(f"[turn] {r.text.strip()[:60]}")
        finally:
            client.close_session(session.session_id)

        # --- 附件（需 daemon 支持 session_attachments，0.21.x 没有）---
        if client.has_feature("session_attachments"):
            session = client.create_session(approval_mode="auto")
            try:
                r = client.run_turn(
                    session.session_id,
                    "总结这个文件",
                    attachments=["README.md"],
                    turn_timeout=120,
                )
                print(f"[附件] {r.text.strip()[:60]}")
            finally:
                client.close_session(session.session_id)


if __name__ == "__main__":
    main()
