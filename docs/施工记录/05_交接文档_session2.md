# 施工交接文档 (2026-02-21 Session 2)

## 一、当前状态总览

| 组件 | 状态 | 说明 |
|------|------|------|
| Server (`server/src/index.js`) | ✅ 可用 | 已加 debug 日志，SSE 格式已修复 |
| Extension 源码 | ✅ 已修复 | new_chat 导航 bug 已修，需重新 build |
| Extension dist | ⚠️ 需重新构建 | 源码改了但最后一次 build 是修复前的 |
| Chrome CDP 调试 | ❌ 不可用 | Chrome 145 下 CDP 端口无法打开，已放弃 |
| 端到端测试 (curl) | ✅ 首次成功 | 第二次因 bug 卡死，修复后未验证 |
| opencode 集成 | ❌ 未验证 | SSE 格式已修复但还没成功测过 |

## 二、本次 Session 修复的关键 Bug

### Bug 1: new_chat 导航导致 content script 自毁（最重要）

**现象**: 第一次 curl 请求成功，后续所有请求永久超时。

**根因**: `GenericAdapter.newChat()` 执行 `location.href = url` 导航页面，content script 被卸载，任务丢失。background 的 `busyTabs` 永远不会被清除，后续任务全部排队卡死。

**修复**:
1. `extension/src/background/index.ts` — `handleTask()` 中由 background 负责导航：
   - 检测 `task.new_chat`，用 `chrome.tabs.update` 导航
   - 等待页面加载完成 + 1.5s 延迟
   - 将 `new_chat` 设为 false 后再发给 content script
2. `extension/src/content/adapters/generic.ts` — `newChat()` 改为空方法
3. `extension/src/background/index.ts` — `executeTask()` 加了 60s 超时保护

### Bug 2: SSE 格式不完整（已修复并验证）

**现象**: opencode 卡在 "build · Ency"。

**根因**: SSE 输出缺少 `id/object/created/model/finish_reason` 字段，AI SDK 无法解析。

**修复**: `server/src/index.js` 的 `writeChunk()` 和 `finishTask()` 输出完整 OpenAI 格式。

### Bug 3: 模型路由错误（已修复并验证）

**现象**: curl 超时。

**根因**: `config.json` 中 `"Ency": "gpt"` 路由到 ChatGPT 而非豆包。

**修复**: 改为 `"Ency": "doubao"`。

## 三、接手后立即要做的事

### 步骤 1: 重新构建扩展
```bash
cd G:\SuchProject\Other\AiBridge\extension
npm run build
```
源码已修复但 dist 还是旧的，必须先 build。

### 步骤 2: 在 Chrome 中重新加载扩展
1. 打开 `chrome://extensions`
2. 找到 WebAI-Bridge 扩展，点刷新按钮
3. 刷新豆包页面（确保 content script 注入）

### 步骤 3: 启动服务器
```bash
cd G:\SuchProject\Other\AiBridge\server
node src/index.js
```
服务器日志会直接输出到终端，方便观察。

### 步骤 4: 验证测试
```bash
# 健康检查
curl http://localhost:9529/health

# 端到端测试（应返回豆包回复的 SSE 流）
curl -N http://localhost:9529/v1/chat/completions \
  -H "Authorization: Bearer sk-webai-bridge-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"Ency","stream":true,"messages":[{"role":"user","content":"1+1等于几"}]}'

# 连续发第二次，验证 new_chat 修复是否生效
curl -N http://localhost:9529/v1/chat/completions \
  -H "Authorization: Bearer sk-webai-bridge-local" \
  -H "Content-Type: application/json" \
  -d '{"model":"Ency","stream":true,"messages":[{"role":"user","content":"今天星期几"}]}'

# opencode 测试
opencode run -m ency/Ency "今天星期几"
```

## 四、关键文件清单与当前状态

### 服务器端
| 文件 | 状态 | 说明 |
|------|------|------|
| `server/src/index.js` | ✅ 已修改 | 加了 debug 日志 + 完整 SSE 格式 |
| `server/config.json` | ✅ 已修改 | `"Ency": "doubao"` |
| `server/package.json` | 无变化 | `"type": "module"` |

### 扩展端
| 文件 | 状态 | 说明 |
|------|------|------|
| `extension/src/background/index.ts` | ✅ 已修改 | new_chat 导航 + 超时保护 |
| `extension/src/content/index.ts` | 无变化 | |
| `extension/src/content/adapters/generic.ts` | ✅ 已修改 | newChat() 改为空方法 |
| `extension/src/content/adapters/chatgpt.ts` | 无变化 | |
| `extension/src/content/adapters/base.ts` | 无变化 | |
| `extension/src/shared/types.ts` | 无变化 | MODEL_ROUTES 定义 |
| `extension/dist/` | ⚠️ 过期 | 需要重新 `npm run build` |

### 配置文件（用户侧）
| 文件 | 说明 |
|------|------|
| `C:\Users\sumizome\.config\opencode\opencode.json` | opencode 的 ency provider 配置 |

## 五、已知问题与注意事项

### Chrome CDP 调试不可用
Chrome 145 在 Windows 上即使传了 `--remote-debugging-port=9222` 也不开放端口。之前 Session 1 能用是因为配合了 `--disable-background-mode` 且确保杀干净所有 Chrome 进程后首次启动。本次 Session 反复尝试均失败，原因不明（可能是 Chrome 更新或系统策略变化）。

**替代方案**: 依赖服务器日志 (`server/server.log`) 和扩展 Service Worker 控制台 (`chrome://extensions` → 检查视图) 来调试。

### GenericAdapter.observeResponse 的潜在问题
`getLastReply()` 用 CSS 选择器匹配回复容器：
```
[class*="markdown"], [class*="message-content"], [class*="response"],
[class*="answer"], [class*="assistant"], [class*="bot"]
```
如果豆包改版 DOM 结构，可能匹配不到。第一次成功测试时只返回了一个 chunk（整段回复），说明轮询间隔 500ms 可能太长，或者豆包渲染速度很快导致只捕获到最终文本。

### new_chat 导航的边界情况
当前逻辑：如果 tab URL 不等于 baseUrl 就导航。但豆包的新对话 URL 是 `https://www.doubao.com/chat/`，已有对话是 `https://www.doubao.com/chat/xxxxx`。如果用户手动在豆包页面操作过，URL 可能不符合预期。

### 服务器 debug 日志
`server/src/index.js` 中加了 `console.log` 调试日志（`[Dispatch]` 和 `[ExtMsg]` 前缀），验证完毕后可以删除。

## 六、待完成任务

1. **重新 build 扩展并验证 new_chat 修复** — 最高优先级
2. **验证 opencode 集成** — `opencode run -m ency/Ency "今天星期几"`
3. **连续多次请求测试** — 确认 busyTabs 超时保护正常工作
4. **测试其他平台** — ChatGPT、Claude、通义千问（尚未尝试）
5. **清理 extension/ 目录下的 CDP 调试脚本** — `*.cjs` 文件和 `*.bat` 文件
6. **清理服务器 debug 日志** — 稳定后移除 console.log

## 七、项目启动命令速查

```bash
# 启动服务器
cd G:\SuchProject\Other\AiBridge\server && node src/index.js

# 构建扩展
cd G:\SuchProject\Other\AiBridge\extension && npm run build

# curl 测试
curl -N http://localhost:9529/v1/chat/completions -H "Authorization: Bearer sk-webai-bridge-local" -H "Content-Type: application/json" -d "{\"model\":\"Ency\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"

# opencode 测试
opencode run -m ency/Ency "今天星期几"

# 健康检查
curl http://localhost:9529/health
```
