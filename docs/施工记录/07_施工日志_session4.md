# Session 4 施工日志 (2026-02-21)

## 一、Observer 结束检测优化

### 问题
元宝 web 端秒回，但 opencode 要多等 ~10秒。原因：observer 纯靠 `noChangeCount >= 20`（20×500ms）超时判断结束。

### 修复
- 新增 `isGenerationIdle()` — 检测停止按钮消失 + 输入框可用
- idle 状态下 3秒（6×500ms）即触发 done，保底 10秒不变
- 文件：`extension/src/content/adapters/generic.ts`

## 二、`<tool_call>` 标签被浏览器吞掉

### 问题
AI 输出 `<tool_call>...</tool_call>`，浏览器当作 HTML 元素渲染，`innerText` 丢失标签，server 端 `parseToolCalls` 正则匹配不到。

### 修复
- 新增 `extractText()` — 检测 DOM 中 `<tool_call>` 元素，clone 后用 `replaceWith()` 重建文本标签
- `collectNewReplyText` 和 `observeResponse` fallback 均改用 `extractText`
- 文件：`extension/src/content/adapters/generic.ts`

## 三、Observer 抓错元素（文件上传区）

### 问题
豆包页面的文件上传区域（"在此处拖放文件..."）匹配了 `[class*="markdown"]` 选择器，observer 把 UI 文本当成 AI 回复。

### 修复
- 新增 `isReplyElement()` — 过滤短文本、文件上传区、输入框附近元素
- 新增 `filterReplies()` — 统一过滤入口
- `collectNewReplyText`、`countReplyElements`、`findLastReplyRaw` 均经过过滤
- 文件：`extension/src/content/adapters/generic.ts`

## 四、Tool Prompt 注入工作目录

### 问题
Kimi 幻觉路径 `/mnt/kimi/upload`，不知道实际工作目录。

### 修复
- 新增 `extractCwd()` — 从 opencode messages 中提取 `Working directory` 路径
- `buildToolSystemPrompt` 接受 cwd 参数，注入目录 + 禁止编造路径提示 + 禁用内置工具提示
- 文件：`server/src/index.js`

## 五、新增文件

| 文件 | 说明 |
|------|------|
| `server/start.bat` | 双击启动脚本 |
| `README.md` | 项目说明文档 |

## 六、平台测试结果

| 平台 | 普通对话 | Tool Calling | 备注 |
|------|---------|-------------|------|
| 豆包 | ✅ | ✅ | 需刷新扩展生效 |
| Qwen | ✅ | ✅ | markdown 格式偶有小瑕疵 |
| 元宝 | ✅ | ⚠️ 待验证 | tool prompt 可见 |
| Kimi | ✅ | ❌ | 平台优先调用内置 Python 执行器 |
| MiMo | ✅ | ✅ | 输出偏长 |

## 七、提交记录

`ae70f57` — Fix observer reliability, add tool_call parsing, cwd injection, and startup script
