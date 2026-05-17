# AiBridge 无头会话与本地后台改造规格

## 1. 结论

当前项目能跑，但运行方式依赖 Chrome 扩展、可见标签页和手动切换，使用成本偏高。推荐改造方向是：以 `server` 为唯一常驻入口，内置 Playwright 管理网页 AI 会话，默认使用无头浏览器；当平台登录、风控、验证码或调试需要人工介入时，在后台一键打开可见浏览器窗口刷新 session。

换句话说，目标不是完全消灭浏览器进程，而是消灭“用户必须一直开着浏览器和扩展”的心智负担。网页端 AI 本质仍需要浏览器运行环境，最稳妥的做法是把浏览器进程托管到本地 Server 后台，并让 session 可管理、可更新、可监控。

## 2. 目标

- 只启动本地 Server 即可对外提供 OpenAI 和 Anthropic 兼容接口。
- 支持后台管理 API key、平台 URL、模型路由、会话状态、可用性监控和运行日志。
- 默认无头运行网页 AI，会话通过本地浏览器 profile 持久化。
- 支持在后台手动刷新登录态：临时打开可见窗口，登录完成后关闭，后续继续无头使用。
- Chrome Extension 链路已收口删除，运行时只保留 Server + Playwright。

## 3. 非目标

- 不绕过平台登录、验证码、风控或付费限制。
- 不承诺所有网页 AI 都能长期稳定无头运行。部分平台可能需要周期性可见窗口刷新 session。
- 不在第一阶段实现多用户远程后台。本项目后台默认只面向本机使用。
- 不改外部客户端调用习惯，`/v1/chat/completions` 和 `/v1/messages` 应保持兼容。

## 4. 新架构

```text
CLI / Agent / OpenAI-compatible Client
        |
        v
Local Server :9529
        |
        +-- Admin Web UI
        +-- API Key Manager
        +-- Provider Router
        +-- Health Monitor
        |
        v
Browser Session Manager
        |
        +-- Playwright Persistent Context: doubao
        +-- Playwright Persistent Context: qwen
        +-- Playwright Persistent Context: yuanbao
        +-- Playwright Persistent Context: kimi
        +-- Playwright Persistent Context: deepseek
```

核心变化：

- 页面操作逻辑统一放在 `server/src/adapters/*`。
- 平台映射迁移为 Server 配置，可在 Admin 修改。
- `server/src/index.js` 直接派发给本地 `ProviderWorker`。
- 每个平台使用独立 `userDataDir` 保存 cookie、localStorage 和登录态。

## 5. 运行模式

### 5.1 默认无头模式

Server 启动后按需拉起对应平台的 Playwright context：

- 首次请求某平台时创建 worker。
- worker 复用同一页面或上下文处理串行任务。
- 空闲超过配置时间后可关闭页面，但保留 profile。
- 后台显示 worker 状态、最后使用时间、失败原因。

### 5.2 可见登录模式

当后台检测到未登录、验证码、风控页、输入框不可用时：

1. 后台将平台标记为 `needs_login` 或 `blocked`。
2. 用户点击“打开会话窗口”。
3. Server 用同一个 `userDataDir` 打开可见浏览器窗口。
4. 用户完成登录或验证。
5. 后台检测输入框可用后提示“会话已恢复”。
6. 用户关闭窗口，后续继续无头运行。

### 5.3 手动可见模式

当某个平台不适合长期无头运行时，Provider 可切到 `always_visible`，或者通过“打开会话窗口”临时进入可见窗口。这个模式仍由 Server 托管 profile，不再依赖 Chrome Extension。

## 6. 后台信息架构

后台入口建议为 `http://localhost:9529/admin`。

### 6.1 总览页

用途：启动后第一眼判断系统能不能用。

主要元素：

- 顶部状态条：Server 状态、监听端口、当前版本、全局 API 认证状态。
- 平台状态表：平台、URL、登录态、可达性、最近请求、平均耗时、连续失败次数。
- 快捷操作：检测全部、打开日志、创建 API key、添加平台。
- 最近任务列表：时间、调用方 key、模型、路由平台、耗时、结果。

状态颜色建议：

- 绿色：可用。
- 黄色：需要登录、检测超时、近期有失败。
- 红色：不可达、DOM 失效、被限制、启动失败。
- 灰色：停用。

### 6.2 平台管理页

字段：

- 平台 ID：如 `doubao`、`qwen`。
- 显示名称：如 `豆包`。
- 入口 URL：可在后台直接修改。
- URL 匹配规则：用于判断当前页面是否在正确站点。
- 适配器：`generic`、`chatgpt` 或后续专用适配器。
- 浏览器模式：`headless`、`visible_on_demand`、`always_visible`。
- 是否启用。
- 健康检查 prompt。
- 单任务超时时间。
- 空闲回收时间。

交互：

- “测试连接”：只打开页面并检测基础 DOM。
- “发送测试消息”：发健康检查 prompt，验证完整问答链路。
- “打开会话窗口”：打开可见浏览器窗口，完成登录、模型选择或模式勾选后保存到 profile。
- “复制诊断信息”：复制平台配置、最近错误和 worker 状态，方便排查。

### 6.3 模型路由页

用途：把外部请求中的 `model` 映射到平台。

字段：

- 匹配模式：精确匹配、包含匹配、正则匹配。
- 匹配值：如 `Ency`、`doubao`、`qwen`。
- 目标平台。
- 优先级。
- 是否强制新建会话，默认关闭以复用同一个网页上下文。
- 是否允许 fallback。

交互：

- 提供“试算路由”输入框：输入一个 model，实时显示会命中哪个平台。
- 支持拖拽调整优先级，或使用数字优先级。
- 保存前检测冲突：多个规则同时命中时提示实际生效顺序。

### 6.4 API Key 管理页

字段：

- 名称。
- key 前缀展示，完整 key 只在创建时显示一次。
- 权限范围：全部平台、指定平台、只读健康检查。
- 每分钟请求限制。
- 是否启用。
- 创建时间、最后使用时间、最后调用 IP。

交互：

- “新建 key”：生成后显示一次，后台仅保存 hash。
- “轮换 key”：创建新 key 并可选择停用旧 key。
- “禁用 key”：立即拒绝后续请求。
- “查看使用记录”：筛选该 key 的最近任务。

### 6.5 会话管理页

用途：管理每个平台的 browser profile。

字段：

- 平台。
- Profile 路径。
- 登录态状态。
- 最近刷新时间。
- 最近一次健康检查。
- Cookie 估计过期时间，若无法可靠判断则显示未知。

交互：

- “打开会话窗口”：用当前 profile 打开可见浏览器。
- “关闭会话窗口”：关闭可见窗口但保留 profile。
- “重置 session”：清空该平台 profile，需要二次确认。
- “导入/切换 profile”：允许配置新的 profile 目录。

### 6.6 日志与任务页

用途：把现在只能看命令行日志的问题搬进后台。

列表字段：

- task_id。
- 时间。
- API key 名称。
- model。
- 路由平台。
- 请求 token 或消息长度估算。
- 首包耗时。
- 总耗时。
- 状态：成功、超时、未登录、DOM 失败、平台限制。

详情抽屉：

- 请求摘要。
- 响应摘要。
- 错误堆栈。
- 页面诊断：URL、title、关键选择器检测结果。
- 操作按钮：重试、复制 curl、复制诊断信息。

## 7. 关键用户流程

### 7.1 首次配置平台

1. 用户打开 `/admin`。
2. 进入“平台管理”，点击“添加平台”。
3. 填写名称、URL、适配器、驱动和健康检查 prompt。
4. 点击“保存并测试”。
5. 如果未登录，系统提示打开会话窗口。
6. 用户登录后点击“我已完成登录”。
7. 系统自动执行健康检查，成功后平台变为可用。

### 7.2 新建本地 API key

1. 用户进入“API Key”。
2. 点击“新建 key”。
3. 输入名称和权限范围。
4. 系统生成 key，只展示一次。
5. 用户将 key 配到 opencode 或其他客户端。
6. 后台开始记录该 key 的使用情况。

### 7.3 修改模型路由

1. 用户进入“模型路由”。
2. 新增规则：`Ency` 指向 `doubao`。
3. 在试算框输入 `Ency`。
4. 页面显示命中 `doubao`，并显示是否会强制新建会话。
5. 用户保存。
6. 下一次 `/v1/chat/completions` 自动使用新路由。

### 7.4 平台不可用排查

1. 总览页显示某平台红色。
2. 用户点击平台进入详情。
3. 详情展示最近错误：如输入框不存在、页面跳转登录、请求超时。
4. 用户点击“打开会话窗口”或“发送测试消息”。
5. 成功后状态恢复绿色，失败则保留诊断信息。

## 8. Server API 草案

对外兼容接口保留：

- `POST /v1/chat/completions`
- `POST /v1/messages`
- `GET /health`

后台接口新增：

- `GET /admin`
- `GET /admin/api/overview`
- `GET /admin/api/providers`
- `POST /admin/api/providers`
- `PATCH /admin/api/providers/:id`
- `POST /admin/api/providers/:id/check`
- `POST /admin/api/providers/:id/open-session`
- `POST /admin/api/providers/:id/close-session`
- `POST /admin/api/providers/:id/reset-session`
- `GET /admin/api/routes`
- `POST /admin/api/routes`
- `PATCH /admin/api/routes/:id`
- `DELETE /admin/api/routes/:id`
- `GET /admin/api/keys`
- `POST /admin/api/keys`
- `PATCH /admin/api/keys/:id`
- `DELETE /admin/api/keys/:id`
- `GET /admin/api/tasks`
- `GET /admin/api/tasks/:id`

## 9. 配置与存储

第一阶段建议使用本地 JSON 文件，降低改造成本：

```text
server/data/config.json
server/data/keys.json
server/data/tasks.jsonl
server/profiles/doubao/
server/profiles/qwen/
```

后续如果任务日志变多，再迁移到 SQLite。

### 9.1 Provider 配置示例

```json
{
  "id": "doubao",
  "name": "豆包",
  "url": "https://www.doubao.com",
  "adapter": "generic",
  "driver": "playwright",
  "browserChannel": "",
  "browserMode": "headless",
  "enabled": true,
  "profileDir": "server/profiles/doubao",
  "healthPrompt": "请只回复 ok",
  "taskTimeoutMs": 90000,
  "idleTtlMs": 600000
}
```

### 9.2 Route 配置示例

```json
{
  "id": "route-ency",
  "type": "exact",
  "pattern": "Ency",
  "providerId": "doubao",
  "priority": 100,
  "newChat": false,
  "fallbackProviderIds": ["qwen"]
}
```

## 10. 适配器设计

建议定义统一接口：

```ts
export interface WebAiAdapter {
  detect(page: Page): Promise<ProviderDetection>;
  newChat(page: Page): Promise<void>;
  sendMessage(page: Page, text: string): Promise<void>;
  observeResponse(page: Page, callbacks: ResponseCallbacks): Promise<void>;
}
```

`generic` 适配器先复用现有策略：

- 查找 `textarea` 或 `[contenteditable="true"]`。
- 尝试点击发送按钮，否则回车发送。
- 通过候选回复容器抓取新增文本。
- 检测停止按钮消失或输入框可用来判断结束。

后续对高频平台逐步增加专用适配器，减少 DOM 变更带来的不稳定。

## 11. 健康检查模型

平台状态建议拆成四层：

- `network`: URL 可打开。
- `session`: 已登录且不在验证码或登录页。
- `dom`: 输入框、发送按钮、回复容器可检测。
- `completion`: 能完成一次测试问答。

后台展示最终状态时保留每层明细。例如 `completion` 失败但 `session` 成功，说明登录态没问题，可能是 DOM 选择器或平台响应问题。

## 12. 实施计划

### Phase 1：Server 内部 Provider 抽象

- 新增 provider、route、key 的配置加载模块。
- 将现有 `resolveModel` 改为可配置路由。
- 新增 Playwright ProviderWorker 统一执行入口。
- 为后续专用平台 adapter 留出统一接口。

### Phase 2：Playwright 无头 Driver

- 在 `server` 引入 Playwright。
- 实现持久化 profile。
- 迁移 `GenericAdapter` 的发送和监听逻辑。
- 支持单平台串行队列和任务超时。
- 支持登录态失败时返回明确错误。

### Phase 3：Admin MVP

- 新增 `/admin` 静态后台页面。
- 实现总览、平台管理、API key、模型路由四个核心页面。
- 实现健康检查和打开可见 session 窗口。

### Phase 4：监控与可维护性

- 任务日志持久化。
- 平台状态定时刷新。
- 失败诊断信息和复制诊断功能。
- 支持 fallback provider。

### Phase 5：收口 Server-only 模式

- 删除扩展目录、WebSocket fallback 和 `ws` 依赖。
- 文档更新为 Server-only 使用方式。
- README 改成“启动 server -> 打开 admin -> 配置平台 -> 调用 API”的流程。

## 13. 风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 平台禁止或限制无头浏览器 | 某些 AI 无法稳定响应 | 提供可见登录窗口、`always_visible` 模式和 profile 重置 |
| DOM 结构频繁变化 | 发送或抓取失败 | 健康检查分层、专用适配器、后台诊断信息 |
| session 过期 | 请求突然失败 | 定期检测、后台提示刷新、支持一键打开 session |
| 多任务并发冲突 | 同一页面回复串扰 | 每个平台 worker 串行队列，后续再做多 profile 并发 |
| key 明文泄露 | 本地 API 被滥用 | key 只保存 hash，后台仅创建时显示完整 key |

## 14. MVP 验收标准

- 启动 `server` 后，不需要安装或打开 Chrome 扩展即可调用一个已登录平台。
- 后台能新增和禁用 API key。
- 后台能修改平台 URL 和模型路由。
- 后台能显示每个平台的可用状态。
- 后台能打开可见窗口刷新至少一个平台的 session。
- `/v1/chat/completions` 对外调用方式保持不变。
- 出错时返回明确错误码，并能在后台看到最近失败原因。
