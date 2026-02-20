# AiBridge

Browser extension + local server that bridges CLI tools (like opencode) to web AI platforms (Doubao, Qwen, Yuanbao, Kimi, MiMo).

## Architecture

```
opencode/CLI  →  Local Server (:9529)  →  WebSocket  →  Chrome Extension  →  Web AI Page
                  (OpenAI-compatible API)                (content script)     (DOM injection)
```

## Setup

### 1. Install dependencies

```bash
cd server && npm install
cd extension && npm install
```

### 2. Build extension

```bash
cd extension && npm run build
```

### 3. Load extension

- Open `chrome://extensions`
- Enable Developer Mode
- Click "Load unpacked" → select `extension/dist`

### 4. Start server

Double-click `server/start.bat` or:

```bash
cd server && node src/index.js
```

### 5. Open a supported AI platform

Navigate to any supported platform in Chrome. The extension will auto-connect.

## Supported Platforms

| Platform | URL | Status |
|----------|-----|--------|
| Doubao | https://www.doubao.com | Fully working |
| Qwen | https://chat.qwen.ai | Working (minor formatting issues) |
| Yuanbao | https://yuanbao.tencent.com | Working |
| Kimi | https://www.kimi.com | Limited (prefers built-in tools) |
| MiMo | https://aistudio.xiaomimimo.com | Working (verbose output) |

## Configuration

- Server config: `server/config.json`
- API port: default `9529`
- Supports both OpenAI (`/v1/chat/completions`) and Anthropic (`/v1/messages`) formats

## Tool Calling

The server injects tool definitions into user messages. Web AI responds with `<tool_call>` XML which gets parsed and forwarded as standard OpenAI tool_calls format.

Allowed tools: `bash`, `read`, `write`, `edit`, `glob`, `grep`
