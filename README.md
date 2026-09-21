# AiBridge

AiBridge 是一个在本机运行、供局域网内 Agent 共用的 AI 网关。它把官网 Web 会话和标准 API 汇总到同一套模型名下，对外同时提供 OpenAI 与 Anthropic 兼容协议。

## 启动

双击根目录的 `start.bat`。首次启动会在项目内创建 `.venv` 并安装依赖，之后打开：

- 管理台：<http://127.0.0.1:7009>
- 网关：`http://本机局域网IP:9600`

管理台只监听本机；网关只接受本机或私有网段客户端。首次使用请在管理台生成网关 Token。

## 技术栈

- 后端：Python 3.12、FastAPI、Uvicorn、HTTPX/HTTP2
- 数据：SQLite；Fernet 加密保存 Cookie 与上游 API Token，网关 Token 只保存 PBKDF2 哈希
- 前端：原生 HTML、CSS、ES Modules，无 Node 构建步骤
- 运行方式：单进程启动两个 ASGI 服务，管理台监听 `127.0.0.1:7009`，网关监听 `0.0.0.0:9600`

项目运行时不依赖 Chrome、Playwright 或浏览器扩展；浏览器只用于首次取得官网会话 Cookie。

## 对外接口

同一个网关 URL 与 Token 可用于两类客户端：

- OpenAI：`POST /v1/chat/completions`、`GET /v1/models`
- Anthropic：`POST /v1/messages`、`GET /v1/models`

Token 既可放在 `Authorization: Bearer ...`，也可放在 Anthropic 常用的 `x-api-key`。模型名使用管理台中的“公开模型名”，客户端不需要知道上游来源。

## 来源类型

- Web：豆包、千问等。每个来源对外暴露一个可编辑模型名。Cookie/cURL 只加密保存在 `server/data`，不会写入 Git。
- 标准 API：支持 OpenAI 兼容和 Anthropic 兼容上游，每个来源可以配置多个模型映射。

首次启动会尝试从本机 CC Switch 的 Claude 配置导入 DeepSeek Anthropic 上游；只读取本机数据库，密钥不会输出到日志。

## 说明

官网 Web 接口不是稳定公开 API。健康检查会真实访问上游，并区分未配置、鉴权失效、风控拦截、协议变化和网络错误，不会用静态“绿色”掩盖失败。豆包的请求含动态签名，建议按管理台提示从 F12 复制完整 cURL；千问通常需要 Cookie 与风控请求头。
