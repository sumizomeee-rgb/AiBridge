import { MODEL_ROUTES, type TaskMessage, type RoutingConfig } from '../shared/types';

const WS_URL = 'ws://localhost:9529';
let ws: WebSocket | null = null;
const taskQueue: Map<string, TaskMessage[]> = new Map();
const busyTabs = new Set<string>();
const taskTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

// --- WebSocket 连接管理 ---
function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('[BG] Connected to server');
    chrome.storage.local.set({ wsConnected: true });
  };

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') {
      ws?.send(JSON.stringify({ type: 'pong' }));
    } else if (msg.type === 'task') {
      handleTask(msg as TaskMessage);
    }
  };

  ws.onclose = () => {
    console.log('[BG] Disconnected, reconnecting...');
    chrome.storage.local.set({ wsConnected: false });
    ws = null;
    setTimeout(connect, 5000);
  };

  ws.onerror = () => ws?.close();
}

// --- 发送消息回 Server ---
export function sendToServer(data: object) {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
  }
}

// --- 路由：根据 model 找到目标平台 URL ---
async function resolveTargetUrl(model: string): Promise<string> {
  const config = await chrome.storage.local.get('routing') as { routing?: RoutingConfig };
  const routing = config.routing || { mode: 'AUTO', target_ai: 'gpt' };

  const key = routing.mode === 'OVERRIDE'
    ? routing.target_ai
    : Object.keys(MODEL_ROUTES).find(k => model.toLowerCase().includes(k)) || 'gpt';

  return MODEL_ROUTES[key]?.url || MODEL_ROUTES['gpt'].url;
}

// --- 查找或创建目标 Tab ---
async function findOrCreateTab(url: string): Promise<chrome.tabs.Tab> {
  const tabs = await chrome.tabs.query({ url: `${url}/*` });
  if (tabs.length > 0) {
    await chrome.tabs.update(tabs[0].id!, { active: true });
    return tabs[0];
  }
  const tab = await chrome.tabs.create({ url, active: true });
  // 等待新 Tab 加载完成
  await new Promise<void>((resolve) => {
    const listener = (tabId: number, info: chrome.tabs.TabChangeInfo) => {
      if (tabId === tab.id && info.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
  return tab;
}

// --- 等待 Tab 加载完成 ---
function waitForTabLoad(tabId: number): Promise<void> {
  return new Promise((resolve) => {
    const listener = (id: number, info: chrome.tabs.TabChangeInfo) => {
      if (id === tabId && info.status === 'complete') {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// --- 任务处理 ---
async function handleTask(task: TaskMessage) {
  try {
    const targetUrl = await resolveTargetUrl(task.model);
    const tab = await findOrCreateTab(targetUrl);
    if (!tab.id) throw new Error('No tab id');

    // new_chat: 由 background 导航到基础 URL，避免 content script 自毁
    if (task.new_chat) {
      const baseUrl = targetUrl.replace(/\/$/, '') + '/';
      const current = tab.url || '';
      if (current !== baseUrl && current !== targetUrl) {
        await chrome.tabs.update(tab.id, { url: baseUrl });
        await waitForTabLoad(tab.id);
        await new Promise(r => setTimeout(r, 1500));
      }
      task = { ...task, new_chat: false };
    }

    const tabKey = String(tab.id);

    if (busyTabs.has(tabKey)) {
      const queue = taskQueue.get(tabKey) || [];
      queue.push(task);
      taskQueue.set(tabKey, queue);
      return;
    }

    await executeTask(tab.id, tabKey, task);
  } catch (err: any) {
    sendToServer({ type: 'error', task_id: task.task_id, code: 'TAB_NOT_FOUND', message: err.message });
  }
}

async function executeTask(tabId: number, tabKey: string, task: TaskMessage) {
  busyTabs.add(tabKey);

  // 超时保护：60秒无响应自动释放
  const timeout = setTimeout(() => {
    if (busyTabs.has(tabKey)) {
      busyTabs.delete(tabKey);
      sendToServer({ type: 'error', task_id: task.task_id, code: 'TIMEOUT', message: 'Task timed out' });
    }
  }, 60000);
  taskTimeouts.set(task.task_id, timeout);

  for (let i = 0; i < 5; i++) {
    try {
      await chrome.tabs.sendMessage(tabId, task);
      return;
    } catch {
      if (i < 4) await new Promise(r => setTimeout(r, 1000));
    }
  }
  clearTimeout(timeout);
  taskTimeouts.delete(task.task_id);
  busyTabs.delete(tabKey);
  sendToServer({ type: 'error', task_id: task.task_id, code: 'DOM_TIMEOUT', message: 'Content script not ready' });
}

// --- 统一消息监听 ---
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getStatus') {
    sendResponse({ connected: ws?.readyState === WebSocket.OPEN });
    return;
  }

  if (msg.type === 'chunk' || msg.type === 'done' || msg.type === 'error') {
    sendToServer(msg);

    if (msg.type === 'done' || msg.type === 'error') {
      const tid = msg.task_id;
      if (taskTimeouts.has(tid)) { clearTimeout(taskTimeouts.get(tid)!); taskTimeouts.delete(tid); }
      const tabKey = String(sender.tab?.id);
      busyTabs.delete(tabKey);
      const queue = taskQueue.get(tabKey);
      if (queue?.length) {
        const next = queue.shift()!;
        if (!queue.length) taskQueue.delete(tabKey);
        executeTask(sender.tab!.id!, tabKey, next);
      }
    }
  }
});

// --- Keep-alive: 防止 MV3 Service Worker 休眠 ---
chrome.alarms.create('keepalive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepalive') {
    // alarm 本身就能唤醒 SW，无需额外操作
  }
});

// --- 启动连接 ---
connect();
