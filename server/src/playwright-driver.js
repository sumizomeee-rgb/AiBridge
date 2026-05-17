import { existsSync, mkdirSync, rmSync } from 'fs';
import { chromium } from 'playwright';
import { profilePath, PROFILE_ROOT } from './paths.js';
import { GenericWebAdapter } from './adapters/generic-web-adapter.js';

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function normalizeProxyServer(value) {
  const server = String(value || '').trim();
  if (!server) return '';
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(server)) return server;
  return `http://${server}`;
}

function isNavigationRace(error) {
  return /Execution context was destroyed|Cannot find context|navigation/i.test(error.message || '');
}

export class PlaywrightDriver {
  constructor() {
    this.sessions = new Map();
    this.queues = new Map();
    this.adapter = new GenericWebAdapter();
    mkdirSync(PROFILE_ROOT, { recursive: true });
  }

  getStatus(providerId) {
    const session = this.sessions.get(providerId);
    if (!session) {
      return { running: false, visible: false, lastStatus: 'idle', lastError: null, lastCheckedAt: null };
    }
    return {
      running: true,
      visible: session.visible,
      url: session.page?.url?.() || '',
      lastStatus: session.lastStatus || 'running',
      lastError: session.lastError || null,
      lastCheckedAt: session.lastCheckedAt || null,
      lastUsedAt: session.lastUsedAt || null,
    };
  }

  listStatuses(providers) {
    return providers.reduce((acc, provider) => {
      acc[provider.id] = this.getStatus(provider.id);
      return acc;
    }, {});
  }

  async applyProviderCookies(provider, context) {
    if (!provider.sessionId || !provider.cookieSessionName || !provider.baseUrl) return;
    await context.addCookies([{
      name: provider.cookieSessionName,
      value: provider.sessionId,
      url: provider.baseUrl,
      secure: provider.baseUrl.startsWith('https://'),
      sameSite: 'Lax',
    }]);
  }

  async launchContext(dir, provider, visible) {
    const baseOptions = {
      headless: !visible,
      viewport: { width: 1440, height: 960 },
      locale: 'zh-CN',
      args: ['--disable-blink-features=AutomationControlled'],
    };
    const providerProxy = provider.proxyEnabled ? provider.proxyServer : '';
    const proxyServer = normalizeProxyServer(providerProxy || process.env.AIBRIDGE_PROXY_SERVER || '');
    if (proxyServer) {
      baseOptions.proxy = {
        server: proxyServer,
        username: provider.proxyUsername || process.env.AIBRIDGE_PROXY_USERNAME || undefined,
        password: provider.proxyPassword || process.env.AIBRIDGE_PROXY_PASSWORD || undefined,
      };
    }
    const preferred = provider.browserChannel || process.env.AIBRIDGE_BROWSER_CHANNEL || 'chrome';
    const normalizeChannel = (channel) => {
      if (!channel || channel === 'auto' || channel === 'bundled' || channel === 'playwright' || channel === 'chromium') return null;
      return channel;
    };
    const channels = [];
    [normalizeChannel(preferred), null, 'chrome', 'msedge'].forEach((channel) => {
      if (!channels.some(item => item === channel)) channels.push(channel);
    });
    let lastError = null;
    for (const channel of channels) {
      try {
        const options = channel ? { ...baseOptions, channel } : baseOptions;
        return await chromium.launchPersistentContext(dir, options);
      } catch (error) {
        lastError = error;
        if (!/Executable doesn't exist|Chromium distribution/.test(error.message)) throw error;
      }
    }
    throw Object.assign(
      new Error(`未找到可用浏览器。请安装 Google Chrome，或运行 npx playwright install chromium。${lastError ? ` 原始错误：${lastError.message}` : ''}`),
      { code: 'BROWSER_NOT_FOUND' },
    );
  }

  async ensureSession(provider, options = {}) {
    const visible = options.visible || provider.browserMode === 'always_visible';
    const existing = this.sessions.get(provider.id);
    if (existing && (existing.visible || existing.visible === visible || options.reuseExisting)) {
      return existing;
    }
    if (existing) await this.closeSession(provider.id);

    const dir = profilePath(provider.profileDir || `profiles/${provider.id}`);
    mkdirSync(dir, { recursive: true });
    const context = await this.launchContext(dir, provider, visible);
    let page = context.pages()[0];
    if (!page) page = await context.newPage();
    await this.applyProviderCookies(provider, context);
    const session = {
      context,
      page,
      visible,
      lastStatus: 'running',
      lastError: null,
      lastCheckedAt: null,
      lastUsedAt: new Date().toISOString(),
    };
    this.sessions.set(provider.id, session);
    context.on('close', () => {
      if (this.sessions.get(provider.id) === session) this.sessions.delete(provider.id);
    });
    return session;
  }

  enqueue(provider, fn) {
    const previous = this.queues.get(provider.id) || Promise.resolve();
    const next = previous.catch(() => {}).then(fn);
    const cleanup = next.catch(() => {}).finally(() => {
      if (this.queues.get(provider.id) === cleanup) this.queues.delete(provider.id);
    });
    this.queues.set(provider.id, cleanup);
    return next;
  }

  async openSession(provider) {
    const session = await this.ensureSession(provider, { visible: true });
    await this.applyProviderCookies(provider, session.context);
    session.lastStatus = 'visible_session';
    session.lastError = null;
    session.lastUsedAt = new Date().toISOString();
    this.adapter.gotoStart(session.page, provider, false, { waitUntil: 'commit', timeoutMs: 5000 })
      .then(() => {
        session.lastStatus = 'visible_session';
        session.lastError = null;
        session.lastUsedAt = new Date().toISOString();
      })
      .catch((error) => {
        session.lastStatus = 'visible_session';
        session.lastError = `窗口已打开，页面仍在加载或导航超时：${error.message}`;
        session.lastUsedAt = new Date().toISOString();
      });
    return this.getStatus(provider.id);
  }

  async closeSession(providerId) {
    const session = this.sessions.get(providerId);
    if (!session) return false;
    this.sessions.delete(providerId);
    await session.context.close().catch(() => {});
    return true;
  }

  async resetSession(provider) {
    await this.closeSession(provider.id);
    const dir = profilePath(provider.profileDir || `profiles/${provider.id}`);
    if (existsSync(dir)) {
      rmSync(dir, { recursive: true, force: true });
    }
    return { ok: true, profileDir: dir };
  }

  async waitForDetection(page, provider, timeoutMs = 15000) {
    const startedAt = Date.now();
    let latest = null;
    while (Date.now() - startedAt < timeoutMs) {
      try {
        latest = await this.adapter.detect(page);
      } catch (error) {
        if (!isNavigationRace(error)) throw error;
        const url = page.url();
        latest = {
          url,
          title: '',
          inputReady: false,
          inputCount: 0,
          loginHints: false,
          blockedHints: /region-ban|security|blocked|forbidden/i.test(url),
        };
      }
      if (latest.inputReady || latest.blockedHints || latest.loginHints) return latest;
      await sleep(500);
    }
    return latest || await this.adapter.detect(page);
  }

  errorFromDetection(detection) {
    if (detection.inputReady) return null;
    const code = detection.blockedHints ? 'ACCESS_BLOCKED' : (detection.loginHints ? 'NOT_LOGGED_IN' : 'DOM_TIMEOUT');
    const message = detection.blockedHints
      ? '平台安全页或区域限制拦截'
      : (detection.loginHints ? '需要在 Admin 中刷新该平台登录态' : '输入框未就绪');
    return Object.assign(new Error(message), { code });
  }

  async check(provider, options = {}) {
    const startedAt = Date.now();
    const session = await this.ensureSession(provider, {
      visible: options.visible || provider.browserMode === 'always_visible',
    });
    try {
      await this.applyProviderCookies(provider, session.context);
      await this.adapter.gotoStart(session.page, provider, false);
      const detection = await this.waitForDetection(session.page, provider, provider.readyTimeoutMs || 15000);
      session.lastCheckedAt = new Date().toISOString();
      session.lastUsedAt = session.lastCheckedAt;
      session.lastStatus = detection.inputReady
        ? 'available'
        : (detection.blockedHints ? 'blocked' : (detection.loginHints ? 'needs_login' : 'dom_unready'));
      session.lastError = detection.inputReady
        ? null
        : (detection.blockedHints ? '平台安全页或区域限制拦截' : '输入框未就绪，可能需要登录或更新适配器');
      return {
        ok: detection.inputReady,
        status: session.lastStatus,
        elapsedMs: Date.now() - startedAt,
        detection,
      };
    } catch (error) {
      session.lastCheckedAt = new Date().toISOString();
      session.lastStatus = 'error';
      session.lastError = error.message;
      return {
        ok: false,
        status: 'error',
        elapsedMs: Date.now() - startedAt,
        error: error.message,
      };
    }
  }

  async runTask(provider, task) {
    return this.enqueue(provider, async () => {
      const session = await this.ensureSession(provider, {
        visible: provider.browserMode === 'always_visible',
      });
      const startedAt = Date.now();
      try {
        await this.applyProviderCookies(provider, session.context);
        await this.adapter.gotoStart(session.page, provider, task.newChat);
        const detection = await this.waitForDetection(session.page, provider, provider.readyTimeoutMs || 20000);
        if (!detection.inputReady) {
          const code = detection.blockedHints ? 'ACCESS_BLOCKED' : (detection.loginHints ? 'NOT_LOGGED_IN' : 'DOM_TIMEOUT');
          const message = detection.blockedHints
            ? '平台安全页或区域限制拦截'
            : (detection.loginHints ? '需要在 Admin 中刷新该平台登录态' : '输入框未就绪');
          throw Object.assign(new Error(message), { code });
        }
        const baseline = await this.adapter.captureBaseline(session.page);
        const text = task.messages[task.messages.length - 1]?.content || '';
        await this.adapter.sendMessage(session.page, text);
        const fullText = await this.adapter.waitForResponse(
          session.page,
          baseline,
          { onChunk: task.onChunk },
          provider.taskTimeoutMs || 120000,
        );
        session.lastStatus = 'available';
        session.lastError = null;
        session.lastUsedAt = new Date().toISOString();
        return { fullText, elapsedMs: Date.now() - startedAt };
      } catch (error) {
        if (isNavigationRace(error)) {
          try {
            const detection = await this.waitForDetection(session.page, provider, 5000);
            const detectionError = this.errorFromDetection(detection);
            if (detectionError) throw detectionError;
          } catch (detectedError) {
            session.lastStatus = detectedError.code || 'error';
            session.lastError = detectedError.message;
            session.lastUsedAt = new Date().toISOString();
            throw detectedError;
          }
        }
        session.lastStatus = error.code || 'error';
        session.lastError = error.message;
        session.lastUsedAt = new Date().toISOString();
        throw error;
      }
    });
  }

  async shutdown() {
    const closing = Array.from(this.sessions.keys()).map(id => this.closeSession(id));
    await Promise.allSettled(closing);
  }
}
