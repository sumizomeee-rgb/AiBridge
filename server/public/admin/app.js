const $ = (selector) => document.querySelector(selector);
let state = { sources: [], keys: [], logs: [], gateway: {} };

const statusLabels = {
  healthy: "可用", unchecked: "待检测", unconfigured: "未配置", unsupported: "未接入",
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
  "web-doubao": `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M9 9.5c2.2-2.7 5.1-4 8.3-3.6 4.9.6 8.4 5.1 7.7 10-.8 5.8-6.6 9.7-12.2 7.7-4.4-1.6-6.9-6.4-5.4-10.8"/><path d="M8.5 8.2 7.4 13l4.8-.8M12 17.5c1.4 1.4 3.4 2 5.4 1.4 1.2-.3 2.2-1.1 2.8-2.1"/></svg>`,
  "web-qwen": `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="m16 4 3.7 4.2 5.5.6.6 5.5 4.2 3.7-4.2 3.7-.6 5.5-5.5.6L16 28l-3.7-4.2-5.5-.6-.6-5.5L2 14l4.2-3.7.6-5.5 5.5-.6Z"/><circle cx="16" cy="16" r="4.2"/></svg>`,
  "web-deepseek": `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 17.5c3.1 1.1 5.7.8 7.8-.9 1.5 2 3.7 3.1 6.5 3.1 3.2 0 5.8-1.3 7.7-4.1-.1 6.7-4.5 11.2-11.1 11.2C9.8 26.8 5.6 23.2 5 17.5Z"/><path d="M18.8 8.2c2.6.1 4.5 1.2 5.6 3.4-2.4 1.1-4.6 1-6.5-.3-1.3-.9-2.2-2.1-2.8-3.7 1.2.4 2.4.6 3.7.6ZM8.3 13.1c1.3-2 3.1-3.2 5.6-3.5"/></svg>`,
  "web-yuanbao": `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M7 11.5 11 6h10l4 5.5-2 13H9Z"/><path d="M11.5 12.5c1 2 2.5 3 4.5 3s3.5-1 4.5-3M12 21h8"/></svg>`,
  "web-kimi": `<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M21.5 5.5A10.8 10.8 0 1 0 26 22.8 11.5 11.5 0 0 1 21.5 5.5Z"/><path d="m22.5 10 .8 1.7L25 12.5l-1.7.8-.8 1.7-.8-1.7-1.7-.8 1.7-.8Z"/></svg>`,
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

function sourceCard(source) {
  const statusClass = statusLabels[source.health_status] ? source.health_status : "error";
  const initials = source.name.replace(/\s*Web|\s*官方/g, "").slice(0, 2).toUpperCase();
  const fallbackMark = `<span>${escapeHtml(initials)}</span>`;
  const providerMark = source.kind === "web" ? (providerMarks[source.id] || fallbackMark) : apiProviderMark(source, fallbackMark);
  const models = source.models.filter((x) => x.enabled).map((x) => `<span class="model-tag">${escapeHtml(x.public_name)}</span>`).join("") || `<span class="muted">暂无公开模型</span>`;
  const type = source.kind === "web" ? "官网直连" : `${source.protocol.toUpperCase()} 兼容`;
  const actions = [iconButton("health", "activity", "测试连接"), iconButton("edit", "settings", "配置来源")];
  if (source.kind === "api" && source.protocol === "openai") actions.push(iconButton("discover", "sync", "同步模型"));
  if (source.kind === "api") actions.push(iconButton("delete", "trash", "删除来源", "danger"));
  return `<article class="source-row ${source.enabled ? "" : "disabled"}" data-source="${escapeHtml(source.id)}">
    <div class="source-identity"><div class="provider-icon">${providerMark}</div><div><strong>${escapeHtml(source.name)}</strong><small>${escapeHtml(type)} · ${escapeHtml(source.base_url)}</small></div></div>
    <div class="models">${models}</div>
    <div class="source-health"><span class="status ${statusClass}"><i></i>${escapeHtml(statusLabels[source.health_status] || "异常")}</span><span class="health-copy">${escapeHtml(source.health_message || "尚未检测")}</span>${source.last_checked_at ? `<time>${escapeHtml(formatTime(source.last_checked_at))}</time>` : ""}</div>
    <div class="row-actions">${actions.join("")}</div>
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
  renderSources(); renderKeys(); renderLogs();
}

function modelLines(source) {
  return (source?.models || []).map((model) => `${model.public_name} = ${model.upstream_name}`).join("\n");
}

function openSource(source = null) {
  const isWeb = source?.kind === "web";
  $("#source-id").value = source?.id || "";
  $("#source-kind").value = source?.kind || "api";
  $("#dialog-title").textContent = source ? `配置 ${source.name}` : "添加标准 API";
  $("#dialog-kicker").textContent = isWeb ? "WEB SOURCE" : "API SOURCE";
  $("#source-name").value = source?.name || "";
  $("#source-protocol").value = source?.protocol === "openai" ? "openai" : "anthropic";
  $("#source-base").value = source?.base_url || "";
  $("#source-api-key").value = "";
  $("#source-auth").value = source?.config?.auth_mode || "bearer";
  $("#source-enabled").checked = source?.enabled ?? true;
  $("#source-curl").value = "";
  $("#source-cookie").value = "";
  $("#source-models").value = modelLines(source);
  $("#source-icon-svg").value = source?.config?.icon_svg || "";
  $("#web-public-name").value = source?.models?.[0]?.public_name || "";
  $("#web-fields").hidden = !isWeb;
  $("#name-wrap").hidden = isWeb;
  $("#protocol-wrap").hidden = isWeb;
  $("#base-wrap").hidden = false;
  $("#api-key-wrap").hidden = isWeb;
  $("#auth-wrap").hidden = isWeb;
  $("#enabled-wrap").hidden = isWeb;
  $("#api-icon-editor").hidden = isWeb;
  $("#api-model-editor").hidden = isWeb;
  $("#base-label").textContent = isWeb ? "官网地址" : "上游 Base URL";
  $("#source-base").placeholder = isWeb ? "https://www.example.com" : "https://api.example.com";
  $("#web-guide").textContent = source?.config?.guide || "";
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
  const id = $("#source-id").value;
  const kind = $("#source-kind").value;
  const old = state.sources.find((x) => x.id === id);
  const body = { name: $("#source-name").value.trim(), kind, enabled: $("#source-enabled").checked };
  if (kind === "web") {
    const previous = old.models[0];
    const publicName = $("#web-public-name").value.trim();
    if (!publicName) return toast("请填写对外模型名", true);
    Object.assign(body, { name: old.name, enabled: old.enabled, protocol: old.protocol, base_url: $("#source-base").value.trim(), config: old.config, curl: $("#source-curl").value.trim(), credential: { cookie: $("#source-cookie").value.trim() }, model: { id: previous?.id, public_name: publicName, upstream_name: previous?.upstream_name || "default", enabled: true } });
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
  try {
    await api(id ? `/api/sources/${id}` : "/api/sources", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    $("#source-dialog").close();
    toast("来源配置已保存");
    await load();
  } catch (error) { toast(error.message, true); }
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
$("#refresh").addEventListener("click", () => load().then(() => toast("状态已刷新")).catch((error) => toast(error.message, true)));
$("#create-key").addEventListener("click", async () => {
  try {
    const result = await api("/api/keys", { method: "POST", body: JSON.stringify({ name: $("#key-name").value.trim() || "本地 Token" }) });
    $("#new-token").textContent = result.token;
    $("#token-dialog").showModal();
    $("#key-name").value = "";
    await load();
  } catch (error) { toast(error.message, true); }
});

load().catch((error) => toast(`无法加载控制台：${error.message}`, true));
