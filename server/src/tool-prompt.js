const ALLOWED_TOOLS = new Set(['bash', 'read', 'write', 'edit', 'glob', 'grep']);

export function filterTools(tools) {
  if (!tools) return null;
  const filtered = tools.filter(tool => {
    const name = (tool.function?.name || tool.name || '').toLowerCase();
    return ALLOWED_TOOLS.has(name);
  });
  return filtered.length ? filtered : null;
}

function extractCwd(messages) {
  for (const message of messages) {
    const text = typeof message.content === 'string' ? message.content : '';
    const match = text.match(/Working directory:\s*(.+)/i) || text.match(/current directory[:\s]+(.+)/i);
    if (match) return match[1].trim().replace(/\\/g, '\\\\');
  }
  return null;
}

function buildToolSystemPrompt(tools, cwd) {
  const defs = tools.map(tool => {
    const fn = tool.function || tool;
    const params = Object.keys(fn.parameters?.properties || {}).join(', ');
    return `- ${fn.name}: ${(fn.description || '').slice(0, 120)}${params ? `\n  Params: ${params}` : ''}`;
  }).join('\n');

  return `You are a coding agent. ${cwd ? `Working directory: ${cwd}` : ''}

Rules:
- NEVER guess file content. Always read a file before editing it.
- Use absolute paths based on the working directory. Do NOT invent paths.
- Only make changes directly requested. Do not add unnecessary code, comments, or refactoring.
- When editing, preserve existing indentation and style.
- Do NOT use built-in tools (Python, code interpreter, etc).
- Each tool call must be a separate XML block. Multiple calls = multiple blocks.
- Respond concisely. Explain what you did briefly after using tools.
- Prefer Glob, Grep, and Read for project analysis. Avoid Bash unless the user explicitly asks to run a command.
- Read uses file_path, not path: {"name":"Read","arguments":{"file_path":"C:\\\\path\\\\file.js"}}
- Glob uses only pattern and path. Do not use ignore, exclude, or glob_ignore.
- Grep uses pattern and path; add glob only when needed.
- Bash uses command; on Windows prefer PowerShell commands and single quotes around paths.

Tools available:
${defs}

ONLY use this XML format to call tools:
<tool_call>
{"name": "TOOL_NAME", "arguments": {"param": "value"}}
</tool_call>

Backslashes in strings MUST be doubled: C:\\\\Users not C:\\Users`;
}

export function injectToolPrompt(messages, tools) {
  const hasTools = tools && tools.length > 0;
  const isToolResultRound = messages.some(message => {
    const content = typeof message.content === 'string' ? message.content : '';
    return message.role === 'tool' || message.role === 'tool_result' || content.includes('[tool_result');
  });
  if (!hasTools || isToolResultRound) return messages;

  const next = [...messages];
  const lastUserIndex = next.findLastIndex(message => message.role === 'user');
  if (lastUserIndex >= 0) {
    next[lastUserIndex] = {
      ...next[lastUserIndex],
      content: `${next[lastUserIndex].content}\n\n${buildToolSystemPrompt(tools, extractCwd(messages))}`,
    };
  }
  return next;
}
