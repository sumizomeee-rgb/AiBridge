import { v4 as uuidv4 } from 'uuid';

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function estimateTokens(text) {
  const value = String(text || '');
  if (!value) return 0;
  return Math.max(1, Math.ceil(value.length / 4));
}

function writeAnthropicEvent(res, event, payload) {
  res.write(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
}

function startAnthropicMessage(task) {
  if (task.anthropicStarted) return;
  const { res, task_id, model } = task;
  task.anthropicStarted = true;
  task.anthropicBlockIndex = 0;
  task.anthropicOutputText = task.anthropicOutputText || '';
  writeAnthropicEvent(res, 'message_start', {
    type: 'message_start',
    message: {
      id: `msg_${task_id.replace(/-/g, '')}`,
      type: 'message',
      role: 'assistant',
      model,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: {
        input_tokens: Number(task.inputTokens || 0),
        output_tokens: 0,
      },
    },
  });
}

function startAnthropicTextBlock(task) {
  startAnthropicMessage(task);
  if (task.anthropicTextBlockOpen) return;
  const index = Number(task.anthropicBlockIndex || 0);
  task.anthropicTextBlockOpen = true;
  task.anthropicActiveTextIndex = index;
  writeAnthropicEvent(task.res, 'content_block_start', {
    type: 'content_block_start',
    index,
    content_block: { type: 'text', text: '' },
  });
}

function stopAnthropicTextBlock(task) {
  if (!task.anthropicTextBlockOpen) return;
  writeAnthropicEvent(task.res, 'content_block_stop', {
    type: 'content_block_stop',
    index: Number(task.anthropicActiveTextIndex || 0),
  });
  task.anthropicTextBlockOpen = false;
  task.anthropicBlockIndex = Number(task.anthropicBlockIndex || 0) + 1;
}

function writeAnthropicText(task, text) {
  if (!text) return;
  startAnthropicTextBlock(task);
  task.anthropicOutputText = `${task.anthropicOutputText || ''}${text}`;
  writeAnthropicEvent(task.res, 'content_block_delta', {
    type: 'content_block_delta',
    index: Number(task.anthropicActiveTextIndex || 0),
    delta: { type: 'text_delta', text },
  });
}

function writeAnthropicToolUse(task, toolCall) {
  stopAnthropicTextBlock(task);
  startAnthropicMessage(task);
  const index = Number(task.anthropicBlockIndex || 0);
  const name = toolCall.function.name;
  const input = toolCall.function.arguments || '{}';
  writeAnthropicEvent(task.res, 'content_block_start', {
    type: 'content_block_start',
    index,
    content_block: {
      type: 'tool_use',
      id: `toolu_${uuidv4().replace(/-/g, '').slice(0, 24)}`,
      name,
      input: {},
    },
  });
  writeAnthropicEvent(task.res, 'content_block_delta', {
    type: 'content_block_delta',
    index,
    delta: { type: 'input_json_delta', partial_json: input },
  });
  writeAnthropicEvent(task.res, 'content_block_stop', {
    type: 'content_block_stop',
    index,
  });
  task.anthropicBlockIndex = index + 1;
}

function finishAnthropicMessage(task, stopReason = 'end_turn') {
  startAnthropicMessage(task);
  stopAnthropicTextBlock(task);
  writeAnthropicEvent(task.res, 'message_delta', {
    type: 'message_delta',
    delta: { stop_reason: stopReason, stop_sequence: null },
    usage: { output_tokens: estimateTokens(task.anthropicOutputText || task.buffer || '') },
  });
  writeAnthropicEvent(task.res, 'message_stop', { type: 'message_stop' });
  task.res.end();
}

export function writeChunk(task, delta) {
  const { res, format, task_id, model } = task;
  if (format === 'anthropic') {
    writeAnthropicText(task, delta);
    return;
  }

  res.write(`data: ${JSON.stringify({
    id: `chatcmpl-${task_id}`,
    object: 'chat.completion.chunk',
    created: nowSeconds(),
    model,
    choices: [{ index: 0, delta: { content: delta }, finish_reason: null }],
  })}\n\n`);
}

export function finishTask(task) {
  const { res, format, task_id, model } = task;
  if (format === 'anthropic') {
    finishAnthropicMessage(task);
  } else {
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`,
      object: 'chat.completion.chunk',
      created: nowSeconds(),
      model,
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
    })}\n\n`);
    res.write('data: [DONE]\n\n');
    res.end();
  }
}

function repairJson(str) {
  try {
    return JSON.parse(str);
  } catch {}
  try {
    return JSON.parse(str.replace(/\\/g, '\\\\'));
  } catch {}
  try {
    const nameMatch = str.match(/"name"\s*:\s*"([^"]+)"/);
    const argsMatch = str.match(/"arguments"\s*:\s*(\{[\s\S]*\})\s*$/);
    if (nameMatch) {
      let args = {};
      if (argsMatch) {
        try {
          args = JSON.parse(argsMatch[1].replace(/\\/g, '\\\\'));
        } catch {}
      }
      return { name: nameMatch[1], arguments: args };
    }
  } catch {}
  return null;
}

function decodeXmlText(value) {
  return String(value || '')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .trim();
}

export function parseToolCalls(text) {
  return parseAllowedToolCalls(text, null);
}

function getAllowedToolNameMap(tools) {
  const map = new Map();
  for (const tool of tools || []) {
    const name = tool.function?.name || tool.name;
    if (name) map.set(String(name).toLowerCase(), String(name));
  }
  return map;
}

function stripToolCalls(text) {
  const value = String(text || '').replace(/<tool_call\b[\s\S]*?<\/tool_call>/g, '').trim();
  if (/^[A-Za-z_][A-Za-z0-9_]*\s*\([\s\S]*\)\s*$/.test(value)) return '';
  return value;
}

function normalizeToolCall(parsed, allowedToolNames) {
  if (!parsed?.name) return null;
  const lowerName = String(parsed.name).toLowerCase();
  if (lowerName === 'tool_name') return null;
  if (allowedToolNames.size && !allowedToolNames.has(lowerName)) return null;
  let args = parsed.arguments;
  if (typeof args === 'string') args = repairJson(args) || args;
  if (args && typeof args === 'object' && !Array.isArray(args)) {
    if (['read', 'write', 'edit'].includes(lowerName) && args.path && !args.file_path) {
      args.file_path = args.path;
      delete args.path;
    }
    if (lowerName === 'bash' && args.cmd && !args.command) {
      args.command = args.cmd;
      delete args.cmd;
    }
    if (lowerName === 'glob') {
      delete args.ignore;
      delete args.exclude;
    }
  }
  return {
    id: `call_${uuidv4().slice(0, 8)}`,
    type: 'function',
    function: {
      name: allowedToolNames.get(lowerName) || parsed.name,
      arguments: typeof args === 'string' ? args : JSON.stringify(args || {}),
    },
  };
}

function parseParameterToolCalls(text, allowedToolNames) {
  const calls = [];
  const regex = /<tool_call\s+[^>]*name=["']([^"']+)["'][^>]*>([\s\S]*?)<\/tool_call>/gi;
  let match;
  while ((match = regex.exec(text)) !== null) {
    const args = {};
    const paramRegex = /<parameter\s+[^>]*name=["']([^"']+)["'][^>]*>([\s\S]*?)<\/parameter>/gi;
    let paramMatch;
    while ((paramMatch = paramRegex.exec(match[2])) !== null) {
      args[paramMatch[1]] = decodeXmlText(paramMatch[2]);
    }
    const body = decodeXmlText(match[2]);
    const bodyJson = Object.keys(args).length ? null : repairJson(body);
    const normalized = normalizeToolCall({ name: match[1], arguments: bodyJson || args }, allowedToolNames);
    if (normalized) calls.push(normalized);
  }
  return calls;
}

function parseFunctionToolCalls(text, allowedToolNames) {
  const value = String(text || '').trim();
  const match = value.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*\(([\s\S]*)\)\s*$/);
  if (!match) return [];
  const parsedArgs = repairJson(match[2].trim());
  const normalized = normalizeToolCall({ name: match[1], arguments: parsedArgs || {} }, allowedToolNames);
  return normalized ? [normalized] : [];
}

function parseAllowedToolCalls(text, tools) {
  const allowedToolNames = getAllowedToolNameMap(tools);
  const regex = /<tool_call>([\s\S]*?)<\/tool_call>/g;
  const calls = [
    ...parseParameterToolCalls(text || '', allowedToolNames),
    ...parseFunctionToolCalls(text || '', allowedToolNames),
  ];
  let match;
  while ((match = regex.exec(text)) !== null) {
    const parsed = repairJson(match[1].trim());
    const normalized = normalizeToolCall(parsed, allowedToolNames);
    if (normalized) calls.push(normalized);
  }
  return calls;
}

export function finishTaskWithToolParsing(task) {
  const { res, task_id, model, buffer, format } = task;
  const toolCalls = parseAllowedToolCalls(buffer || '', task.tools);
  if (format === 'anthropic') {
    const text = stripToolCalls(buffer);
    if (text) writeAnthropicText(task, text);
    if (toolCalls.length > 0) {
      for (const toolCall of toolCalls) writeAnthropicToolUse(task, toolCall);
      finishAnthropicMessage(task, 'tool_use');
    } else {
      finishAnthropicMessage(task);
    }
    return;
  }

  if (toolCalls.length > 0) {
    for (let i = 0; i < toolCalls.length; i++) {
      const tc = toolCalls[i];
      res.write(`data: ${JSON.stringify({
        id: `chatcmpl-${task_id}`,
        object: 'chat.completion.chunk',
        created: nowSeconds(),
        model,
        choices: [{ index: 0, delta: { tool_calls: [{ index: i, ...tc }] }, finish_reason: null }],
      })}\n\n`);
    }
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`,
      object: 'chat.completion.chunk',
      created: nowSeconds(),
      model,
      choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
    })}\n\n`);
  } else {
    if (buffer) {
      writeChunk(task, buffer);
    }
    res.write(`data: ${JSON.stringify({
      id: `chatcmpl-${task_id}`,
      object: 'chat.completion.chunk',
      created: nowSeconds(),
      model,
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
    })}\n\n`);
  }
  res.write('data: [DONE]\n\n');
  res.end();
}

export function writeError(task, code, message) {
  const { res } = task;
  const status = code === 'RATE_LIMITED' ? 429 : 502;
  if (res.headersSent) {
    if (task.format === 'anthropic') {
      writeAnthropicText(task, `[AiBridge ${code}] ${message}`);
      finishAnthropicMessage(task);
      return;
    }
    res.write(`data: ${JSON.stringify({ error: { code, message } })}\n\n`);
    res.end();
  } else {
    res.status(status).json({ error: { code, message } });
  }
}
