import express from 'express';
import cors from 'cors';
import { createServer } from 'http';
import { WebSocketServer } from 'ws';
import { v4 as uuidv4 } from 'uuid';
import { readFileSync } from 'fs';
import { execSync } from 'child_process';

const config = JSON.parse(readFileSync(new URL('../config.json', import.meta.url), 'utf-8'));

// 启动前自动杀掉占用端口的进程
try {
  const pid = execSync(`netstat -ano | findstr :${config.server.port} | findstr LISTENING`, { encoding: 'utf-8' })
    .trim().split('\n')[0]?.trim().split(/\s+/).pop();
  if (pid && pid !== '0') {
    execSync(`taskkill /f /pid ${pid}`);
    console.log(`[Server] Killed process ${pid} on port ${config.server.port}`);
  }
} catch { /* 没有占用则忽略 */ }

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

const PORT = config.server.port;
const activeTasks = new Map();
let extensionWs = null;

// --- HTTP Server + WebSocket 共用端口 ---
const server = createServer(app);
const wss = new WebSocketServer({ server });

// --- WebSocket 连接管理 ---
wss.on('connection', (ws) => {
  console.log('[WS] Extension connected');
  extensionWs = ws;

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    handleExtensionMessage(msg);
  });

  ws.on('close', () => {
    console.log('[WS] Extension disconnected');
    if (extensionWs === ws) extensionWs = null;
  });

  // 心跳
  const heartbeat = setInterval(() => {
    if (ws.readyState === ws.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
  }, 15000);
  ws.on('close', () => clearInterval(heartbeat));
});

// --- 处理 Extension 回传消息 ---
function handleExtensionMessage(msg) {
  if (msg.type === 'pong') return;
  console.log(`[ExtMsg] type=${msg.type}, task_id=${msg.task_id}`);

  const task = activeTasks.get(msg.task_id);
  if (!task) return;

  if (msg.type === 'chunk') {
    if (task.hasTools) {
      // 有 tools 时缓冲全部文本，最后统一解析
      task.buffer = (task.buffer || '') + msg.delta;
    } else {
      writeChunk(task, msg.delta);
    }
  } else if (msg.type === 'done') {
    if (task.hasTools) {
      // 优先使用完整文本（避免 markdown 重排导致增量 chunk 乱码）
      if (msg.fullText) task.buffer = msg.fullText;
      finishTaskWithToolParsing(task);
    } else {
      finishTask(task);
    }
    activeTasks.delete(msg.task_id);
  } else if (msg.type === 'error') {
    writeError(task, msg.code, msg.message);
    activeTasks.delete(msg.task_id);
  }
}

// --- SSE 写入工具函数 ---
function writeChunk(task, delta) {
  const { res, format, task_id, model } = task;
  if (format === 'anthropic') {
    res.write(`event: content_block_delta\ndata: ${JSON.stringify({
      type: 'content_block_delta', index: 0,
      delta: { type: 'text_delta', text: delta }
    })}\n\n`);
  } else {
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000), model,
      choices: [{ index: 0, delta: { content: delta }, finish_reason: null }]
    })}\n\n`);
  }
}

function finishTask(task) {
  const { res, format, task_id, model } = task;
  if (format === 'anthropic') {
    res.write(`event: message_stop\ndata: {"type":"message_stop"}\n\n`);
  } else {
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000), model,
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }]
    })}\n\n`);
    res.write(`data: [DONE]\n\n`);
  }
  res.end();
}

// --- Tool Call 解析 ---
function repairJson(str) {
  // 修复常见的 JSON 问题：未转义的反斜杠（Windows路径）
  // 先尝试直接解析，失败再修复
  try { return JSON.parse(str); } catch {}
  // 替换未转义的反斜杠
  try { return JSON.parse(str.replace(/\\/g, '\\\\')); } catch {}
  // 尝试提取 name 和 arguments 用正则
  try {
    const nameMatch = str.match(/"name"\s*:\s*"([^"]+)"/);
    const argsMatch = str.match(/"arguments"\s*:\s*(\{[\s\S]*\})\s*$/);
    if (nameMatch) {
      let args = {};
      if (argsMatch) try { args = JSON.parse(argsMatch[1].replace(/\\/g, '\\\\')); } catch {}
      return { name: nameMatch[1], arguments: args };
    }
  } catch {}
  return null;
}

function parseToolCalls(text) {
  const regex = /<tool_call>([\s\S]*?)<\/tool_call>/g;
  const calls = [];
  let match;
  while ((match = regex.exec(text)) !== null) {
    const parsed = repairJson(match[1].trim());
    if (parsed && parsed.name) {
      calls.push({
        id: `call_${uuidv4().slice(0, 8)}`,
        type: 'function',
        function: {
          name: parsed.name,
          arguments: typeof parsed.arguments === 'string' ? parsed.arguments : JSON.stringify(parsed.arguments || {})
        }
      });
    }
  }
  return calls;
}

function finishTaskWithToolParsing(task) {
  const { res, task_id, model, buffer } = task;
  const toolCalls = parseToolCalls(buffer || '');
  console.log(`[ToolParse] found ${toolCalls.length} tool calls`);

  if (toolCalls.length > 0) {
    // 发送 tool_calls 格式
    for (let i = 0; i < toolCalls.length; i++) {
      const tc = toolCalls[i];
      res.write(`data: ${JSON.stringify({
        id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
        created: Math.floor(Date.now() / 1000), model,
        choices: [{ index: 0, delta: { tool_calls: [{ index: i, ...tc }] }, finish_reason: null }]
      })}\n\n`);
    }
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000), model,
      choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }]
    })}\n\n`);
  } else {
    // 没有 tool call，作为普通文本发送
    if (buffer) {
      res.write(`data: ${JSON.stringify({
        id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
        created: Math.floor(Date.now() / 1000), model,
        choices: [{ index: 0, delta: { content: buffer }, finish_reason: null }]
      })}\n\n`);
    }
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`, object: 'chat.completion.chunk',
      created: Math.floor(Date.now() / 1000), model,
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }]
    })}\n\n`);
  }
  res.write(`data: [DONE]\n\n`);
  res.end();
}

function writeError(task, code, message) {
  const { res, format } = task;
  const status = code === 'RATE_LIMITED' ? 429 : 502;
  if (res.headersSent) {
    // SSE 已开始，只能通过流发送错误后关闭
    res.write(`data: ${JSON.stringify({ error: { code, message } })}\n\n`);
    res.end();
  } else {
    res.status(status).json({ error: { code, message } });
  }
}

// --- API Key 校验 ---
function authMiddleware(req, res, next) {
  const key = config.auth.apiKey;
  if (!key) return next(); // 未配置则跳过校验
  const provided = (req.headers.authorization || '').replace('Bearer ', '')
    || req.headers['x-api-key'] || '';
  if (provided !== key) {
    return res.status(401).json({ error: { code: 'UNAUTHORIZED', message: 'Invalid API key' } });
  }
  next();
}
app.use('/v1', authMiddleware);

// --- 模型名映射 ---
function resolveModel(model) {
  const map = config.models || {};
  return map[model] || model;
}

// --- Tool Call System Prompt ---
// 只保留 web AI 能正确调用的工具
const ALLOWED_TOOLS = new Set(['bash', 'read', 'write', 'edit', 'glob', 'grep']);

function filterTools(tools) {
  if (!tools) return null;
  const filtered = tools.filter(t => {
    const name = (t.function?.name || t.name || '').toLowerCase();
    return ALLOWED_TOOLS.has(name);
  });
  return filtered.length ? filtered : null;
}

function extractCwd(messages) {
  for (const m of messages) {
    const text = typeof m.content === 'string' ? m.content : '';
    const match = text.match(/Working directory:\s*(.+)/i) || text.match(/current directory[:\s]+(.+)/i);
    if (match) return match[1].trim().replace(/\\/g, '\\\\');
  }
  return null;
}

function buildToolSystemPrompt(tools, cwd) {
  const defs = tools.map(t => {
    const f = t.function || t;
    const params = Object.keys(f.parameters?.properties || {}).join(', ');
    return `- ${f.name}: ${(f.description || '').slice(0, 120)}${params ? `\n  Params: ${params}` : ''}`;
  }).join('\n');
  return `${cwd ? `You are working in directory: ${cwd}\nIMPORTANT: All file paths MUST be based on this directory. Do NOT invent or guess paths.\n` : ''}You have the following tools:
${defs}

Do NOT use built-in tools (Python, code interpreter, etc). ONLY use the XML format below:
<tool_call>
{"name": "ACTUAL_TOOL_NAME", "arguments": {"param": "value"}}
</tool_call>

Backslashes in strings MUST be doubled: C:\\\\Users not C:\\Users`;
}

// --- 通用：派发任务到 Extension ---
function dispatchTask(res, format, model, messages, newChat, tools) {
  model = resolveModel(model);
  const hasTools = tools && tools.length > 0;
  console.log(`[Dispatch] model=${model}, format=${format}, newChat=${newChat}, tools=${hasTools ? tools.length : 0}`);

  if (!extensionWs || extensionWs.readyState !== extensionWs.OPEN) {
    return res.status(503).json({ error: { code: 'NO_EXTENSION', message: 'Extension not connected' } });
  }

  // 注入 tool 定义到最后一条用户消息（仅首轮，tool result 轮不重复注入）
  const isToolResultRound = messages.some(m => m.role === 'tool' || m.role === 'tool_result');
  if (hasTools && !isToolResultRound) {
    const toolPrompt = buildToolSystemPrompt(tools, extractCwd(messages));
    messages = [...messages];
    const lastUserIdx = messages.findLastIndex(m => m.role === 'user');
    if (lastUserIdx >= 0) {
      messages[lastUserIdx] = {
        ...messages[lastUserIdx],
        content: messages[lastUserIdx].content + '\n\n' + toolPrompt
      };
    }
  }

  const task_id = uuidv4();
  console.log(`[Dispatch] task_id=${task_id}, sending to extension...`);

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  activeTasks.set(task_id, { res, format, task_id, model, hasTools, buffer: '' });

  extensionWs.send(JSON.stringify({
    type: 'task', task_id, model, messages, stream: true, new_chat: newChat
  }));
  console.log(`[Dispatch] task sent to extension`);

  res.on('close', () => activeTasks.delete(task_id));
}

// --- OpenAI 兼容接口 ---
app.post('/v1/chat/completions', (req, res) => {
  const { model = 'gpt-4', messages = [], tools } = req.body;
  const userMsgs = messages.filter(m => m.role === 'user');
  const hasToolResults = messages.some(m => m.role === 'tool');
  // 有 tool 结果说明是工具回传，不开新对话
  const newChat = req.body.new_chat ?? (!hasToolResults && userMsgs.length <= 1);
  dispatchTask(res, 'openai', model, messages, newChat, filterTools(tools));
});

// --- Anthropic 兼容接口 ---
app.post('/v1/messages', (req, res) => {
  const { model = 'claude', messages: rawMsgs = [], tools } = req.body;
  const userMsgs = rawMsgs.filter(m => m.role === 'user');
  const hasToolResults = rawMsgs.some(m => m.role === 'tool_result' || m.role === 'tool');
  const newChat = req.body.new_chat ?? (!hasToolResults && userMsgs.length <= 1);
  const messages = rawMsgs.map(m => ({
    role: m.role,
    content: Array.isArray(m.content)
      ? m.content.map(b => b.text || '').join('')
      : m.content
  }));
  dispatchTask(res, 'anthropic', model, messages, newChat, filterTools(tools));
});

// --- 健康检查 ---
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', extension: !!extensionWs });
});

server.listen(PORT, () => {
  console.log(`[Server] Running on http://localhost:${PORT}`);
  console.log(`[Server] WebSocket ready on ws://localhost:${PORT}`);
});
