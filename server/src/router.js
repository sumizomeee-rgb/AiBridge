function matchRoute(route, model) {
  const value = String(model || '');
  const pattern = String(route.pattern || '');
  if (!pattern) return false;
  if (route.type === 'exact') return value === pattern;
  if (route.type === 'regex') {
    try {
      return new RegExp(pattern, 'i').test(value);
    } catch {
      return false;
    }
  }
  return value.toLowerCase().includes(pattern.toLowerCase());
}

export function resolveRoute(configStore, model) {
  const routes = configStore.listRoutes()
    .filter(route => route.enabled !== false)
    .sort((a, b) => Number(b.priority || 0) - Number(a.priority || 0));

  const matched = routes.find(route => matchRoute(route, model));
  const providerId = matched?.providerId || 'gpt';
  const provider = configStore.getProvider(providerId) || configStore.listProviders()[0];
  return { route: matched || null, provider };
}
