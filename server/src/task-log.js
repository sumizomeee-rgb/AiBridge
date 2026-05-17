import { existsSync, mkdirSync, readFileSync, appendFileSync } from 'fs';
import { resolve } from 'path';
import { DATA_DIR } from './paths.js';

export class TaskLog {
  constructor(path = resolve(DATA_DIR, 'tasks.jsonl')) {
    this.path = path;
    mkdirSync(DATA_DIR, { recursive: true });
  }

  append(record) {
    const line = JSON.stringify({ ...record, loggedAt: new Date().toISOString() });
    appendFileSync(this.path, `${line}\n`, 'utf-8');
  }

  recent(limit = 100) {
    if (!existsSync(this.path)) return [];
    const lines = readFileSync(this.path, 'utf-8').trim().split(/\r?\n/).filter(Boolean);
    return lines.slice(-limit).reverse().map(line => {
      try {
        return JSON.parse(line);
      } catch {
        return { error: 'BAD_LOG_LINE', raw: line };
      }
    });
  }
}
