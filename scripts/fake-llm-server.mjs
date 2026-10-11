// 独立的 fake OpenAI 兼容 server：固定内容回应 /chat/completions。
// 仅用于无真实模型 key 的集成测试。
// 用法：node scripts/fake-llm-server.mjs  →  打印 "READY <baseUrl>" 后常驻。

import { createServer } from 'node:http';

let requestIndex = 0;

const server = createServer((req, res) => {
  // OpenAI 兼容模型列表（供 UI 的 /api/models/fetch 测试）
  if (req.method === 'GET' && req.url?.endsWith('/v1/models')) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(
      JSON.stringify({
        object: 'list',
        data: [
          { id: 'fake-model', object: 'model' },
          { id: 'fake-model-large', object: 'model' },
        ],
      }),
    );
    return;
  }
  if (req.method !== 'POST' || !req.url?.endsWith('/chat/completions')) {
    res.writeHead(404);
    res.end('not found');
    return;
  }
  let raw = '';
  req.on('data', (chunk) => (raw += chunk));
  req.on('end', () => {
    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      res.writeHead(400);
      res.end('bad json');
      return;
    }
    const idx = requestIndex++;
    const text = `fake-answer-${idx}`;
    const model = typeof body.model === 'string' ? body.model : 'fake-model';
    // E2E 触发器：最后一条 user 消息含 [[tool]] 时，返回 run_shell_command 工具调用，
    // 用来走通 daemon → UI 的权限审批链路。工具结果回来后（last role=tool）恢复正常回答。
    const msgs = Array.isArray(body.messages) ? body.messages : [];
    const last = msgs[msgs.length - 1] || {};
    const lastText = typeof last.content === 'string'
      ? last.content
      : (Array.isArray(last.content) ? last.content.map((c) => c && c.text || '').join('') : '');
    const wantTool = last.role === 'user' && lastText.includes('[[tool]]');
    const toolCall = {
      id: `call_fake_${idx}`,
      type: 'function',
      function: { name: 'run_shell_command', arguments: '{"command":"rm -rf ./tmp/cache && echo rebuilt"}' },
    };
    if (body.stream === true) {
      res.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-cache',
      });
      const chunk = (delta, finishReason = null) =>
        JSON.stringify({
          id: `chatcmpl-fake-${idx}`,
          object: 'chat.completion.chunk',
          created: 0,
          model,
          choices: [{ index: 0, delta, finish_reason: finishReason }],
        });
      res.write(`data: ${chunk({ role: 'assistant', content: '' })}\n\n`);
      if (wantTool) {
        res.write(`data: ${chunk({ tool_calls: [{ index: 0, ...toolCall }] }, 'tool_calls')}\n\n`);
      } else {
        res.write(`data: ${chunk({ content: text })}\n\n`);
        res.write(`data: ${chunk({}, 'stop')}\n\n`);
      }
      res.write('data: [DONE]\n\n');
      res.end();
    } else {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(
        JSON.stringify({
          id: `chatcmpl-fake-${idx}`,
          object: 'chat.completion',
          created: 0,
          model,
          choices: [
            {
              index: 0,
              message: wantTool
                ? { role: 'assistant', content: null, tool_calls: [toolCall] }
                : { role: 'assistant', content: text },
              finish_reason: wantTool ? 'tool_calls' : 'stop',
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }),
      );
    }
  });
});

server.listen(Number(process.env.FAKE_PORT || 0), '127.0.0.1', () => {
  console.log(`READY http://127.0.0.1:${server.address().port}`);
});
