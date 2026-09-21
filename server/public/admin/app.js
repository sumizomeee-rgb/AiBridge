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

function sourceCard(source) {
  const statusClass = statusLabels[source.health_status] ? source.health_status : "error";
  const initials = source.name.replace(/\s*Web|\s*官方/g, "").slice(0, 2).toUpperCase();
  const models = source.models.filter((x) => x.enabled).map((x) => `<span class="model-tag">${escapeHtml(x.public_name)}</span>`).join("") || `<span class="muted">暂无公开模型</span>`;
  return `<article class="source-card ${source.enabled ? "" : "disabled"}" data-source="${escapeHtml(source.id)}">
    <div class="card-top"><div class="card-title"><div class="provider-icon">${escapeHtml(initials)}</div><div><strong>${escapeHtml(source.name)}</strong><small>${source.kind === "web" ? "官网直连" : source.protocol.toUpperCase() + " COMPATIBLE"}</small></div></div><span class="status ${statusClass}">${escapeHtml(statusLabels[source.health_status] || "异常")}</span></div>
    <p class="card-message">${escapeHtml(source.health_message || "尚未检测")}${source.last_checked_at ? `<br><small>${escapeHtml(formatTime(source.last_checked_at))}</small>` : ""}</p>
    <div class="models">${models}</div>
    <div class="card-actions"><button data-action="health">健康测试</button><button data-action="edit">配置</button>${source.kind === "api" && source.protocol === "openai" ? `<button data-action="discover">同步模型</button>` : ""}${source.kind === "api" ? `<button class="danger" data-action="delete">删除</button>` : ""}</div>
  </article>`;
}

function renderSources() {
  const web = state.sources.filter((x) => x.kind === "web");
  const apis = state.sources.filter((x) => x.kind === "api");
  $("#web-sources").innerHTML = web.map(sourceCard).join("") || `<div class="empty">没有 Web 来源</div>`;
  $("#api-sources").innerHTML = apis.map(sourceCard).join("") || `<div class="empty">还没有标准 API，点击右上角添加。</div>`;
}

function renderKeys() {
  $("#keys").innerHTML = state.keys.length ? state.keys.map((key) => `<div class="list-row"><div><strong>${escapeHtml(key.name)}</strong><br><code>${escapeHtml(key.prefix)}••••••••</code></div><small>${key.last_used_at ? `最后使用 ${escapeHtml(formatTime(key.last_used_at))}` : "尚未使用"}</small><button class="danger" data-key-delete="${escapeHtml(key.id)}">删除</button></div>`).join("") : `<div class="empty">尚未生成网关 Token</div>`;
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
  $("#local-url").textContent = state.gateway.local_url;
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
  $("#web-fields").hidden = !isWeb;
  $("#protocol-wrap").hidden = isWeb;
  $("#base-wrap").hidden = isWeb;
  $("#api-key-wrap").hidden = isWeb;
  $("#auth-wrap").hidden = isWeb;
  $("#web-guide").textContent = source?.config?.guide || "";
  $("#model-help").textContent = isWeb ? "Web 来源只使用第一行；公开名可改，上游名通常保持不变" : "每行：公开名 = 上游模型名";
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
  const models = parseModels($("#source-models").value, old?.models || []);
  if (!models.length) return toast("至少配置一个模型映射", true);
  const body = { name: $("#source-name").value.trim(), kind, enabled: $("#source-enabled").checked };
  if (kind === "web") {
    Object.assign(body, { protocol: old.protocol, base_url: old.base_url, config: old.config, curl: $("#source-curl").value.trim(), credential: { cookie: $("#source-cookie").value.trim() }, model: models[0] });
  } else {
    Object.assign(body, { protocol: $("#source-protocol").value, base_url: $("#source-base").value.trim(), config: { auth_mode: $("#source-auth").value, anthropic_version: "2023-06-01" }, models });
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
