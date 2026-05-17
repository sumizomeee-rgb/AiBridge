import { createHash, randomBytes, randomUUID } from 'crypto';

function hashKey(key) {
  return createHash('sha256').update(key).digest('hex');
}

function publicKeyRecord(record) {
  const { hash, ...rest } = record;
  return rest;
}

export class KeyStore {
  constructor(configStore) {
    this.configStore = configStore;
  }

  list() {
    return this.configStore.listApiKeys().map(publicKeyRecord);
  }

  create(input = {}) {
    const raw = `sk-aibridge-${randomBytes(24).toString('hex')}`;
    const now = new Date().toISOString();
    const record = {
      id: `key-${randomUUID().slice(0, 8)}`,
      name: input.name || '本地 Key',
      prefix: raw.slice(0, 18),
      hash: hashKey(raw),
      enabled: input.enabled !== false,
      scopes: Array.isArray(input.scopes) ? input.scopes : ['all'],
      rateLimitPerMinute: Number(input.rateLimitPerMinute || 0),
      createdAt: now,
      lastUsedAt: null,
      lastUsedIp: null,
    };
    const keys = this.configStore.listApiKeys();
    keys.push(record);
    this.configStore.setApiKeys(keys);
    return { key: raw, record: publicKeyRecord(record) };
  }

  update(id, patch = {}) {
    const keys = this.configStore.listApiKeys();
    const index = keys.findIndex(key => key.id === id);
    if (index < 0) return null;
    keys[index] = {
      ...keys[index],
      name: patch.name ?? keys[index].name,
      enabled: patch.enabled ?? keys[index].enabled,
      scopes: Array.isArray(patch.scopes) ? patch.scopes : keys[index].scopes,
      rateLimitPerMinute: patch.rateLimitPerMinute === undefined
        ? keys[index].rateLimitPerMinute
        : Number(patch.rateLimitPerMinute || 0),
    };
    this.configStore.setApiKeys(keys);
    return publicKeyRecord(keys[index]);
  }

  delete(id) {
    const keys = this.configStore.listApiKeys();
    const next = keys.filter(key => key.id !== id);
    this.configStore.setApiKeys(next);
    return next.length !== keys.length;
  }

  verify(raw, req) {
    const legacy = this.configStore.getAuth().apiKey;
    if (legacy && raw === legacy) {
      return { id: 'legacy', name: 'Legacy Key', scopes: ['all'] };
    }
    if (!raw) return null;
    const hashed = hashKey(raw);
    const keys = this.configStore.listApiKeys();
    const record = keys.find(key => key.enabled !== false && key.hash === hashed);
    if (!record) return null;
    record.lastUsedAt = new Date().toISOString();
    record.lastUsedIp = req?.ip || null;
    this.configStore.setApiKeys(keys);
    return publicKeyRecord(record);
  }
}
