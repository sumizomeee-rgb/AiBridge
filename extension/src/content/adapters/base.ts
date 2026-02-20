import type { TaskMessage } from '../shared/types';

export interface AiAdapter {
  /** 点击"新建对话"按钮 */
  newChat(): Promise<void>;
  /** 模拟输入文本并发送 */
  sendMessage(text: string): Promise<void>;
  /** 监听 AI 回复，增量回调 */
  observeResponse(onChunk: (delta: string) => void, onDone: (fullText: string) => void): void;
  /** 停止监听 */
  stopObserve(): void;
}
