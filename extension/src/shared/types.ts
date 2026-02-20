// WebSocket 消息类型定义

export interface TaskMessage {
  type: 'task';
  task_id: string;
  model: string;
  messages: { role: string; content: string }[];
  stream: boolean;
  new_chat: boolean;
}

export interface ChunkMessage {
  type: 'chunk';
  task_id: string;
  delta: string;
}

export interface DoneMessage {
  type: 'done';
  task_id: string;
}

export interface ErrorMessage {
  type: 'error';
  task_id: string;
  code: 'NOT_LOGGED_IN' | 'TAB_NOT_FOUND' | 'DOM_TIMEOUT' | 'RATE_LIMITED' | 'UNKNOWN';
  message: string;
}

export interface PingMessage { type: 'ping'; }
export interface PongMessage { type: 'pong'; }

export type WsMessage = TaskMessage | ChunkMessage | DoneMessage | ErrorMessage | PingMessage | PongMessage;

// 路由配置
export type RoutingMode = 'AUTO' | 'OVERRIDE';

export interface RoutingConfig {
  mode: RoutingMode;
  target_ai: string;
}

// 模型 -> 平台映射
export const MODEL_ROUTES: Record<string, { name: string; url: string }> = {
  gpt:    { name: 'ChatGPT',  url: 'https://chatgpt.com' },
  doubao: { name: '豆包',      url: 'https://www.doubao.com' },
  claude: { name: 'Claude',   url: 'https://claude.ai' },
  qwen:     { name: 'Qwen',      url: 'https://chat.qwen.ai' },
  yuanbao:  { name: '元宝',      url: 'https://yuanbao.tencent.com' },
  kimi:     { name: 'Kimi',      url: 'https://www.kimi.com' },
  mimo:     { name: 'MiMo',      url: 'https://aistudio.xiaomimimo.com' },
};
