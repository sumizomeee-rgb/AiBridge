# 施工交接文档 (2026-02-21 Session 3)

## 一、当前状态总览

| 组件 | 状态 | 说明 |
|------|------|------|
| Server (`server/src/index.js`) | ✅ 运行中 | tool prompt 顺序已调整，body limit 2mb |
| Extension dist | ✅ 已构建 | 最新代码已 build |
| 豆包 (doubao) | ✅ 完全可用 | tool calling + 普通对话均正常 |
| Qwen (chat.qwen.ai) | ⚠️ 基本可用 | 回复有小瑕疵（markdown格式偶尔乱），功能正常 |
| 元宝 (yuanbao.tencent.com) | ⚠️ 待验证 | tool prompt 顺序刚修复，未重新测试 |
| Kimi (kimi.com) | ❌ 未测试 | 已添加配置，未验证 |
| MiMo (aistudio.xiaomimimo.com) | ❌ 未测试 | 已添加配置，未验证 |
| Popup 下拉框 | ✅ 已修复 | 从 MODEL_ROUTES 动态生成选项 |

## 二、本次 Session 完成的工作

### 1. Self-review 3个 Bug 修复

- **Bug 2 — tool prompt 重复注入**: tool result 轮不再重复注入 tool 定义
- **Bug 3 — tool schema 过大**: 精简为 name + 截断 description + 参数名列表
- **Bug 4 — body size limit**: `express.json({ limit: '2mb' })`

### 2. 新增平台支持

在 `types.ts` 和 `manifest.json` 中添加了：
- Qwen → `https://chat.qwen.ai`
- 元宝 → `https://yuanbao.tencent.com`
- Kimi → `https://www.kimi.com`
- MiMo → `https://aistudio.xiaomimimo.com`

### 3. Qwen tool calling 调试（多轮修复）

| 问题 | 修复 |
|------|------|
| Qwen 调用占位符 "tool_name" | prompt 模板改为 "ACTUAL_TOOL_NAME" |
| observer 超时太短（3s） | noChangeCount 阈值从 6 → 20（10s） |
| 第二轮 tool result 无响应 | 添加 `prevReplyCount` + `countReplyElements()` 元素计数检测 |
| 最终回复乱码 | `onDone` 传递 `fullText`，server 优先使用完整文本 |
| 最终回复截断 | `collectNewReplyText()` 收集所有新回复元素拼接文本 |

### 4. Tool prompt 注入顺序调整

元宝忽略用户实际问题，原因是 tool prompt 在前、用户消息在后。
修复：改为用户消息在前 + tool prompt 在后：
```js
content: messages[lastUserIdx].content + '\n\n' + toolPrompt
```

### 5. Popup 下拉框动态化

`index.html` 中移除硬编码 `<option>`，`main.ts` 中从 `MODEL_ROUTES` 动态生成。

## 三、关键文件变更清单

### 服务器端
| 文件 | 变更 |
|------|------|
| `server/src/index.js` | tool prompt 顺序、schema 精简、body limit、fullText 支持、tool result 轮跳过注入 |

### 扩展端
| 文件 | 变更 |
|------|------|
| `extension/src/content/adapters/generic.ts` | `collectNewReplyText()`、`countReplyElements()`、`prevReplyCount`、去除 debug 日志 |
| `extension/src/content/adapters/base.ts` | `onDone` 签名加 `fullText` 参数 |
| `extension/src/content/adapters/chatgpt.ts` | `onDone` 签名同步更新 |
| `extension/src/content/index.ts` | done 消息携带 `fullText` |
| `extension/src/shared/types.ts` | 新增 qwen/yuanbao/kimi/mimo 平台定义 |
| `extension/manifest.json` | 新增 host_permissions 和 content_scripts |
| `extension/src/popup/index.html` | target 下拉框改为动态生成 |
| `extension/src/popup/main.ts` | 从 MODEL_ROUTES 生成 option 元素 |

## 四、已知问题

### 1. Qwen 回复格式小瑕疵
Qwen markdown 重渲染后 `collectNewReplyText()` 拼接多个元素时可能引入多余换行。功能不受影响，纯显示问题。

### 2. 元宝 tool prompt 可见性
tool prompt 注入到用户消息中，web 端会显示出来。顺序已调整（用户消息在前），但 tool 定义文本仍可见。不影响功能。

### 3. observer 通用性
`[class*="markdown"]` 等选择器是通用猜测，新平台可能需要调整。Kimi/MiMo 尚未测试。

### 4. 连续对话第二条消息偶尔失败
Qwen 测试中出现过第二条消息 `type=error`，可能是前一轮 observer 未完全清理或 web 端仍在生成状态。

## 五、接手后优先事项

1. **测试元宝** — tool prompt 顺序修复后验证是否正常回答问题
2. **测试 Kimi 和 MiMo** — 验证 GenericAdapter 是否兼容
3. **清理 debug 日志** — `server/src/index.js` 中的 `[Dispatch]`/`[ExtMsg]` 日志
4. **稳定性测试** — 连续多轮对话、tool calling 多轮

## 六、启动命令速查

```bash
# 构建扩展
cd G:\SuchProject\Other\AiBridge\extension && npm run build

# 启动服务器
cd G:\SuchProject\Other\AiBridge\server && node src/index.js

# 健康检查
curl http://localhost:9529/health

# opencode 测试
opencode run -m ency/Ency "你好"
```
