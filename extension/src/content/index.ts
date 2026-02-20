import type { AiAdapter } from './adapters/base';
import type { TaskMessage } from '../shared/types';
import { ChatGPTAdapter } from './adapters/chatgpt';
import { GenericAdapter } from './adapters/generic';

function getAdapter(): AiAdapter {
  const host = location.hostname;
  if (host.includes('chatgpt.com')) return new ChatGPTAdapter();
  // 豆包、通义、Gemini 等全部走通用适配器
  return new GenericAdapter();
}

chrome.runtime.onMessage.addListener((msg: TaskMessage, _sender, _sendResponse) => {
  if (msg.type !== 'task') return;
  handleTask(getAdapter(), msg);
});

async function handleTask(adapter: AiAdapter, task: TaskMessage) {
  try {
    if (task.new_chat) await adapter.newChat();
    const text = task.messages[task.messages.length - 1]?.content || '';
    await adapter.sendMessage(text);

    adapter.observeResponse(
      (delta) => chrome.runtime.sendMessage({ type: 'chunk', task_id: task.task_id, delta }),
      () => chrome.runtime.sendMessage({ type: 'done', task_id: task.task_id }),
    );
  } catch (err: any) {
    chrome.runtime.sendMessage({ type: 'error', task_id: task.task_id, code: 'DOM_TIMEOUT', message: err.message });
  }
}
