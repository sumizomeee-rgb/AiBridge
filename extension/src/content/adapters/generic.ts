import type { AiAdapter } from './base';

/**
 * 通用适配器：不依赖硬编码选择器，动态探测页面元素
 * 适用于豆包、通义千问、Gemini 等所有聊天页面
 */
export class GenericAdapter implements AiAdapter {
  private observer: MutationObserver | null = null;
  private lastLength = 0;
  private doneTimer: ReturnType<typeof setInterval> | null = null;
  // 发送前最后一条回复的文本，用于区分新旧回复
  private prevReplyText = '';

  async newChat() {
    // 导航由 background 处理，content script 不再自行导航（避免自毁）
  }

  async sendMessage(text: string) {
    // 记录发送前最后一条回复的完整文本
    const lastEl = this.findLastReplyRaw();
    this.prevReplyText = lastEl?.innerText || '';

    const el = this.findInput();
    if (!el) throw new Error('Input not found on ' + location.hostname);

    el.focus();
    await this.wait(100);

    if (el instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
      setter.call(el, text);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      el.textContent = '';
      el.focus();
      document.execCommand('insertText', false, text);
    }
    await this.wait(500);

    const sendBtn = this.findSendButton(el);
    if (sendBtn) {
      sendBtn.click();
    } else {
      el.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true
      }));
    }
  }

  observeResponse(onChunk: (delta: string) => void, onDone: () => void) {
    this.lastLength = 0;
    let noChangeCount = 0;
    let started = false;

    const tick = () => {
      const container = this.findLastReplyRaw();
      if (!container) return;

      const text = container.innerText || '';

      // 跳过发送前就存在的旧回复
      if (!started && text === this.prevReplyText) return;

      if (text.length > this.lastLength) {
        started = true;
        noChangeCount = 0;
        onChunk(text.substring(this.lastLength));
        this.lastLength = text.length;
      } else if (started) {
        noChangeCount++;
        if (noChangeCount >= 6) {
          this.stopObserve();
          onDone();
        }
      }
    };

    this.doneTimer = setInterval(tick, 500);
  }

  stopObserve() {
    if (this.doneTimer) { clearInterval(this.doneTimer); this.doneTimer = null; }
    if (this.observer) { this.observer.disconnect(); this.observer = null; }
  }

  /** 无条件返回页面上最后一个回复容器 */
  private findLastReplyRaw(): HTMLElement | null {
    const selectors = [
      '[class*="markdown"]',
      '[class*="message-content"]',
      '[class*="response"]',
      '[class*="answer"]',
      '[class*="assistant"]',
      '[class*="bot"]',
    ];
    for (const sel of selectors) {
      const els = document.querySelectorAll(sel);
      if (els.length) return els[els.length - 1] as HTMLElement;
    }
    return null;
  }

  private findInput(): HTMLElement | null {
    const textareas = [...document.querySelectorAll('textarea')] as HTMLTextAreaElement[];
    if (textareas.length) {
      return textareas.sort((a, b) =>
        b.getBoundingClientRect().top - a.getBoundingClientRect().top
      )[0];
    }
    const editables = [...document.querySelectorAll('[contenteditable="true"]')] as HTMLElement[];
    if (editables.length) {
      return editables.sort((a, b) =>
        b.getBoundingClientRect().top - a.getBoundingClientRect().top
      )[0];
    }
    return null;
  }

  private findSendButton(input: HTMLElement): HTMLElement | null {
    const inputRect = input.getBoundingClientRect();
    let container = input.parentElement;
    for (let i = 0; i < 6 && container; i++) {
      const btns = [...container.querySelectorAll('button')];
      for (const btn of btns) {
        const text = (btn.textContent || '').toLowerCase();
        const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
        if (text.includes('send') || text.includes('发送') ||
            aria.includes('send') || aria.includes('发送')) return btn;
      }
      const iconBtns = btns.filter(b => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && !b.disabled && b.querySelector('svg') &&
          !b.textContent?.trim() && r.left >= inputRect.right - 50;
      });
      if (iconBtns.length) return iconBtns[iconBtns.length - 1];
      container = container.parentElement;
    }
    return null;
  }

  private wait(ms: number) {
    return new Promise(r => setTimeout(r, ms));
  }
}
