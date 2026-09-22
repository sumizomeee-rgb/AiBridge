const $ = (selector) => document.querySelector(selector);
let state = { sources: [], keys: [], logs: [], gateway: {}, permissions: {}, catcher: {} };
let checkingWebSources = new Set();
let webHealthRunning = false;

const statusLabels = {
  healthy: "可用", checking: "检测中", unchecked: "待检测", unconfigured: "未配置", unsupported: "未接入",
  auth_expired: "鉴权失效", challenge: "风控拦截", protocol_mismatch: "协议异常",
  network_error: "网络异常", upstream_error: "上游错误", invalid_config: "配置不完整",
  rate_limited: "上游限流",
};

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "content-type": "application/json", ...(options.headers || {}) }, ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error?.message || `请求失败（HTTP ${response.status}）`);
  return data;
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = error ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = ""; }, 3200);
}

const icons = {
  activity: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2.2-5 4.1 10 2.2-5H21"/></svg>`,
  settings: `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3v-.2h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>`,
  sync: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7h-5V2"/><path d="M20 7a8 8 0 0 0-13.7-2.4L4 7"/><path d="M4 17h5v5"/><path d="M4 17a8 8 0 0 0 13.7 2.4L20 17"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/></svg>`,
  refresh: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>`,
};

const providerMarks = {
  "web-auto": `<img src="/assets/favicon.svg" alt="">`,
  "web-deepseek": `<img src="/assets/providers/deepseek.svg" alt="">`,
  "web-qwen": `<img src="/assets/providers/qwen.png" alt="">`,
  "web-doubao": `<img src="/assets/providers/doubao.png" alt="">`,
  "web-yuanbao": `<img src="/assets/providers/yuanbao.png" alt="">`,
  "web-kimi": `<img src="/assets/providers/kimi.svg" alt="">`,
  "web-perplexity": `<img src="/assets/providers/perplexity.svg" alt="">`,
  "web-wenxin": `<img src="https://psstatic.cdn.bcebos.com/aife/image/baidu_ai_logo_1736910930000.png" alt="">`,
};

function apiProviderMark(source, fallback) {
  const svg = String(source.config?.icon_svg || "").trim();
  if (!svg || svg.length > 20000 || !/^<svg(?:\s|>)/i.test(svg) || !/<\/svg>$/i.test(svg)) return fallback;
  const dataUrl = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return `<img src="${escapeHtml(dataUrl)}" alt="">`;
}

function iconButton(action, icon, label, className = "") {
  return `<button class="icon-action ${className}" data-action="${action}" data-tooltip="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${icons[icon]}</button>`;
}

function sourceSwitch(source) {
  const canToggle = Boolean(state.permissions?.source_toggle);
  const stateLabel = source.enabled ? "已启用" : "已停用";
  const hint = canToggle ? `${stateLabel}，点击切换` : `${stateLabel}；仅可在本机 127.0.0.1 操作`;
  return `<span class="switch-wrap" data-tooltip="${escapeHtml(hint)}"><button class="source-switch ${source.enabled ? "active" : ""}" type="button" role="switch" aria-checked="${source.enabled}" aria-label="${escapeHtml(source.name)}：${stateLabel}" data-source-enable="${escapeHtml(source.id)}" ${canToggle ? "" : "disabled"}><span></span></button></span>`;
}

function sourceCard(source) {
  const checking = checkingWebSources.has(source.id);
  const healthStatus = checking ? "checking" : source.health_status;
  const statusClass = statusLabels[healthStatus] ? healthStatus : "error";
  const initials = source.name.replace(/\s*Web|\s*官方/g, "").slice(0, 2).toUpperCase();
  const fallbackMark = `<span>${escapeHtml(initials)}</span>`;
  const providerMark = source.kind === "web" ? (providerMarks[source.id] || fallbackMark) : apiProviderMark(source, fallbackMark);
  const models = source.models.filter((x) => x.enabled).map((x) => `<span class="model-tag">${escapeHtml(x.public_name)}</span>`).join("") || `<span class="muted">暂无公开模型</span>`;
  const isAuto = source.id === "web-auto";
  let website = "";
  if (source.kind === "web" && !isAuto && source.base_url) {
    try {
      const parsed = new URL(source.base_url);
      if (["http:", "https:"].includes(parsed.protocol)) website = parsed.href;
    } catch { /* 非标准地址不生成外链 */ }
  }
  const providerIcon = website
    ? `<a class="provider-icon provider-link" href="${escapeHtml(website)}" target="_blank" rel="noopener noreferrer" data-tooltip="打开 ${escapeHtml(source.name)} 官网" aria-label="打开 ${escapeHtml(source.name)} 官网">${providerMark}</a>`
    : `<div class="provider-icon ${isAuto ? "auto" : ""}">${providerMark}</div>`;
  const type = isAuto ? "自动路由" : (source.kind === "web" ? "官网直连" : (source.protocol === "anthropic" ? "Anthropic API" : "OpenAI API"));
  const endpoint = isAuto
    ? `${source.config?.routing_mode === "custom" ? `自定义 ${(source.config?.source_ids || []).length} 个来源` : "智能来源"} · ${source.config?.dispatch_mode === "balanced" ? "均衡轮询" : "优先来源"}`
    : source.base_url;
  const runtime = source.runtime || {};
  const capacity = source.kind === "web"
    ? `<span class="capacity"><i></i>${isAuto ? `池 ${runtime.active || 0}/${runtime.limit || 0}` : `运行 ${runtime.active || 0}/${runtime.limit || 3}`}${runtime.queued ? ` · 排队 ${runtime.queued}` : ""}</span>`
    : "";
  const actions = [iconButton("health", "activity", "测试连接"), iconButton("edit", "settings", "配置来源")];
  if (source.kind === "api" && source.protocol === "openai") actions.push(iconButton("discover", "sync", "同步模型"));
  if (source.kind === "api") actions.push(iconButton("delete", "trash", "删除来源", "danger"));
  return `<article class="source-row ${source.enabled ? "" : "disabled"}" data-source="${escapeHtml(source.id)}">
    <div class="source-identity">${providerIcon}<div><strong>${escapeHtml(source.name)}</strong><small>${escapeHtml(type)} · ${escapeHtml(endpoint)}</small></div></div>
    <div class="models-wrap"><div class="models">${models}</div>${capacity}</div>
    <div class="source-health"><span class="status ${statusClass}"><i></i>${escapeHtml(statusLabels[healthStatus] || "异常")}</span><span class="health-copy">${escapeHtml(checking ? "正在验证官网会话…" : (source.health_message || "尚未检测"))}</span>${!checking && source.last_checked_at ? `<time>${escapeHtml(formatTime(source.last_checked_at))}</time>` : ""}</div>
    <div class="row-actions">${sourceSwitch(source)}${actions.join("")}</div>
  </article>`;
}

function renderSources() {
  const web = state.sources.filter((x) => x.kind === "web");
  const apis = state.sources.filter((x) => x.kind === "api");
  $("#web-sources").innerHTML = web.map(sourceCard).join("") || `<div class="empty">没有 Web 来源</div>`;
  $("#api-sources").innerHTML = apis.map(sourceCard).join("") || `<div class="empty">还没有标准 API，点击右上角添加。</div>`;
}

function renderKeys() {
  $("#keys").innerHTML = state.keys.length ? state.keys.map((key) => `<div class="list-row"><div><strong>${escapeHtml(key.name)}</strong><br><code>${escapeHtml(key.prefix)}••••••••</code></div><small>${key.last_used_at ? `最后使用 ${escapeHtml(formatTime(key.last_used_at))}` : "尚未使用"}</small><button class="icon-action danger" data-key-delete="${escapeHtml(key.id)}" data-tooltip="删除 Token" aria-label="删除 Token">${icons.trash}</button></div>`).join("") : `<div class="empty">尚未生成网关 Token</div>`;
}

function renderCatcher() {
  const catcher = state.catcher || {};
  const enabled = Boolean(catcher.enabled);
  const block = $("#catcher");
  block.classList.toggle("off", !enabled);
  $("#catcher-state").textContent = enabled ? "已开启" : "未开启";
  const toggle = $("#catcher-toggle");
  toggle.classList.toggle("active", enabled);
  toggle.setAttribute("aria-checked", String(enabled));
  $("#catcher-auto-enable").checked = Boolean(catcher.auto_enable);

  const allowed = new Set(catcher.allowed_source_ids || []);
  const gradeLabels = { stable: "稳定", beta: "测试", experimental: "实验" };
  $("#catcher-sources").innerHTML = (catcher.rules || []).map((rule) => `<label class="catcher-source"><input type="checkbox" data-catcher-source="${escapeHtml(rule.source_id)}" ${allowed.has(rule.source_id) ? "checked" : ""}><span>${escapeHtml(rule.name)}</span><em class="source-grade">${escapeHtml(gradeLabels[rule.grade] || rule.grade)}</em></label>`).join("");

  const clients = catcher.clients || [];
  $("#catcher-client-summary").textContent = clients.length ? `${clients.length} 个扩展已配对` : "尚无扩展连接";
  $("#catcher-clients").innerHTML = clients.map((client) => `<div class="catcher-client"><div><strong>${escapeHtml(client.name)}</strong><small>${escapeHtml(client.token_prefix)}•• · ${client.last_seen_at ? `最后连接 ${escapeHtml(formatTime(client.last_seen_at))}` : "尚未连接"}</small></div><button data-catcher-revoke="${escapeHtml(client.id)}">撤销</button></div>`).join("");

  const last = (catcher.events || [])[0];
  const eventNode = $("#catcher-last-event");
  if (last) {
    const source = state.sources.find((item) => item.id === last.source_id);
    eventNode.textContent = `${formatTime(last.created_at)} · ${source?.name || last.source_id} · ${last.message}`;
    eventNode.className = `catcher-last-event ${last.status === "healthy" ? "ok" : "fail"}`;
  } else {
    eventNode.textContent = "等待浏览器捕获";
    eventNode.className = "catcher-last-event";
  }
  if (!catcher.pairing_active) $("#catcher-pairing").hidden = true;
}

function catcherSettingsFromUi(overrides = {}) {
  return {
    enabled: state.catcher?.enabled || false,
    auto_enable: $("#catcher-auto-enable").checked,
    allowed_source_ids: [...document.querySelectorAll("[data-catcher-source]:checked")].map((node) => node.dataset.catcherSource),
    ...overrides,
  };
}

async function saveCatcherSettings(overrides = {}) {
  await api("/api/catcher/settings", { method: "PATCH", body: JSON.stringify(catcherSettingsFromUi(overrides)) });
  await load();
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
}

function renderLogs() {
  $("#logs-body").innerHTML = state.logs.length ? state.logs.map((log) => `<tr><td>${escapeHtml(formatTime(log.created_at))}</td><td><strong>${escapeHtml(log.model_name)}</strong><br><small>${escapeHtml(log.source_name)}</small></td><td>${escapeHtml(log.protocol)}</td><td class="${log.status === "ok" ? "ok" : "fail"}">${log.status === "ok" ? "成功" : "失败"}</td><td>${log.latency_ms == null ? "—" : `${log.latency_ms} ms`}</td><td>${escapeHtml(log.error || "—")}</td></tr>`).join("") : `<tr><td colspan="6" class="empty">尚无请求记录</td></tr>`;
}

async function load() {
  state = await api("/api/state");
  $("#lan-url").textContent = state.gateway.lan_url;
  renderSources(); renderCatcher(); renderKeys(); renderLogs();
}

async function checkAllWebSources(force = false, silent = false) {
  if (webHealthRunning) return;
  const targets = state.sources.filter((source) => source.kind === "web" && source.id !== "web-auto" && source.has_credential && (force || source.enabled));
  const button = $("#check-web-sources");
  webHealthRunning = true;
  checkingWebSources = new Set(targets.map((source) => source.id));
  button.disabled = true;
  button.classList.add("checking");
  button.setAttribute("aria-busy", "true");
  button.dataset.tooltip = "正在检测 Web 来源";
  renderSources();
  try {
    const result = await api("/api/web-sources/health", { method: "POST", body: JSON.stringify({ force }) });
    if (!silent) {
      const failures = (result.results || []).filter((item) => item.status !== "healthy").length;
      if (result.running) toast("已有一轮 Web 健康检查正在进行");
      else if (!result.checked) toast("没有需要检测的 Web 来源");
      else toast(`已检测 ${result.checked} 个来源${failures ? `，${failures} 个异常` : "，全部可用"}`, failures > 0);
    }
  } catch (error) {
    if (!silent) toast(`批量健康检查失败：${error.message}`, true);
  } finally {
    checkingWebSources.clear();
    webHealthRunning = false;
    button.disabled = false;
    button.classList.remove("checking");
    button.setAttribute("aria-busy", "false");
    button.dataset.tooltip = "检测全部 Web 来源（会发送测试消息）";
    await load().catch(() => { /* 保留当前界面 */ });
  }
}

function setLogsCollapsed(collapsed, remember = true) {
  const content = $("#logs-content");
  content.classList.toggle("collapsed", collapsed);
  content.setAttribute("aria-hidden", String(collapsed));
  content.inert = collapsed;
  const button = $("#logs-toggle");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "展开请求记录" : "折叠请求记录");
  button.dataset.tooltip = collapsed ? "展开请求记录" : "折叠请求记录";
  button.classList.toggle("collapsed", collapsed);
  if (remember) {
    try { localStorage.setItem("aibridge.logs.collapsed", collapsed ? "1" : "0"); } catch { /* 浏览器禁用存储时保持当前状态 */ }
  }
}

function modelLines(source) {
  return (source?.models || []).map((model) => `${model.public_name} = ${model.upstream_name}`).join("\n");
}

function autoMode() {
  return document.querySelector('input[name="auto-routing-mode"]:checked')?.value || "smart";
}

function autoDispatchMode() {
  return document.querySelector('input[name="auto-dispatch-mode"]:checked')?.value || "priority";
}

function selectedAutoSourceIds() {
  return [...document.querySelectorAll("[data-auto-source]:checked")].map((input) => input.dataset.autoSource);
}

function renderAutoSourcePicker(selectedIds = []) {
  const sources = state.sources.filter((item) => item.kind === "web" && item.id !== "web-auto");
  const selectedSet = new Set(selectedIds);
  const byId = new Map(sources.map((source) => [source.id, source]));
  const ordered = [
    ...selectedIds.map((id) => byId.get(id)).filter(Boolean),
    ...sources.filter((source) => !selectedSet.has(source.id)),
  ];
  $("#auto-source-picker").innerHTML = ordered.map((source) => {
    const selected = selectedSet.has(source.id);
    const selectedIndex = selectedIds.indexOf(source.id);
    const mark = providerMarks[source.id] || `<span>${escapeHtml(source.name.slice(0, 2))}</span>`;
    const stateText = `${source.enabled ? "已启用" : "未启用"} · ${statusLabels[source.health_status] || "异常"}`;
    return `<div class="route-source ${selected ? "selected" : ""}" data-route-source="${escapeHtml(source.id)}">
      <label class="route-source-main"><input type="checkbox" data-auto-source="${escapeHtml(source.id)}" ${selected ? "checked" : ""}><span class="provider-icon">${mark}</span><span class="route-source-copy"><strong>${escapeHtml(source.name)}</strong><span class="route-source-state">${escapeHtml(stateText)}</span></span></label>
      <span class="route-order"><button type="button" data-route-move="up" aria-label="上移 ${escapeHtml(source.name)}" ${!selected || selectedIndex === 0 ? "disabled" : ""}>↑</button><button type="button" data-route-move="down" aria-label="下移 ${escapeHtml(source.name)}" ${!selected || selectedIndex === selectedIds.length - 1 ? "disabled" : ""}>↓</button></span>
    </div>`;
  }).join("");
  $("#auto-source-count").textContent = `${selectedIds.length} 个`;
}

function syncAutoRoutingFields() {
  const custom = autoMode() === "custom";
  $("#auto-custom-routing").hidden = !custom;
}

function openSource(source = null) {
  const isWeb = source?.kind === "web";
  const isAuto = source?.id === "web-auto";
  $("#source-id").value = source?.id || "";
  $("#source-kind").value = source?.kind || "api";
  $("#dialog-title").textContent = source ? `配置 ${source.name}` : "添加标准 API";
  $("#dialog-kicker").textContent = isWeb ? "WEB SOURCE" : "API SOURCE";
  $("#source-name").value = source?.name || "";
  $("#source-protocol").value = source?.protocol === "openai" ? "openai" : "anthropic";
  $("#source-base").value = source?.base_url || "";
  $("#source-api-key").value = "";
  $("#source-auth").value = source?.config?.auth_mode || "bearer";
  $("#source-curl").value = "";
  $("#source-cookie").value = "";
  $("#source-models").value = modelLines(source);
  $("#source-icon-svg").value = source?.config?.icon_svg || "";
  $("#web-public-name").value = source?.models?.[0]?.public_name || "";
  $("#web-max-concurrency").value = source?.config?.max_concurrency || 3;
  const routingMode = source?.config?.routing_mode === "custom" ? "custom" : "smart";
  const modeInput = document.querySelector(`input[name="auto-routing-mode"][value="${routingMode}"]`);
  if (modeInput) modeInput.checked = true;
  const dispatchMode = source?.config?.dispatch_mode === "balanced" ? "balanced" : "priority";
  const dispatchInput = document.querySelector(`input[name="auto-dispatch-mode"][value="${dispatchMode}"]`);
  if (dispatchInput) dispatchInput.checked = true;
  renderAutoSourcePicker(source?.config?.source_ids || []);
  syncAutoRoutingFields();
  $("#web-fields").hidden = !isWeb;
  $("#auto-routing-fields").hidden = !isAuto;
  $("#web-credential-fields").hidden = isAuto;
  $("#web-concurrency-wrap").hidden = isAuto;
  $("#name-wrap").hidden = isWeb;
  $("#protocol-wrap").hidden = isWeb;
  $("#base-wrap").hidden = isAuto;
  $("#api-key-wrap").hidden = isWeb;
  $("#auth-wrap").hidden = isWeb;
  $("#api-icon-editor").hidden = isWeb;
  $("#api-model-editor").hidden = isWeb;
  $("#base-label").textContent = isWeb ? "官网地址" : "上游 Base URL";
  $("#source-base").placeholder = isWeb ? "https://www.example.com" : "https://api.example.com";
  const guide = source?.config?.guide || "";
  $("#web-guide").textContent = guide;
  $("#source-curl").placeholder = guide ? `${guide}\n\n完整粘贴到这里` : "完整粘贴 Copy as cURL (bash)";
  $("#model-help").textContent = "每行：公开名 = 上游模型名";
  $("#source-dialog").showModal();
}

function parseModels(text, existing = []) {
  return text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line, index) => {
    const [publicName, ...rest] = line.split("=");
    const upstream = rest.join("=").trim() || publicName.trim();
    const old = existing.find((x) => x.public_name === publicName.trim()) || existing[index];
    return { id: old?.id, public_name: publicName.trim(), upstream_name: upstream, enabled: true };
  });
}

$("#source-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitButton = event.submitter || event.currentTarget.querySelector('button[type="submit"]');
  const submitLabel = submitButton.textContent;
  const id = $("#source-id").value;
  const kind = $("#source-kind").value;
  const old = state.sources.find((x) => x.id === id);
  const body = { name: $("#source-name").value.trim(), kind, enabled: old?.enabled ?? true };
  if (kind === "web") {
    const previous = old.models[0];
    const publicName = $("#web-public-name").value.trim();
    if (!publicName) return toast("请填写对外模型名", true);
    const isAuto = old.id === "web-auto";
    const routingMode = autoMode();
    const dispatchMode = autoDispatchMode();
    const sourceIds = selectedAutoSourceIds();
    if (isAuto && routingMode === "custom" && !sourceIds.length) return toast("自定义路由至少选择一个来源", true);
    const maxConcurrency = Math.max(1, Math.min(32, Number.parseInt($("#web-max-concurrency").value, 10) || 3));
    Object.assign(body, {
      name: old.name,
      enabled: old.enabled,
      protocol: old.protocol,
      base_url: isAuto ? "" : $("#source-base").value.trim(),
      config: isAuto ? { ...old.config, routing_mode: routingMode, dispatch_mode: dispatchMode, source_ids: sourceIds } : { ...old.config, max_concurrency: maxConcurrency },
      model: { id: previous?.id, public_name: publicName, upstream_name: previous?.upstream_name || (isAuto ? "auto" : "default"), enabled: true },
    });
    if (!isAuto) {
      body.curl = $("#source-curl").value.trim();
      body.credential = { cookie: $("#source-cookie").value.trim() };
    }
  } else {
    const models = parseModels($("#source-models").value, old?.models || []);
    if (!models.length) return toast("至少配置一个模型映射", true);
    Object.assign(body, {
      protocol: $("#source-protocol").value,
      base_url: $("#source-base").value.trim(),
      config: {
        ...(old?.config || {}),
        auth_mode: $("#source-auth").value,
        anthropic_version: "2023-06-01",
        icon_svg: $("#source-icon-svg").value.trim(),
      },
      models,
    });
    const key = $("#source-api-key").value.trim();
    if (key) body.credential = { api_key: key };
  }
  submitButton.disabled = true;
  submitButton.textContent = "保存并检查…";
  let saved;
  try {
    saved = await api(id ? `/api/sources/${id}` : "/api/sources", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    $("#source-dialog").close();
    toast("配置已保存，正在检查连接…");
  } catch (error) {
    toast(error.message, true);
    submitButton.disabled = false;
    submitButton.textContent = submitLabel;
    return;
  }

  const savedId = id || saved.id;
  try {
    const result = await api(`/api/sources/${savedId}/health`, { method: "POST", body: "{}" });
    toast(`${body.name}：${result.message}`, result.status !== "healthy");
  } catch (error) {
    toast(`配置已保存，但健康检查失败：${error.message}`, true);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = submitLabel;
    await load().catch((error) => toast(`状态刷新失败：${error.message}`, true));
  }
});

document.querySelectorAll('input[name="auto-routing-mode"]').forEach((input) => input.addEventListener("change", syncAutoRoutingFields));
$("#auto-source-picker").addEventListener("change", (event) => {
  if (!event.target.matches("[data-auto-source]")) return;
  renderAutoSourcePicker(selectedAutoSourceIds());
});
$("#auto-source-picker").addEventListener("click", (event) => {
  const button = event.target.closest("[data-route-move]");
  if (!button) return;
  const sourceId = button.closest("[data-route-source]").dataset.routeSource;
  const selected = selectedAutoSourceIds();
  const index = selected.indexOf(sourceId);
  const target = button.dataset.routeMove === "up" ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= selected.length) return;
  [selected[index], selected[target]] = [selected[target], selected[index]];
  renderAutoSourcePicker(selected);
});

document.addEventListener("click", async (event) => {
  const close = event.target.closest("[data-close]");
  if (close) return $("#source-dialog").close();
  if (event.target.closest("[data-close-token]")) return $("#token-dialog").close();
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    await navigator.clipboard.writeText(document.getElementById(copy.dataset.copy).textContent);
    return toast("已复制到剪贴板");
  }
  const keyDelete = event.target.closest("[data-key-delete]");
  if (keyDelete) {
    if (!confirm("确定删除这个 Token？使用它的客户端会立即失效。")) return;
    await api(`/api/keys/${keyDelete.dataset.keyDelete}`, { method: "DELETE" });
    return load();
  }
  const catcherRevoke = event.target.closest("[data-catcher-revoke]");
  if (catcherRevoke) {
    if (!confirm("撤销后，该扩展必须重新配对才能同步。确定继续？")) return;
    await api(`/api/catcher/clients/${catcherRevoke.dataset.catcherRevoke}`, { method: "DELETE" });
    toast("扩展配对已撤销");
    return load();
  }
  const sourceEnable = event.target.closest("[data-source-enable]");
  if (sourceEnable) {
    if (!state.permissions?.source_toggle) return toast("来源开关仅可在本机 127.0.0.1 操作", true);
    const source = state.sources.find((x) => x.id === sourceEnable.dataset.sourceEnable);
    if (!source) return;
    sourceEnable.disabled = true;
    try {
      await api(`/api/sources/${source.id}/enabled`, { method: "PATCH", body: JSON.stringify({ enabled: !source.enabled }) });
      toast(`${source.name}已${source.enabled ? "停用" : "启用"}`);
      return load();
    } catch (error) {
      sourceEnable.disabled = false;
      return toast(error.message, true);
    }
  }
  const card = event.target.closest("[data-source]");
  const action = event.target.closest("[data-action]")?.dataset.action;
  if (!card || !action) return;
  const source = state.sources.find((x) => x.id === card.dataset.source);
  if (action === "edit") return openSource(source);
  if (action === "delete") {
    if (!confirm(`确定删除“${source.name}”及其全部模型映射？`)) return;
    await api(`/api/sources/${source.id}`, { method: "DELETE" });
    toast("来源已删除"); return load();
  }
  const button = event.target.closest("button");
  button.disabled = true;
  try {
    if (action === "health") {
      const result = await api(`/api/sources/${source.id}/health`, { method: "POST", body: "{}" });
      toast(`${source.name}：${result.message}`, result.status !== "healthy");
    } else if (action === "discover") {
      const result = await api(`/api/sources/${source.id}/discover-models`, { method: "POST", body: "{}" });
      toast(`发现 ${result.models.length} 个模型；打开配置后可添加公开映射。`);
    }
    await load();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

$("#add-api").addEventListener("click", () => openSource());
$("#catcher-toggle").addEventListener("click", async () => {
  try {
    await saveCatcherSettings({ enabled: !state.catcher?.enabled });
    toast(`浏览器同步已${state.catcher?.enabled ? "开启" : "关闭"}`);
  } catch (error) { toast(error.message, true); }
});
$("#catcher-pair").addEventListener("click", async () => {
  try {
    const result = await api("/api/catcher/pairing", { method: "POST", body: "{}" });
    $("#catcher-pairing-code").textContent = result.code;
    $("#catcher-pairing").hidden = false;
    toast("配对码已生成，十分钟内有效");
    await load();
  } catch (error) { toast(error.message, true); }
});
$("#catcher-auto-enable").addEventListener("change", async () => {
  try {
    await saveCatcherSettings();
    toast("自动启用策略已保存");
  } catch (error) { toast(error.message, true); }
});
$("#catcher-sources").addEventListener("change", async (event) => {
  if (!event.target.matches("[data-catcher-source]")) return;
  try {
    await saveCatcherSettings();
    toast("同步范围已更新");
  } catch (error) { toast(error.message, true); }
});
$("#refresh").addEventListener("click", () => load().then(() => toast("状态已刷新")).catch((error) => toast(error.message, true)));
$("#check-web-sources").addEventListener("click", () => checkAllWebSources(true));
$("#logs-toggle").addEventListener("click", () => setLogsCollapsed(!$("#logs-content").classList.contains("collapsed")));
$("#create-key").addEventListener("click", async () => {
  try {
    const result = await api("/api/keys", { method: "POST", body: JSON.stringify({ name: $("#key-name").value.trim() || "本地 Token" }) });
    $("#new-token").textContent = result.token;
    $("#token-dialog").showModal();
    $("#key-name").value = "";
    await load();
  } catch (error) { toast(error.message, true); }
});

let initialLogsCollapsed = false;
try { initialLogsCollapsed = localStorage.getItem("aibridge.logs.collapsed") === "1"; } catch { /* 浏览器禁用存储时默认展开 */ }
setLogsCollapsed(initialLogsCollapsed, false);
load().catch((error) => toast(`无法加载控制台：${error.message}`, true));
setInterval(() => {
  if (document.hidden || $("#source-dialog").open || $("#token-dialog").open) return;
  load().catch(() => { /* 后台刷新失败时保留当前界面 */ });
}, 3000);
