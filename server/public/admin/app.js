let state = {
  overview: null,
  selectedProviderId: null,
  selectedRouteId: null,
  view: 'overview',
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(`/admin/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || response.statusText);
  return data;
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 3200);
}

function statusPill(status, enabled = true) {
  if (!enabled) return '<span class="pill muted">停用</span>';
  if (!status || status.lastStatus === 'idle') return '<span class="pill muted">未启动</span>';
  if (status.lastStatus === 'available' || status.lastStatus === 'visible_session') return '<span class="pill ok">可用</span>';
  if (status.lastStatus === 'needs_login' || status.lastStatus === 'dom_unready') return '<span class="pill warn">待处理</span>';
  return '<span class="pill bad">异常</span>';
}

function shortUrl(url) {
  if (!url) return '-';
  return url.replace(/^https?:\/\//, '').replace(/\/$/, '');
}

async function loadOverview() {
  state.overview = await api('/overview');
  $('#serverPort').textContent = `:${state.overview.server.port || 9529}`;
  const origin = state.overview.server.localOrigin || window.location.origin;
  $('#openaiBaseUrl').textContent = `${origin}/v1`;
  $('#anthropicBaseUrl').textContent = origin;
  if (!state.selectedProviderId) state.selectedProviderId = state.overview.providers[0]?.id || null;
  if (!state.selectedRouteId) state.selectedRouteId = state.overview.routes[0]?.id || null;
  renderAll();
}

function setView(view) {
  state.view = view;
  $$('.view').forEach(el => el.classList.toggle('active', el.id === `view-${view}`));
  $$('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.view === view));
  const titles = { overview: '总览', providers: '平台', routes: '模型路由', keys: 'API Key', tasks: '任务日志' };
  $('#pageTitle').textContent = titles[view] || '总览';
}

function renderMetrics() {
  const providers = state.overview.providers;
  const sessions = state.overview.sessions;
  const running = providers.filter(provider => sessions[provider.id]?.running).length;
  const available = providers.filter(provider => sessions[provider.id]?.lastStatus === 'available').length;
  const keys = state.overview.keys.filter(key => key.enabled !== false).length;
  $('#metrics').innerHTML = [
    ['平台', providers.length],
    ['运行会话', running],
    ['可用平台', available],
    ['启用 Key', keys],
  ].map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join('');
}

function renderOverviewProviders() {
  const providers = state.overview.providers;
  const sessions = state.overview.sessions;
  $('#overviewProviders').innerHTML = providers.map(provider => {
    const status = sessions[provider.id] || {};
    return `
      <tr>
        <td><strong>${provider.name}</strong><br><span class="muted-text">${provider.id}</span></td>
        <td>${provider.browserMode}</td>
        <td>${statusPill(status, provider.enabled)}</td>
        <td>${shortUrl(provider.startUrl || provider.baseUrl)}</td>
        <td>${status.lastError || '-'}</td>
        <td>
          <div class="row-actions">
            <button data-action="check" data-id="${provider.id}">检测</button>
            <button data-action="open" data-id="${provider.id}">登录</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

function renderProviders() {
  const providers = state.overview.providers;
  const sessions = state.overview.sessions;
  $('#providerList').innerHTML = providers.map(provider => {
    const active = provider.id === state.selectedProviderId ? 'active' : '';
    return `
      <div class="provider-row ${active}" data-provider-id="${provider.id}">
        <div>
          <strong>${provider.name}</strong>
          <span>${shortUrl(provider.startUrl || provider.baseUrl)}</span>
        </div>
        ${statusPill(sessions[provider.id], provider.enabled)}
      </div>
    `;
  }).join('');
  renderProviderForm();
}

function fillForm(form, data) {
  Array.from(form.elements).forEach(input => {
    if (!input.name) return;
    if (input.type === 'checkbox') input.checked = data[input.name] !== false;
    else input.value = data[input.name] ?? '';
  });
}

function readForm(form) {
  const data = {};
  Array.from(form.elements).forEach(input => {
    if (!input.name) return;
    if (input.type === 'checkbox') data[input.name] = input.checked;
    else if (input.type === 'number') data[input.name] = Number(input.value || 0);
    else data[input.name] = input.value.trim();
  });
  return data;
}

function renderProviderForm() {
  const provider = state.overview.providers.find(item => item.id === state.selectedProviderId)
    || state.overview.providers[0];
  if (!provider) return;
  state.selectedProviderId = provider.id;
  fillForm($('#providerForm'), provider);
  const status = state.overview.sessions[provider.id] || {};
  $('#providerDiagnostic').textContent = JSON.stringify({
      provider: provider.id,
      status,
      profileDir: provider.profileDir,
      cookieSessionName: provider.cookieSessionName || null,
      sessionIdConfigured: Boolean(provider.sessionId),
      proxyEnabled: Boolean(provider.proxyEnabled),
      proxyConfigured: Boolean(provider.proxyServer),
  }, null, 2);
}

function renderRouteProviderOptions() {
  $('#routeProviderSelect').innerHTML = state.overview.providers
    .map(provider => `<option value="${provider.id}">${provider.name} (${provider.id})</option>`)
    .join('');
}

function renderRoutes() {
  renderRouteProviderOptions();
  $('#routeRows').innerHTML = state.overview.routes.map(route => {
    const provider = state.overview.providers.find(item => item.id === route.providerId);
    const active = route.id === state.selectedRouteId ? ' class="selected-row"' : '';
    return `
      <tr data-route-id="${route.id}"${active}>
        <td><strong>${route.pattern}</strong><br><span>${route.id}</span></td>
        <td>${route.type}</td>
        <td>${provider?.name || route.providerId}</td>
        <td>${route.priority}</td>
        <td>${route.enabled ? '<span class="pill ok">启用</span>' : '<span class="pill muted">停用</span>'}</td>
      </tr>
    `;
  }).join('');
  renderRouteForm();
}

function renderRouteForm() {
  const route = state.overview.routes.find(item => item.id === state.selectedRouteId)
    || state.overview.routes[0]
    || { id: '', type: 'contains', pattern: '', providerId: state.overview.providers[0]?.id || '', priority: 0, enabled: true, newChat: false };
  state.selectedRouteId = route.id;
  fillForm($('#routeForm'), route);
}

function renderKeys() {
  $('#keyRows').innerHTML = state.overview.keys.map(key => `
    <tr>
      <td>${key.name}</td>
      <td><code>${key.prefix}</code></td>
      <td>${key.enabled ? '<span class="pill ok">启用</span>' : '<span class="pill muted">停用</span>'}</td>
      <td>${key.createdAt || '-'}</td>
      <td>${key.lastUsedAt || '-'}</td>
      <td>
        <div class="row-actions">
          <button data-action="toggle-key" data-id="${key.id}" data-enabled="${key.enabled}">${key.enabled ? '禁用' : '启用'}</button>
          <button data-action="delete-key" data-id="${key.id}">删除</button>
        </div>
      </td>
    </tr>
  `).join('');
}

function renderTasks() {
  const tasks = state.overview.recentTasks || [];
  $('#taskRows').innerHTML = tasks.map(task => `
    <tr>
      <td>${task.loggedAt || '-'}</td>
      <td>${task.status === 'ok' ? '<span class="pill ok">成功</span>' : '<span class="pill bad">失败</span>'}</td>
      <td>${task.model || '-'}</td>
      <td>${task.providerId || '-'}</td>
      <td>${task.elapsedMs ?? '-'} ms</td>
      <td>${task.message || task.code || '-'}</td>
    </tr>
  `).join('');
}

function renderAll() {
  if (!state.overview) return;
  renderMetrics();
  renderOverviewProviders();
  renderProviders();
  renderRoutes();
  renderKeys();
  renderTasks();
}

async function checkProvider(id) {
  toast('正在检测平台...');
  const result = await api(`/providers/${id}/check`, { method: 'POST', body: {} });
  $('#providerDiagnostic').textContent = JSON.stringify(result, null, 2);
  await loadOverview();
  toast(result.ok ? '检测通过' : '检测完成，平台需要处理');
}

async function openSession(id) {
  toast('正在打开会话窗口...');
  const result = await api(`/providers/${id}/open-session`, { method: 'POST', body: {} });
  $('#providerDiagnostic').textContent = JSON.stringify(result, null, 2);
  await loadOverview();
  toast('会话窗口已打开');
}

async function saveProvider() {
  const data = readForm($('#providerForm'));
  if (!data.id) return toast('平台 ID 不能为空');
  const existing = state.overview.providers.some(provider => provider.id === data.id);
  if (existing) await api(`/providers/${data.id}`, { method: 'PATCH', body: data });
  else await api('/providers', { method: 'POST', body: data });
  state.selectedProviderId = data.id;
  await loadOverview();
  toast('平台已保存');
}

async function saveRoute() {
  const data = readForm($('#routeForm'));
  if (!data.id) data.id = `route-${Date.now()}`;
  const existing = state.overview.routes.some(route => route.id === data.id);
  if (existing) await api(`/routes/${data.id}`, { method: 'PATCH', body: data });
  else await api('/routes', { method: 'POST', body: data });
  state.selectedRouteId = data.id;
  await loadOverview();
  toast('路由已保存');
}

function bindEvents() {
  $$('.nav-item').forEach(button => {
    button.addEventListener('click', () => setView(button.dataset.view));
  });

  $('#refreshBtn').addEventListener('click', () => loadOverview().then(() => toast('已刷新')));
  $('#reloadTasksBtn').addEventListener('click', () => loadOverview().then(() => toast('任务已刷新')));
  $('#checkAllBtn').addEventListener('click', async () => {
    for (const provider of state.overview.providers.filter(item => item.enabled)) {
      await api(`/providers/${provider.id}/check`, { method: 'POST', body: {} }).catch(() => null);
    }
    await loadOverview();
    toast('全部检测完成');
  });

  document.addEventListener('click', async (event) => {
    const target = event.target;
    const providerRow = target.closest?.('[data-provider-id]');
    if (providerRow) {
      state.selectedProviderId = providerRow.dataset.providerId;
      renderProviders();
    }

    const routeRow = target.closest?.('[data-route-id]');
    if (routeRow) {
      state.selectedRouteId = routeRow.dataset.routeId;
      renderRoutes();
    }

    if (target.dataset?.action === 'check') await checkProvider(target.dataset.id);
    if (target.dataset?.action === 'open') await openSession(target.dataset.id);
    if (target.dataset?.action === 'toggle-key') {
      await api(`/keys/${target.dataset.id}`, {
        method: 'PATCH',
        body: { enabled: target.dataset.enabled !== 'true' },
      });
      await loadOverview();
    }
    if (target.dataset?.action === 'delete-key') {
      await api(`/keys/${target.dataset.id}`, { method: 'DELETE' });
      await loadOverview();
    }
  });

  $('#newProviderBtn').addEventListener('click', () => {
    state.selectedProviderId = null;
    fillForm($('#providerForm'), {
      id: '',
      name: '',
      baseUrl: '',
      startUrl: '',
      sessionId: '',
      cookieSessionName: '',
      profileDir: '',
      proxyEnabled: false,
      proxyServer: '',
      proxyUsername: '',
      proxyPassword: '',
      adapter: 'generic',
      browserChannel: '',
      browserMode: 'headless',
      healthPrompt: '请只回复 ok',
      taskTimeoutMs: 120000,
      idleTtlMs: 600000,
      enabled: true,
      newChat: false,
    });
  });

  $('#saveProviderBtn').addEventListener('click', saveProvider);
  $('#checkProviderBtn').addEventListener('click', () => checkProvider($('#providerForm').elements.id.value));
  $('#openSessionBtn').addEventListener('click', () => openSession($('#providerForm').elements.id.value));
  $('#closeSessionBtn').addEventListener('click', async () => {
    await api(`/providers/${$('#providerForm').elements.id.value}/close-session`, { method: 'POST', body: {} });
    await loadOverview();
    toast('会话已关闭');
  });
  $('#resetSessionBtn').addEventListener('click', async () => {
    const id = $('#providerForm').elements.id.value;
    if (!id) return;
    await api(`/providers/${id}/reset-session`, { method: 'POST', body: {} });
    await loadOverview();
    toast('Profile 已重置');
  });

  $('#newRouteBtn').addEventListener('click', () => {
    state.selectedRouteId = null;
    fillForm($('#routeForm'), {
      id: '',
      type: 'contains',
      pattern: '',
      providerId: state.overview.providers[0]?.id || '',
      priority: 0,
      enabled: true,
      newChat: false,
    });
  });
  $('#saveRouteBtn').addEventListener('click', saveRoute);
  $('#routePreviewBtn').addEventListener('click', async () => {
    const model = $('#routePreviewInput').value.trim();
    const result = await api('/routes/preview', { method: 'POST', body: { model } });
    $('#routePreviewResult').textContent = result.provider
      ? `命中 ${result.provider.name} / ${result.route.pattern}`
      : '未命中';
  });

  $('#createKeyBtn').addEventListener('click', async () => {
    const result = await api('/keys', {
      method: 'POST',
      body: {
        name: $('#newKeyName').value.trim() || '本地 Key',
        rateLimitPerMinute: Number($('#newKeyRate').value || 0),
      },
    });
    $('#createdKey').textContent = result.key;
    $('#keyReveal').classList.remove('hidden');
    await loadOverview();
    toast('Key 已创建');
  });
}

bindEvents();
setView('overview');
loadOverview().catch(error => toast(error.message));
