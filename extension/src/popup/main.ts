import { MODEL_ROUTES, type RoutingConfig } from '../shared/types';

const dot = document.getElementById('dot')!;
const statusText = document.getElementById('statusText')!;
const modeSelect = document.getElementById('mode') as HTMLSelectElement;
const targetSelect = document.getElementById('target') as HTMLSelectElement;
const nav = document.getElementById('nav')!;

// 状态查询
chrome.runtime.sendMessage({ type: 'getStatus' }, (res) => {
  const on = res?.connected;
  dot.className = 'dot ' + (on ? 'on' : 'off');
  statusText.textContent = on ? 'Connected' : 'Disconnected';
});

// 动态生成 target 下拉选项
for (const [key, { name }] of Object.entries(MODEL_ROUTES)) {
  const opt = document.createElement('option');
  opt.value = key;
  opt.textContent = name;
  targetSelect.appendChild(opt);
}

// 加载已保存配置
chrome.storage.local.get('routing', (data) => {
  const r = data.routing as RoutingConfig | undefined;
  if (r) {
    modeSelect.value = r.mode;
    targetSelect.value = r.target_ai;
    targetSelect.disabled = r.mode === 'AUTO';
  }
});

// 保存配置
function saveConfig() {
  const routing: RoutingConfig = {
    mode: modeSelect.value as RoutingConfig['mode'],
    target_ai: targetSelect.value,
  };
  chrome.storage.local.set({ routing });
  targetSelect.disabled = routing.mode === 'AUTO';
}

modeSelect.addEventListener('change', saveConfig);
targetSelect.addEventListener('change', saveConfig);

// 导航快捷链接
for (const [key, { name, url }] of Object.entries(MODEL_ROUTES)) {
  const a = document.createElement('a');
  a.textContent = name;
  a.addEventListener('click', async () => {
    const tabs = await chrome.tabs.query({ url: `${url}/*` });
    if (tabs.length) {
      chrome.tabs.update(tabs[0].id!, { active: true });
    } else {
      chrome.tabs.create({ url });
    }
  });
  nav.appendChild(a);
}
