import type { AiAdapter } from './base';

export class DoubaoAdapter implements AiAdapter {
  private observer: MutationObserver | null = null;
  private lastLength = 0;

  async newChat() {
    const btn = document.querySelector('[data-testid="new_chat_button"]') as HTMLElement
      || document.querySelector('.new-chat-btn') as HTMLElement
      || document.querySelector('div[class*="new-chat"]') as HTMLElement;
    if (btn) {
      btn.click();
      await this.wait(1500);
    }
  }

  async sendMessage(text: string) {
    const el = document.querySelector('textarea') as HTMLTextAreaElement
      || document.querySelector('[contenteditable="true"]') as HTMLElement;
    if (!el) throw new Error('Input not found');

    el.focus();
    if (el instanceof HTMLTextAreaElement) {
      const nativeSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
      nativeSetter.call(el, text);
    } else {
      el.textContent = text;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    await this.wait(300);

    // 找发送按钮
    const sendBtn = document.querySelector('[data-testid="send_button"]') as HTMLElement
      || document.querySelector('button[class*="send"]') as HTMLElement
      || this.findButtonByIcon('send');
    if (sendBtn) {
      sendBtn.click();
    } else {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    }
  }

  observeResponse(onChunk: (delta: string) => void, onDone: () => void) {
    this.lastLength = 0;
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

      // 轮询检测完成
      const checkDone = setInterval(() => {
        const isGenerating = document.querySelector('[class*="stop"]')
          || document.querySelector('button[class*="loading"]');
        if (!isGenerating) {
          clearInterval(checkDone);
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
    // 豆包的回复容器通常是 markdown 渲染区域
    const replies = document.querySelectorAll('[class*="message"][class*="assistant"]')
      || document.querySelectorAll('[class*="bot-message"]')
      || document.querySelectorAll('[class*="markdown"]');
    return replies.length ? replies[replies.length - 1] as HTMLElement : null;
  }

  private findButtonByIcon(type: string): HTMLElement | null {
    const buttons = document.querySelectorAll('button');
    for (const btn of buttons) {
      if (btn.querySelector(`svg`) && btn.closest('[class*="input"]')) {
        return btn;
      }
    }
    return null;
  }

  private wait(ms: number) {
    return new Promise(r => setTimeout(r, ms));
  }
}
