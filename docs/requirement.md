# WebAI-Bridge (Manifest V3) - 主开发蓝图与架构设计文档

> 历史文档：本文件记录的是早期 Chrome Extension + WebSocket 方案。当前实现已切换为 Server-only + Playwright，扩展目录和运行时 WebSocket fallback 已删除；新的实施规格见 `docs/headless-admin-spec.md`。

## 1. 项目概述与核心定位
**WebAI-Bridge** 是一款基于 Chrome Manifest V3 的开发者工具集，由“本地 Node.js 网关服务”与“浏览器插件”两部分组成。
它的核心目标是：将主流网页端 AI（如 ChatGPT, 豆包, Claude, 通义千问）无缝封装为本地标准的 OpenAI (`/v1/chat/completions`) 和 Anthropic 兼容接口，供外部 Agent（如 Dify, NextChat, Open-Interpreter）零成本、自动化调用。

**核心特性：**
- **双向长连接：** 插件主动通过 WebSocket 连接本地网关，无需修改系统注册表，支持跨域免流。
- **UI 路由控制：** 插件自带可视化控制台（Popup UI），支持“智能匹配（Auto）”与“强制接管（Override）”双模路由。
- **全自动化：** 自动处理“New Chat”新建会话、自动模拟人类键入、实时抓取流式（Streaming）响应。

---

## 2. 技术栈与运行环境

- **运行系统：** Windows 10/11
- **本地网关 (Local Server)：** Node.js (v20+), `express`, `ws`, `cors`。最终通过 `pkg` 打包为独立 `.exe`。
- **Chrome 插件 (Extension)：** Chrome Manifest V3, 推荐使用 `Vite` + `@crxjs/vite-plugin` 构建，使用 TypeScript 编写核心逻辑以保证 DOM 操作的类型安全。

---

## 3. 系统架构与数据流向

整个系统采用“中继模式 (Relay Mode)”。



### 3.1 核心数据流：
1. **Agent 客户端**发起 HTTP POST 请求到 `http://localhost:9529/v1/chat/completions`。
2. **Local Server** 接收请求，生成唯一 `task_id`，将 HTTP 请求挂起（Pending），并将数据转换为内部 JSON 格式。
3. **Local Server** 通过 WebSocket 将任务下发给已连接的 **Chrome Extension (Background Script)**。
4. **Background Script** 根据路由策略（Auto/Override）找到对应的浏览器 Tab。
5. **Background Script** 通过 `chrome.tabs.sendMessage` 将任务派发给注入在页面的 **Content Script**。
6. **Content Script** 执行 DOM 操作（点击新对话 -> 填入文本 -> 点击发送 -> 监听新生成的 DOM 节点）。
7. **Content Script** 提取到新的增量文本（Chunk），原路返回：Content Script -> Background -> WebSocket -> Local Server。
8. **Local Server** 将收到的 Chunk 通过 Server-Sent Events (SSE) 协议 `res.write()` 推送回 **Agent 客户端**。

---

## 4. 模块详细设计与实现逻辑

### 4.1 模块一：Local Server (本地网关)
**职责：** 协议伪装、WebSocket 维护、流式转发。
- **端口配置：** HTTP 服务和 WebSocket 服务均监听 `9529` 端口。
- **跨域处理：** 必须设置全局 CORS 允许所有来源 `Access-Control-Allow-Origin: *`，否则 Web 端 Agent 无法调用。
- **请求暂存机制：** 维护一个 Map：`activeTasks = new Map<String(taskId), ResponseObject>()`。WebSocket 收到插件返回的数据时，通过 taskId 找到对应的 HTTP `res` 对象进行输出。

### 4.2 模块二：Extension Background (插件大脑)
**职责：** WebSocket 客户端、保活机制、路由调度。
- **长连接与保活：** Manifest V3 的 Service Worker 会休眠。必须在 `WebSocket.onclose` 中实现断线重连（如 `setInterval` 5秒重连）。必要时使用 Offscreen API 保持常驻。
- **路由逻辑 (核心)：**
  读取 `chrome.storage.local` 中的 UI 配置。
  - *如果 mode === 'AUTO'*：解析任务的 `model` 字段（如 `gpt-4` 找 chatgpt.com，`doubao` 找 doubao.com）。
  - *如果 mode === 'OVERRIDE'*：忽略任务的 `model`，强制使用 UI 配置的 `target_ai`。
- **Tab 管理：** 收到任务后，使用 `chrome.tabs.query` 查找目标网站。如果存在则复用（并 `chrome.tabs.update` 激活），如果不存在则 `chrome.tabs.create` 新建。

### 4.3 模块三：Extension Content Scripts (DOM 执行器)
**职责：** 模拟真实用户交互，实时抓取文本。
- **New Chat 逻辑：**
  如果收到的任务 JSON 中 `conversation_id` 为空或标识为新会话：
  1. 查找并点击页面上的“新建对话/New Chat”按钮（需预先配置各平台的 CSS 选择器）。
  2. 等待 URL 变化或输入框清空后，再执行后续操作。
- **模拟输入与发送：**
  *防坑警告：* 对于 React/Vue 构建的单页应用，直接修改 `textarea.value` 无效。必须触发原生事件：
  ```javascript
  const inputEl = document.querySelector('textarea');
  inputEl.value = '你好';
  inputEl.dispatchEvent(new Event('input', { bubbles: true }));
  // 模拟回车或点击发送按钮
  流式监听 (MutationObserver)：
监听包含 AI 回复的容器 div。每次 DOM 变化时，读取最新的一条气泡的 .innerText。保存一个 lastLength 变量，每次截取 text.substring(lastLength) 作为增量 Chunk 发送回 Background。

4.4 模块四：Extension Popup UI (控制台)
职责： 状态展示、路由模式切换、快速页面导航。

状态区： 实时显示 WebSocket 连接状态（绿灯/红灯）。

控制区：

<select id="routing-mode">：选项为 Auto (智能匹配) / Override (强制接管)。

<select id="override-target">：选项为 ChatGPT / 豆包 / Claude 等（仅在 Override 模式下生效）。

配置变更时，立刻写入 chrome.storage.local 并通过 chrome.runtime.sendMessage 通知 Background。

导航区： 列出支持的 AI 列表。点击时调用 chrome.tabs.query 和 chrome.tabs.update 快速跳转或新开对应 AI 页面。

5. Agent 开发投喂指南 (分阶段 Prompt 建议)
为了保证代码生成质量，请要求 Agent 按以下阶段逐步实现，不要一次性生成所有代码：

Phase 1: Local Server 构建。
让 Agent 使用 Express 和 ws 库，实现 HTTP 接收 OpenAI 格式请求，转为 WebSocket 消息，并处理 SSE 流式返回的骨架代码。

Phase 2: Extension 骨架与通信。
让 Agent 编写 Manifest V3 配置、Background.js 的 WebSocket 客户端连接逻辑（包含断线重连），以及简单的 Popup UI 界面。

Phase 3: 路由控制与 Tab 管理。
让 Agent 实现 Popup UI 和 Background 之间的数据同步，以及根据模型名称（或强制路由配置）查找、新建、激活对应 URL 标签页的逻辑。

Phase 4: Content Script 核心适配器。
让 Agent 以具体网站（如 ChatGPT 或 Doubao）为例，编写 Content Script 类。重点实现：点击 New Chat 按钮、触发 React 兼容的 input 事件、使用 MutationObserver 提取增量文本的逻辑。
