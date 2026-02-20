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
  private prevReplyCount = 0;

  async newChat() {
    // 导航由 background 处理，content script 不再自行导航（避免自毁）
  }

  async sendMessage(text: string) {
    // 记录发送前的回复元素数量和最后元素引用
    this.prevReplyCount = this.countReplyElements();
    const lastEl = this.findLastReplyRaw();
    this.prevReplyText = lastEl?.innerText || '';
    console.log(`[GenericAdapter] sendMessage: prevCount=${this.prevReplyCount}, prevText.len=${this.prevReplyText.length}, host=${location.hostname}`);

    const el = this.findInput();
    console.log(`[GenericAdapter] findInput: ${el ? el.tagName + '.' + el.className.slice(0, 50) : 'NULL'}`);
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

  observeResponse(onChunk: (delta: string) => void, onDone: (fullText: string) => void) {
    this.lastLength = 0;
    let noChangeCount = 0;
    let started = false;

    const tick = () => {
      const currentCount = this.countReplyElements();
      // 始终用全部新回复元素的拼接文本，避免 DOM 重构导致单元素丢失内容
      const raw = this.findLastReplyRaw();
      const text = this.collectNewReplyText() || (raw ? this.extractText(raw) : '');

      if (!started && currentCount <= this.prevReplyCount && text === this.prevReplyText) return;

      if (text.length > this.lastLength) {
        started = true;
        noChangeCount = 0;
        onChunk(text.substring(this.lastLength));
        this.lastLength = text.length;
      } else if (started) {
        noChangeCount++;
        // 检测生成是否已结束（发送按钮可用 or 停止按钮消失）
        const idle = this.isGenerationIdle();
        if (noChangeCount >= 20 || (idle && noChangeCount >= 6)) {
          this.stopObserve();
          onDone(text);
        }
      }
    };

    this.doneTimer = setInterval(tick, 500);
  }

  stopObserve() {
    if (this.doneTimer) { clearInterval(this.doneTimer); this.doneTimer = null; }
    if (this.observer) { this.observer.disconnect(); this.observer = null; }
  }

  /** 提取文本，保留 <tool_call> 标签（浏览器会把它当 HTML 元素吃掉） */
  private extractText(el: HTMLElement): string {
    if (!el.getElementsByTagName('tool_call').length) return el.innerText;
    const clone = el.cloneNode(true) as HTMLElement;
    for (const tc of Array.from(clone.getElementsByTagName('tool_call'))) {
      tc.replaceWith(`<tool_call>${tc.textContent}</tool_call>`);
    }
    return clone.innerText;
  }

  /** 检测页面是否已停止生成（停止按钮消失 or 发送按钮可用） */
  private isGenerationIdle(): boolean {
    // 有"停止"按钮说明还在生成
    const stops = document.querySelectorAll('button');
    for (const btn of stops) {
      const t = (btn.textContent || '').toLowerCase();
      const a = (btn.getAttribute('aria-label') || '').toLowerCase();
      if (t.includes('stop') || t.includes('停止') || a.includes('stop') || a.includes('停止')) return false;
    }
    // 输入框可用说明生成结束
    const input = this.findInput();
    if (input && !input.closest('[disabled]') && !(input as any).disabled) return true;
    return false;
  }

  /** 判断元素是否是 AI 回复（排除页面 UI 组件） */
  private isReplyElement(el: HTMLElement): boolean {
    const text = el.innerText || '';
    if (text.length < 2) return false;
    // 排除文件上传区、输入框附近的 UI 元素
    if (/拖放文件|文件数量|文件类型|drop.*file/i.test(text) && text.length < 200) return false;
    if (el.closest('textarea, [contenteditable], [class*="upload"], [class*="input-area"], [class*="chat-input"]')) return false;
    return true;
  }

  /** 收集 prevReplyCount 之后出现的所有新回复元素文本 */
  private collectNewReplyText(): string {
    const selectors = ['[class*="markdown"]', '[class*="message-content"]', '[class*="response"]', '[class*="answer"]', '[class*="assistant"]', '[class*="bot"]'];
    for (const sel of selectors) {
      const els = this.filterReplies(document.querySelectorAll(sel));
      if (els.length > this.prevReplyCount) {
        return els.slice(this.prevReplyCount)
          .map(el => this.extractText(el)).join('\n');
      }
    }
    return '';
  }

  private countReplyElements(): number {
    const selectors = ['[class*="markdown"]', '[class*="message-content"]', '[class*="response"]', '[class*="answer"]', '[class*="assistant"]', '[class*="bot"]'];
    for (const sel of selectors) {
      const n = this.filterReplies(document.querySelectorAll(sel)).length;
      if (n) return n;
    }
    return 0;
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
      const els = this.filterReplies(document.querySelectorAll(sel));
      if (els.length) return els[els.length - 1];
    }
    console.log('[GenericAdapter] findLastReplyRaw: NO MATCH');
    return null;
  }

  private filterReplies(nodeList: NodeListOf<Element>): HTMLElement[] {
    return Array.from(nodeList).filter(el => this.isReplyElement(el as HTMLElement)) as HTMLElement[];
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
