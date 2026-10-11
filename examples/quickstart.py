"""最简单用法：一问一答。

前置：qwen serve 已在 127.0.0.1:4170 运行。
    set QWEN_SERVER_TOKEN=...   （daemon 用 --token 启动时）
"""

from qwen_serve_client import ask

answer = ask("用一句话介绍你自己。")
print(answer)
