# AiBridge

AiBridge 是一个在本机运行、供局域网内 Agent 共用的 AI 网关。它把官网 Web 会话和标准 API 汇总到同一套模型名下，对外同时提供 OpenAI 与 Anthropic 兼容协议。

## 启动

双击根目录的 `start.bat`。首次启动会在项目内创建 `.venv` 并安装依赖，之后打开：

- 管理台：<http://127.0.0.1:7009>
- 网关：`http://本机局域网IP:9600`

管理台只监听本机；网关只接受本机或私有网段客户端。首次使用请在管理台生成网关 Token。

运行日志同时输出到启动窗口和 `server/data/logs/aibridge.log`。日志按 5 MB 滚动，最多保留 5 个历史文件；若服务异常退出，可从文件末尾查看最后一次错误及 traceback。

## 技术栈

- 后端：Python 3.12、FastAPI、Uvicorn、HTTPX/HTTP2
- 数据：SQLite；Fernet 加密保存 Cookie 与上游 API Token，网关 Token 只保存 PBKDF2 哈希
- 前端：原生 HTML、CSS、ES Modules，无 Node 构建步骤
- 运行方式：单进程启动两个 ASGI 服务，管理台监听 `127.0.0.1:7009`，网关监听 `0.0.0.0:9600`

项目运行时不依赖 Chrome、Playwright 或浏览器扩展；浏览器扩展是可选的自动同步能力，未安装时仍可手动粘贴 cURL。

## 对外接口

同一个网关 URL 与 Token 可用于两类客户端：

- OpenAI：`POST /v1/chat/completions`、`GET /v1/models`
- Anthropic：`POST /v1/messages`、`GET /v1/models`

Token 既可放在 `Authorization: Bearer ...`，也可放在 Anthropic 常用的 `x-api-key`。模型名使用管理台中的“公开模型名”，客户端不需要知道上游来源。

## 来源类型

- Web：DeepSeek、千问、豆包、Kimi、Perplexity、文心、LongCat、MiMo 等。每个来源对外暴露一个可编辑模型名。Cookie/cURL 只加密保存在 `server/data`，不会写入 Git。LongCat 使用已配对的 AiBridge Catcher 在官网页面中实时生成安全签名，调用期间需要保持 `longcat.chat` 页面打开；MiMo 同样通过已登录的官网页面中继请求，使用期间需要保持 MiMo 页面打开。
- 标准 API：支持 OpenAI 兼容和 Anthropic 兼容上游，每个来源可以配置多个模型映射。

首次启动会尝试从本机 CC Switch 的 Claude 配置导入 DeepSeek Anthropic 上游；只读取本机数据库，密钥不会输出到日志。

## 可选浏览器同步

独立项目 `AiBridge-Catcher` 提供 Chrome 扩展。管理台开启“浏览器同步”并完成一次性配对后，在支持的官网登录并发送一句消息，扩展会把目标请求交给本机 `127.0.0.1:7009`。AiBridge 先用候选凭证执行真实健康检查，通过后才加密保存；失败不会覆盖已有健康配置。

扩展只监听管理台允许的官网对话接口。Kimi 会额外同步页面中的访问令牌与刷新令牌，网关可在访问令牌到期前自动续期并保存轮换后的令牌。

## 说明

官网 Web 接口不是稳定公开 API。健康检查会真实访问上游，并区分未配置、鉴权失效、风控拦截、协议变化和网络错误，不会用静态“绿色”掩盖失败。豆包的请求含动态签名，建议按管理台提示从 F12 复制完整 cURL；千问通常需要 Cookie 与风控请求头；DeepSeek 每次对话需要 Node.js 18+ 调用官网 WASM 完成 PoW；文心会从复制的请求正文中提取登录种子，并为每次调用重建 `chat_token`。

Web 来源会压缩 Claude Code 等 Agent 注入的大段系统提示与工具 schema，并通过网关工具桥还原 OpenAI `tool_calls` 和 Anthropic `tool_use`。桥接支持工具结果回送与 DeepSeek Web 的 DSML 调用格式，已可完成多轮本地工具任务；但官网协议随时可能变化，可靠性仍低于原生标准 API。

Web 工具方言按来源隔离：DeepSeek Web 才解析 DSML，豆包与千问只解析网关标准标签。自定义 API 来源不进入 Web 工具桥，也不做跨协议伪装：OpenAI API 条目使用 `/v1/chat/completions`，Anthropic API 条目使用 `/v1/messages`。

## Web 来源开发约定

新增 Web 来源时必须同时完成协议适配、Catcher 规则、现有数据库迁移和管理台展示。供应商图标应使用官网公开的品牌资源并保存到 `server/public/admin/providers/`，不得以文字缩写占位；对应路径必须加入管理台浏览器冒烟测试。新增 Catcher 来源还要为已有安装执行一次性白名单迁移，同时保留用户迁移后的手动开关选择。
