import express from 'express';
import { randomUUID } from 'crypto';
import { networkInterfaces } from 'os';

function asyncHandler(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
}

function toBool(value, fallback = true) {
  if (value === undefined || value === null || value === '') return fallback;
  if (typeof value === 'boolean') return value;
  return value === 'true' || value === '1' || value === 1;
}

function getLocalIpv4() {
  const addresses = Object.values(networkInterfaces())
    .flat()
    .filter(item => item && item.family === 'IPv4' && !item.internal)
    .map(item => item.address);
  const privateAddress = addresses.find(address =>
    /^192\.168\./.test(address)
    || /^10\./.test(address)
    || /^172\.(1[6-9]|2\d|3[0-1])\./.test(address)
  );
  return privateAddress || addresses[0] || '127.0.0.1';
}

function adminServerInfo(configStore) {
  const server = configStore.getServer();
  const port = server.port || 9529;
  const localIp = getLocalIpv4();
  return {
    ...server,
    port,
    localIp,
    localOrigin: `http://${localIp}:${port}`,
  };
}

export function createAdminRouter({ configStore, keyStore, taskLog, playwrightDriver }) {
  const router = express.Router();

  router.get('/overview', (_req, res) => {
    const providers = configStore.listProviders();
    res.json({
      server: adminServerInfo(configStore),
      providers,
      routes: configStore.listRoutes(),
      keys: keyStore.list(),
      sessions: playwrightDriver.listStatuses(providers),
      recentTasks: taskLog.recent(25),
    });
  });

  router.get('/providers', (_req, res) => {
    const providers = configStore.listProviders();
    res.json({ providers, sessions: playwrightDriver.listStatuses(providers) });
  });

  router.post('/providers', (req, res) => {
    const provider = configStore.saveProvider({ ...req.body, id: req.body.id || `provider-${randomUUID().slice(0, 8)}` });
    res.json({ provider });
  });

  router.patch('/providers/:id', (req, res) => {
    const provider = configStore.updateProvider(req.params.id, req.body);
    if (!provider) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Provider not found' } });
    res.json({ provider });
  });

  router.post('/providers/:id/check', asyncHandler(async (req, res) => {
    const provider = configStore.getProvider(req.params.id);
    if (!provider) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Provider not found' } });
    const result = await playwrightDriver.check(provider, { visible: toBool(req.body.visible, false) });
    res.json(result);
  }));

  router.post('/providers/:id/open-session', asyncHandler(async (req, res) => {
    const provider = configStore.getProvider(req.params.id);
    if (!provider) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Provider not found' } });
    const status = await playwrightDriver.openSession(provider);
    res.json({ status });
  }));

  router.post('/providers/:id/close-session', asyncHandler(async (req, res) => {
    await playwrightDriver.closeSession(req.params.id);
    res.json({ ok: true });
  }));

  router.post('/providers/:id/reset-session', asyncHandler(async (req, res) => {
    const provider = configStore.getProvider(req.params.id);
    if (!provider) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Provider not found' } });
    const result = await playwrightDriver.resetSession(provider);
    res.json(result);
  }));

  router.get('/routes', (_req, res) => {
    res.json({ routes: configStore.listRoutes(), providers: configStore.listProviders() });
  });

  router.post('/routes', (req, res) => {
    const route = configStore.saveRoute({ ...req.body, id: req.body.id || `route-${randomUUID().slice(0, 8)}` });
    res.json({ route });
  });

  router.patch('/routes/:id', (req, res) => {
    const route = configStore.updateRoute(req.params.id, req.body);
    if (!route) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Route not found' } });
    res.json({ route });
  });

  router.delete('/routes/:id', (req, res) => {
    res.json({ ok: configStore.deleteRoute(req.params.id) });
  });

  router.post('/routes/preview', (req, res) => {
    const model = req.body.model || '';
    const routes = configStore.listRoutes()
      .filter(route => route.enabled !== false)
      .sort((a, b) => Number(b.priority || 0) - Number(a.priority || 0));
    const matched = routes.find(route => {
      if (route.type === 'exact') return model === route.pattern;
      if (route.type === 'regex') {
        try { return new RegExp(route.pattern, 'i').test(model); } catch { return false; }
      }
      return String(model).toLowerCase().includes(String(route.pattern).toLowerCase());
    });
    res.json({ route: matched || null, provider: matched ? configStore.getProvider(matched.providerId) : null });
  });

  router.get('/keys', (_req, res) => {
    res.json({ keys: keyStore.list() });
  });

  router.post('/keys', (req, res) => {
    res.json(keyStore.create(req.body));
  });

  router.patch('/keys/:id', (req, res) => {
    const key = keyStore.update(req.params.id, req.body);
    if (!key) return res.status(404).json({ error: { code: 'NOT_FOUND', message: 'Key not found' } });
    res.json({ key });
  });

  router.delete('/keys/:id', (req, res) => {
    res.json({ ok: keyStore.delete(req.params.id) });
  });

  router.get('/tasks', (req, res) => {
    res.json({ tasks: taskLog.recent(Number(req.query.limit || 100)) });
  });

  router.use((error, _req, res, _next) => {
    res.status(500).json({ error: { code: error.code || 'INTERNAL_ERROR', message: error.message } });
  });

  return router;
}
