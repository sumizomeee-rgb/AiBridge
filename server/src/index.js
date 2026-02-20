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
app.use(express.json());

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
    writeChunk(task, msg.delta);
  } else if (msg.type === 'done') {
    finishTask(task);
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

// --- 通用：派发任务到 Extension ---
function dispatchTask(res, format, model, messages, newChat) {
  model = resolveModel(model);
  console.log(`[Dispatch] model=${model}, format=${format}, newChat=${newChat}`);
  if (!extensionWs || extensionWs.readyState !== extensionWs.OPEN) {
    return res.status(503).json({ error: { code: 'NO_EXTENSION', message: 'Extension not connected' } });
  }

  const task_id = uuidv4();
  console.log(`[Dispatch] task_id=${task_id}, sending to extension...`);

  // SSE headers
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();

  activeTasks.set(task_id, { res, format, task_id, model });

  extensionWs.send(JSON.stringify({
    type: 'task', task_id, model, messages, stream: true, new_chat: newChat
  }));
  console.log(`[Dispatch] task sent to extension`);

  // 客户端断开时清理
  res.on('close', () => activeTasks.delete(task_id));
}

// --- OpenAI 兼容接口 ---
app.post('/v1/chat/completions', (req, res) => {
  const { model = 'gpt-4', messages = [] } = req.body;
  // 自动判断：只有1条用户消息（可能有system）视为新对话
  const userMsgs = messages.filter(m => m.role === 'user');
  const newChat = req.body.new_chat ?? (userMsgs.length <= 1);
  dispatchTask(res, 'openai', model, messages, newChat);
});

// --- Anthropic 兼容接口 ---
app.post('/v1/messages', (req, res) => {
  const { model = 'claude', messages: rawMsgs = [] } = req.body;
  const userMsgs = rawMsgs.filter(m => m.role === 'user');
  const newChat = req.body.new_chat ?? (userMsgs.length <= 1);
  // Anthropic content 可能是数组，统一转为字符串
  const messages = rawMsgs.map(m => ({
    role: m.role,
    content: Array.isArray(m.content)
      ? m.content.map(b => b.text || '').join('')
      : m.content
  }));
  dispatchTask(res, 'anthropic', model, messages, new_chat);
});

// --- 健康检查 ---
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', extension: !!extensionWs });
});

server.listen(PORT, () => {
  console.log(`[Server] Running on http://localhost:${PORT}`);
  console.log(`[Server] WebSocket ready on ws://localhost:${PORT}`);
});
