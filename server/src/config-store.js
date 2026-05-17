import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { randomUUID } from 'crypto';
import { resolve } from 'path';
import { DATA_DIR, SERVER_ROOT } from './paths.js';

const defaultConfigPath = resolve(DATA_DIR, 'config.json');
const exampleConfigPath = resolve(DATA_DIR, 'config.example.json');
const legacyConfigPath = resolve(SERVER_ROOT, 'config.json');

function readJson(path, fallback = null) {
  try {
    return JSON.parse(readFileSync(path, 'utf-8'));
  } catch {
    return fallback;
  }
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function normalizeProvider(provider) {
  const id = String(provider.id || provider.name || randomUUID()).trim();
  const baseUrl = String(provider.baseUrl || provider.url || '').replace(/\/$/, '');
  const sessionId = String(provider.sessionId || '').trim();
  const startUrl = String(provider.startUrl || '').trim();

  return {
    id,
    name: provider.name || id,
    baseUrl,
    startUrl,
    sessionId,
    cookieSessionName: provider.cookieSessionName || (id === 'doubao' ? 'sessionId' : ''),
    adapter: provider.adapter || 'generic',
    driver: 'playwright',
    browserChannel: provider.browserChannel || '',
    browserMode: provider.browserMode || 'headless',
    proxyEnabled: provider.proxyEnabled === true,
    proxyServer: provider.proxyServer || '',
    proxyUsername: provider.proxyUsername || '',
    proxyPassword: provider.proxyPassword || '',
    enabled: provider.enabled !== false,
    profileDir: provider.profileDir || `profiles/${id}`,
    healthPrompt: provider.healthPrompt || '请只回复 ok',
    readyTimeoutMs: Number(provider.readyTimeoutMs || 20000),
    taskTimeoutMs: Number(provider.taskTimeoutMs || 120000),
    idleTtlMs: Number(provider.idleTtlMs || 600000),
    newChat: provider.newChat === true,
  };
}

function normalizeRoute(route) {
  return {
    id: route.id || `route-${randomUUID().slice(0, 8)}`,
    type: route.type || 'contains',
    pattern: String(route.pattern || ''),
    providerId: String(route.providerId || ''),
    priority: Number(route.priority || 0),
    enabled: route.enabled !== false,
    newChat: route.newChat === true,
    fallbackProviderIds: Array.isArray(route.fallbackProviderIds) ? route.fallbackProviderIds : [],
  };
}

export class ConfigStore {
  constructor(path = defaultConfigPath) {
    this.path = path;
    this.data = null;
    this.load();
  }

  load() {
    mkdirSync(DATA_DIR, { recursive: true });
    const legacy = readJson(legacyConfigPath, {});
    if (!existsSync(this.path) && existsSync(exampleConfigPath)) {
      writeFileSync(this.path, readFileSync(exampleConfigPath, 'utf-8'), 'utf-8');
    }
    const base = readJson(this.path, null);
    if (!base) {
      throw new Error(`Missing config file: ${this.path}`);
    }

    this.data = {
      ...base,
      server: { ...(base.server || {}), ...(legacy.server || {}) },
      auth: { ...(base.auth || {}), ...(legacy.auth || {}) },
      providers: (base.providers || []).map(normalizeProvider),
      routes: (base.routes || []).map(normalizeRoute),
      apiKeys: Array.isArray(base.apiKeys) ? base.apiKeys : [],
      taskLogLimit: Number(base.taskLogLimit || 200),
    };
    return this.get();
  }

  save() {
    writeFileSync(this.path, `${JSON.stringify(this.data, null, 2)}\n`, 'utf-8');
  }

  get() {
    return clone(this.data);
  }

  getServer() {
    return this.get().server || {};
  }

  getAuth() {
    return this.get().auth || {};
  }

  listProviders() {
    return this.get().providers || [];
  }

  getProvider(id) {
    return this.listProviders().find(provider => provider.id === id);
  }

  saveProvider(input) {
    const provider = normalizeProvider(input);
    const providers = this.data.providers || [];
    const index = providers.findIndex(item => item.id === provider.id);
    if (index >= 0) providers[index] = { ...providers[index], ...provider };
    else providers.push(provider);
    this.save();
    return provider;
  }

  updateProvider(id, patch) {
    const providers = this.data.providers || [];
    const index = providers.findIndex(item => item.id === id);
    if (index < 0) return null;
    providers[index] = normalizeProvider({ ...providers[index], ...patch, id });
    this.save();
    return clone(providers[index]);
  }

  listRoutes() {
    return this.get().routes || [];
  }

  saveRoute(input) {
    const route = normalizeRoute(input);
    const routes = this.data.routes || [];
    const index = routes.findIndex(item => item.id === route.id);
    if (index >= 0) routes[index] = { ...routes[index], ...route };
    else routes.push(route);
    this.save();
    return route;
  }

  updateRoute(id, patch) {
    const routes = this.data.routes || [];
    const index = routes.findIndex(item => item.id === id);
    if (index < 0) return null;
    routes[index] = normalizeRoute({ ...routes[index], ...patch, id });
    this.save();
    return clone(routes[index]);
  }

  deleteRoute(id) {
    const before = this.data.routes.length;
    this.data.routes = this.data.routes.filter(route => route.id !== id);
    this.save();
    return this.data.routes.length !== before;
  }

  listApiKeys() {
    return this.get().apiKeys || [];
  }

  setApiKeys(keys) {
    this.data.apiKeys = keys;
    this.save();
  }
}
