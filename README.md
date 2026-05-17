# AiBridge

Local server that bridges CLI tools (like opencode) to web AI platforms through OpenAI/Anthropic-compatible APIs.

## Architecture

```
opencode/CLI  →  Local Server (:9529)  →  Playwright Session  →  Web AI Page
                  (OpenAI-compatible API)   (headless/profile)     (DOM automation)
```

The runtime is server-only: Playwright manages persistent browser profiles for each provider, and the old Chrome extension path has been removed.

## Setup

### 1. Start server

Double-click `server/start.bat`.

The script will install npm dependencies only when needed, reuse local Google Chrome first, then reuse an existing Playwright Chromium, and only download Playwright Chromium when no usable browser is found.

You can also start it manually:

```bash
cd server && node src/index.js
```

### 2. Open Admin

Open:

```text
http://localhost:9529/admin
```

In Admin you can:

- create and disable local API keys
- edit provider URLs and session IDs
- configure model routing
- open a visible login window for each provider
- monitor provider reachability and recent task failures

Profiles are stored under `server/profiles/`. Delete a provider profile or click "Reset Profile" in Admin to force a fresh login.

## Supported Platforms

| Platform | URL | Status |
|----------|-----|--------|
| ChatGPT | https://chatgpt.com | Configured |
| Doubao | https://www.doubao.com | Configured |
| Claude | https://claude.ai | Configured |
| Qwen | https://chat.qwen.ai | Configured |
| Yuanbao | https://yuanbao.tencent.com | Configured |
| Kimi | https://www.kimi.com | Configured |
| MiMo | https://aistudio.xiaomimimo.com | Configured |
| DeepSeek | https://chat.deepseek.com | Configured |

## Configuration

- Runtime config: `server/data/config.json`
- Legacy config: `server/config.json`
- API port: default `9529`
- Admin UI: `http://localhost:9529/admin`
- Supports both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats

## Tool Calling

The server injects tool definitions into user messages. Web AI responds with `<tool_call>` XML which gets parsed and forwarded as standard OpenAI tool_calls format.

Allowed tools: `bash`, `read`, `write`, `edit`, `glob`, `grep`
