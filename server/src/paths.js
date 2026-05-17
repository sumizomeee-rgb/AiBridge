import { dirname, resolve, isAbsolute } from 'path';
import { fileURLToPath } from 'url';

export const SRC_DIR = dirname(fileURLToPath(import.meta.url));
export const SERVER_ROOT = resolve(SRC_DIR, '..');
export const DATA_DIR = resolve(SERVER_ROOT, 'data');
export const PROFILE_ROOT = resolve(SERVER_ROOT, 'profiles');
export const PUBLIC_DIR = resolve(SERVER_ROOT, 'public');

export function serverPath(inputPath) {
  if (!inputPath) return SERVER_ROOT;
  return isAbsolute(inputPath) ? inputPath : resolve(SERVER_ROOT, inputPath);
}

export function profilePath(inputPath) {
  if (!inputPath) return PROFILE_ROOT;
  return isAbsolute(inputPath) ? inputPath : resolve(SERVER_ROOT, inputPath);
}
