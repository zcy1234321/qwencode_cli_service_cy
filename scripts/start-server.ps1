# 启动 qwen serve 推理后端（本机模式：仅 127.0.0.1 可访问）
#
# 用法：
#   $env:QWEN_SERVER_TOKEN = "你的密钥"   # 可选；不设置则自动生成并打印一次
#   ./scripts/start-server.ps1
#
# 跨机器访问时把 -Hostname 改为 0.0.0.0，并务必设置强 token；
# 生产环境再加 --tls-cert/--tls-key 启用 HTTPS。

param(
    [int]$Port = 4170,
    [string]$Hostname = "127.0.0.1",
    [int]$MaxSessions = 16,
    # 一轮对话结束后、会话无 SSE 客户端时的保留宽限（毫秒）。
    # 默认 0 = 立即拆会话进程，下一条消息要冷启动重建（很慢）；
    # Web UI 是轮询式 SSE 客户端，必须给一个宽限窗口。
    [int]$PromptSettledGraceMs = 300000
)

$token = $env:QWEN_SERVER_TOKEN
if (-not $token) {
    $token = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    Write-Host "QWEN_SERVER_TOKEN 未设置，本次使用临时 token："
    Write-Host "  $token"
    Write-Host "客户端请用此 token（或先 set QWEN_SERVER_TOKEN 再启动本脚本）。"
}

qwen serve `
    --port $Port `
    --hostname $Hostname `
    --token $token `
    --require-auth `
    --no-web `
    --max-sessions $MaxSessions `
    --session-prompt-settled-close-grace-ms $PromptSettledGraceMs
