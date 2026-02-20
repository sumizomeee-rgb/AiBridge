import type { AiAdapter } from './base';

export class ChatGPTAdapter implements AiAdapter {
  private observer: MutationObserver | null = null;
  private lastLength = 0;

  async newChat() {
    const btn = document.querySelector('a[href="/"]') as HTMLElement
      || document.querySelector('nav a:first-child') as HTMLElement;
    if (btn) {
      btn.click();
      await this.wait(1500);
    }
  }

  async sendMessage(text: string) {
    const el = document.querySelector('#prompt-textarea') as HTMLElement;
    if (!el) throw new Error('Input not found');

    // React 兼容：设置内容并触发 input 事件
    el.focus();
    el.textContent = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await this.wait(300);

    // 点击发送按钮
    const sendBtn = document.querySelector('[data-testid="send-button"]') as HTMLElement;
    if (sendBtn) {
      sendBtn.click();
    } else {
      // 备用：模拟回车
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    }
  }

  observeResponse(onChunk: (delta: string) => void, onDone: () => void) {
    this.lastLength = 0;
    // 等待回复容器出现后开始监听
    const startObserve = () => {
      const container = this.getLastReplyContainer();
      if (!container) {
        setTimeout(startObserve, 500);
        return;
      }
      this.observer = new MutationObserver(() => {
        const text = container.innerText || '';
        if (text.length > this.lastLength) {
          onChunk(text.substring(this.lastLength));
          this.lastLength = text.length;
        }
      });
      this.observer.observe(container, { childList: true, subtree: true, characterData: true });

      // 轮询检测完成（按钮从 stop 变回 send）
      const checkDone = setInterval(() => {
        const stopBtn = document.querySelector('[data-testid="stop-button"]');
        if (!stopBtn) {
          clearInterval(checkDone);
          // 最后一次读取
          const finalText = container.innerText || '';
          if (finalText.length > this.lastLength) {
            onChunk(finalText.substring(this.lastLength));
          }
          this.stopObserve();
          onDone();
        }
      }, 500);
    };
    setTimeout(startObserve, 1000);
  }

  stopObserve() {
    this.observer?.disconnect();
    this.observer = null;
  }

  private getLastReplyContainer(): HTMLElement | null {
    const articles = document.querySelectorAll('[data-message-author-role="assistant"]');
    return articles.length ? articles[articles.length - 1] as HTMLElement : null;
  }

  private wait(ms: number) {
    return new Promise(r => setTimeout(r, ms));
  }
}
