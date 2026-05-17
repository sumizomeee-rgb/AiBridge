function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function hasUnclosedToolCall(text) {
  const value = String(text || '');
  return value.lastIndexOf('<tool_call') > value.lastIndexOf('</tool_call>');
}

export class GenericWebAdapter {
  async gotoStart(page, provider, newChat, options = {}) {
    const target = newChat ? provider.baseUrl : (provider.startUrl || provider.baseUrl);
    if (!target) throw new Error('Provider URL is empty');
    const current = page.url();
    const normalizedCurrent = current.replace(/\/$/, '');
    const normalizedTarget = target.replace(/\/$/, '');
    const shouldNavigate = newChat
      ? normalizedCurrent !== normalizedTarget
      : !normalizedCurrent.startsWith(normalizedTarget);
    if (shouldNavigate) {
      await page.goto(target, {
        waitUntil: options.waitUntil || 'domcontentloaded',
        timeout: options.timeoutMs || 45000,
      });
    }
  }

  async detect(page) {
    await page.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
    return page.evaluate(() => {
      const textareas = Array.from(document.querySelectorAll('textarea'))
        .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0);
      const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'))
        .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0);
      const pageText = document.body?.innerText || '';
      const loginHints = /登录|登陆|sign in|log in|验证码|captcha|verify/i.test(pageText);
      const blockedHints = /region-ban|security|风险|受限|不可用|blocked|forbidden/i.test(`${location.href}\n${pageText}`);
      return {
        url: location.href,
        title: document.title,
        inputReady: textareas.length > 0 || editables.length > 0,
        inputCount: textareas.length + editables.length,
        loginHints,
        blockedHints,
      };
    });
  }

  async captureBaseline(page) {
    return page.evaluate(() => {
      function isReplyElement(el) {
        const text = el.innerText || '';
        if (text.length < 2) return false;
        if (/^(新对话\s*)?(内容由豆包 AI 生成，请仔细甄别\s*)?(下载电脑版)?$/i.test(text.trim())) return false;
        if (/拖放文件|文件数量|文件类型|drop.*file/i.test(text) && text.length < 200) return false;
        if (el.closest('textarea, [contenteditable], [class*="upload"], [class*="input-area"], [class*="chat-input"]')) {
          return false;
        }
        return true;
      }

      function extractText(el) {
        if (!el.getElementsByTagName('tool_call').length) return el.innerText || '';
        const clone = el.cloneNode(true);
        for (const tc of Array.from(clone.getElementsByTagName('tool_call'))) {
          tc.replaceWith(`<tool_call>${tc.textContent}</tool_call>`);
        }
        return clone.innerText || '';
      }

      function keepLeafElements(items) {
        return items.filter(el => !items.some(other => other !== el && el.contains(other)));
      }

      const selectors = [
        '[class*="md-box-root"]',
        '[data-message-author-role="assistant"]',
        '[class*="markdown"]',
        '[class*="message-content"]',
        '[class*="response"]',
        '[class*="answer"]',
        '[class*="assistant"]',
        '[class*="bot"]',
      ];
      for (const selector of selectors) {
        const items = keepLeafElements(Array.from(document.querySelectorAll(selector)).filter(isReplyElement));
        if (items.length) {
          return { count: items.length, lastText: extractText(items[items.length - 1]) };
        }
      }
      return { count: 0, lastText: '' };
    });
  }

  async sendMessage(page, text) {
    const result = await page.evaluate((message) => {
      function findInput() {
        const textareas = Array.from(document.querySelectorAll('textarea'))
          .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0);
        if (textareas.length) {
          return textareas.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
        }
        const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'))
          .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0);
        if (editables.length) {
          return editables.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
        }
        return null;
      }

      function findSendButton(input) {
        const inputRect = input.getBoundingClientRect();
        let container = input.parentElement;
        for (let i = 0; i < 7 && container; i++) {
          const buttons = Array.from(container.querySelectorAll('button')).filter(btn => !btn.disabled);
          for (const button of buttons) {
            const text = (button.textContent || '').toLowerCase();
            const aria = (button.getAttribute('aria-label') || '').toLowerCase();
            if (text.includes('send') || text.includes('发送') || aria.includes('send') || aria.includes('发送')) {
              return button;
            }
          }
          const iconButtons = buttons.filter(button => {
            const rect = button.getBoundingClientRect();
            return rect.width > 0 && button.querySelector('svg') && !button.textContent?.trim()
              && rect.left >= inputRect.right - 80;
          });
          if (iconButtons.length) return iconButtons[iconButtons.length - 1];
          container = container.parentElement;
        }
        return null;
      }

      const input = findInput();
      if (!input) return { ok: false, error: `Input not found on ${location.hostname}` };
      input.focus();
      if (input instanceof HTMLTextAreaElement) {
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
        if (setter) setter.call(input, message);
        else input.value = message;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        input.textContent = '';
        document.execCommand('insertText', false, message);
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }

      const button = findSendButton(input);
      if (button) {
        button.click();
        return { ok: true, clicked: true };
      }
      return { ok: true, clicked: false };
    }, text);

    if (!result.ok) throw new Error(result.error);
    if (!result.clicked) await page.keyboard.press('Enter');
  }

  async waitForResponse(page, baseline, callbacks, timeoutMs) {
    const startedAt = Date.now();
    let lastLength = 0;
    let noChangeCount = 0;
    let started = false;
    let finalText = '';

    while (Date.now() - startedAt < timeoutMs) {
      const state = await page.evaluate((base) => {
        function isReplyElement(el) {
          const text = el.innerText || '';
          if (text.length < 2) return false;
          if (/^(新对话\s*)?(内容由豆包 AI 生成，请仔细甄别\s*)?(下载电脑版)?$/i.test(text.trim())) return false;
          if (/拖放文件|文件数量|文件类型|drop.*file/i.test(text) && text.length < 200) return false;
          if (el.closest('textarea, [contenteditable], [class*="upload"], [class*="input-area"], [class*="chat-input"]')) {
            return false;
          }
          return true;
        }

        function extractText(el) {
          if (!el.getElementsByTagName('tool_call').length) return el.innerText || '';
          const clone = el.cloneNode(true);
          for (const tc of Array.from(clone.getElementsByTagName('tool_call'))) {
            tc.replaceWith(`<tool_call>${tc.textContent}</tool_call>`);
          }
          return clone.innerText || '';
        }

        function keepLeafElements(items) {
          return items.filter(el => !items.some(other => other !== el && el.contains(other)));
        }

        const selectors = [
          '[class*="md-box-root"]',
          '[data-message-author-role="assistant"]',
          '[class*="markdown"]',
          '[class*="message-content"]',
          '[class*="response"]',
          '[class*="answer"]',
          '[class*="assistant"]',
          '[class*="bot"]',
        ];
        let text = '';
        let count = 0;
        for (const selector of selectors) {
          const items = keepLeafElements(Array.from(document.querySelectorAll(selector)).filter(isReplyElement));
          if (items.length > base.count) {
            count = items.length;
            text = items.slice(base.count).map(extractText).join('\n');
            break;
          }
          if (items.length) {
            count = items.length;
            text = extractText(items[items.length - 1]);
            break;
          }
        }

        const buttons = Array.from(document.querySelectorAll('button'));
        const hasStop = buttons.some(btn => {
          const textValue = (btn.textContent || '').toLowerCase();
          const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
          return textValue.includes('stop') || textValue.includes('停止') || aria.includes('stop') || aria.includes('停止');
        });
        const hasInput = document.querySelector('textarea, [contenteditable="true"]') != null;
        const idle = !hasStop && hasInput;
        return { text, count, idle };
      }, baseline);

      if (!started && state.count <= baseline.count && state.text === baseline.lastText) {
        await sleep(500);
        continue;
      }

      if (state.text.length > lastLength) {
        started = true;
        noChangeCount = 0;
        const delta = state.text.slice(lastLength);
        finalText = state.text;
        lastLength = state.text.length;
        callbacks.onChunk(delta);
      } else if (started) {
        noChangeCount += 1;
        if (noChangeCount >= 20 || (state.idle && noChangeCount >= 6)) {
          if (hasUnclosedToolCall(state.text) && Date.now() - startedAt < timeoutMs - 5000) {
            await sleep(500);
            continue;
          }
          return finalText || state.text;
        }
      }
      await sleep(500);
    }
    throw new Error('Task timed out waiting for web AI response');
  }
}
