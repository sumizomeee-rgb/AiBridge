import express from 'express';
import cors from 'cors';
import { v4 as uuidv4 } from 'uuid';
import { execSync } from 'child_process';
import { resolve } from 'path';
import { ConfigStore } from './config-store.js';
import { KeyStore } from './key-store.js';
import { TaskLog } from './task-log.js';
import { PlaywrightDriver } from './playwright-driver.js';
import { createAdminRouter } from './admin-routes.js';
import { PUBLIC_DIR } from './paths.js';
import { resolveRoute } from './router.js';
import { filterTools, injectToolPrompt } from './tool-prompt.js';
import { finishTask, finishTaskWithToolParsing, writeChunk, writeError } from './sse.js';

const configStore = new ConfigStore();
const keyStore = new KeyStore(configStore);
const taskLog = new TaskLog();
const playwrightDriver = new PlaywrightDriver();

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

const PORT = configStore.getServer().port || 9529;

try {
  const pid = execSync(`netstat -ano | findstr :${PORT} | findstr LISTENING`, { encoding: 'utf-8' })
    .trim().split('\n')[0]?.trim().split(/\s+/).pop();
  if (pid && pid !== '0') {
    execSync(`taskkill /f /pid ${pid}`);
    console.log(`[Server] Killed process ${pid} on port ${PORT}`);
  }
} catch {
  // 没有占用则忽略
}

function authMiddleware(req, res, next) {
  const provided = (req.headers.authorization || '').replace('Bearer ', '')
    || req.headers['x-api-key'] || '';
  const verified = keyStore.verify(provided, req);
  if (!verified && (configStore.getAuth().apiKey || keyStore.list().length)) {
    return res.status(401).json({ error: { code: 'UNAUTHORIZED', message: 'Invalid API key' } });
  }
  req.apiKey = verified;
  next();
}

function prepareSse(res) {
  res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();
}

function looksLikeFreshConversation(messages) {
  const dialogMessages = messages.filter(message => message.role !== 'system');
  const userMessages = dialogMessages.filter(message => message.role === 'user');
  const priorMessages = dialogMessages.filter(message => message.role !== 'user');
  return userMessages.length === 1 && priorMessages.length === 0;
}

function estimateInputTokens(messages, tools) {
  const text = JSON.stringify({ messages, tools: tools || [] });
  return text ? Math.max(1, Math.ceil(text.length / 4)) : 0;
}

function stringifyAnthropicContent(content) {
  if (content === undefined || content === null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(stringifyAnthropicBlock).filter(Boolean).join('\n\n');
  }
  if (typeof content === 'object') {
    if (typeof content.text === 'string') return content.text;
    if (content.content !== undefined) return stringifyAnthropicContent(content.content);
    return JSON.stringify(content);
  }
  return String(content);
}

function stringifyAnthropicBlock(block) {
  if (block === undefined || block === null) return '';
  if (typeof block === 'string') return block;
  if (block.type === 'text') return block.text || '';
  if (block.type === 'tool_result') {
    const body = stringifyAnthropicContent(block.content);
    const status = block.is_error ? ' error' : '';
    return `[tool_result ${block.tool_use_id || ''}${status}]\n${body}\n[/tool_result]`;
  }
  if (block.type === 'tool_use') {
    return `[assistant_tool_use ${block.name || ''}]\n${JSON.stringify(block.input || {})}\n[/assistant_tool_use]`;
  }
  if (typeof block.text === 'string') return block.text;
  if (block.content !== undefined) return stringifyAnthropicContent(block.content);
  return JSON.stringify(block);
}

function normalizeAnthropicMessages(rawMsgs, system) {
  const messages = [];
  const systemText = stringifyAnthropicContent(system);
  if (systemText) messages.push({ role: 'system', content: systemText });
  for (const message of rawMsgs) {
    messages.push({
      role: message.role,
      content: stringifyAnthropicContent(message.content),
    });
  }
  return messages;
}

async function dispatchTask(res, format, requestedModel, messages, newChat, tools, req) {
  const routeResult = resolveRoute(configStore, requestedModel);
  const provider = routeResult.provider;
  if (!provider || provider.enabled === false) {
    return res.status(503).json({ error: { code: 'NO_PROVIDER', message: 'No enabled provider matched this model' } });
  }

  const task_id = uuidv4();
  const hasTools = tools && tools.length > 0;
  const finalMessages = injectToolPrompt(messages, tools);
  const explicitNewChat = typeof newChat === 'boolean' ? newChat : undefined;
  const forcedNewChat = routeResult.route?.newChat === true || provider.newChat === true;
  const shouldNewChat = explicitNewChat ?? (forcedNewChat || looksLikeFreshConversation(messages));
  const task = {
    res,
    format,
    task_id,
    model: requestedModel,
    providerId: provider.id,
    hasTools,
    tools,
    buffer: '',
    inputTokens: estimateInputTokens(finalMessages, tools),
  };
  const startedAt = Date.now();

  prepareSse(res);

  try {
    await playwrightDriver.runTask(provider, {
      messages: finalMessages,
      newChat: shouldNewChat,
      onChunk: (delta) => {
        if (hasTools) task.buffer += delta;
        else writeChunk(task, delta);
      },
    });

    if (hasTools) finishTaskWithToolParsing(task);
    else finishTask(task);
    taskLog.append({
      task_id,
      status: 'ok',
      model: requestedModel,
      providerId: provider.id,
      routeId: routeResult.route?.id || null,
      keyId: req.apiKey?.id || null,
      elapsedMs: Date.now() - startedAt,
    });
  } catch (error) {
    writeError(task, error.code || 'WEBAI_ERROR', error.message);
    taskLog.append({
      task_id,
      status: 'error',
      code: error.code || 'WEBAI_ERROR',
      message: error.message,
      model: requestedModel,
      providerId: provider.id,
      routeId: routeResult.route?.id || null,
      keyId: req.apiKey?.id || null,
      elapsedMs: Date.now() - startedAt,
    });
  }
}

app.use('/admin/api', createAdminRouter({
  configStore,
  keyStore,
  taskLog,
  playwrightDriver,
}));

app.use('/admin', express.static(resolve(PUBLIC_DIR, 'admin')));
app.get('/admin', (_req, res) => {
  res.sendFile(resolve(PUBLIC_DIR, 'admin', 'index.html'));
});

app.use('/v1', authMiddleware);

app.post('/v1/chat/completions', (req, res) => {
  const { model = 'gpt-4', messages = [], tools } = req.body;
  const newChat = req.body.new_chat;
  dispatchTask(res, 'openai', model, messages, newChat, filterTools(tools), req);
});

app.post('/v1/messages', (req, res) => {
  const { model = 'claude', messages: rawMsgs = [], tools, system } = req.body;
  const newChat = req.body.new_chat;
  const messages = normalizeAnthropicMessages(rawMsgs, system);
  dispatchTask(res, 'anthropic', model, messages, newChat, filterTools(tools), req);
});

app.get('/health', (_req, res) => {
  const providers = configStore.listProviders();
  res.json({
    status: 'ok',
    providers: providers.length,
    sessions: playwrightDriver.listStatuses(providers),
  });
});

const server = app.listen(PORT, () => {
  console.log(`[Server] Running on http://localhost:${PORT}`);
  console.log(`[Server] Admin UI on http://localhost:${PORT}/admin`);
});

async function shutdown() {
  console.log('\n[Server] Shutting down...');
  await playwrightDriver.shutdown();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
