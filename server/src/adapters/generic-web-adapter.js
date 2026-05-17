function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isNavigationRace(error) {
  return /Execution context was destroyed|Cannot find context|navigation/i.test(error?.message || '');
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
        waitUntil: options.waitUntil || 'commit',
        timeout: options.timeoutMs || 20000,
      });
    }
  }

  async detect(page) {
    await page.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(() => {});
    return page.evaluate(() => {
      const visible = el => el.offsetWidth > 0 && el.offsetHeight > 0;
      const textareas = Array.from(document.querySelectorAll('textarea')).filter(visible);
      const editables = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(visible);
      const pageText = document.body?.innerText || '';
      const pageState = `${location.href}\n${pageText}`;
      const loginBlocking = /from_logout=1|\u767b\u5f55\u540e|\u5fae\u4fe1\u626b\u7801\u767b\u5f55|\u624b\u673a\u53f7\u5feb\u6377\u767b\u5f55|\u767b\u5f55\u4ee5\u540c\u6b65\u5386\u53f2\u4f1a\u8bdd/i.test(pageState)
        || (/\u767b\u5f55/.test(pageText) && !/\u9000\u51fa|\u8d26\u53f7|user-menu|sumizome/i.test(pageText));
      const loginHints = loginBlocking || /\u767b\u5f55|\u767b\u9304|sign in|log in|\u9a8c\u8bc1\u7801|captcha|verify/i.test(pageState);
      const blockedHints = /region-ban|security|\u98ce\u9669|\u53d7\u9650|\u4e0d\u53ef\u7528|blocked|forbidden/i.test(pageState);
      return {
        url: location.href,
        title: document.title,
        inputReady: !loginBlocking && (textareas.length > 0 || editables.length > 0),
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
        const trimmed = text.trim();
        if (/^(?:\u65b0\u5bf9\u8bdd\s*)?(?:\u5185\u5bb9\u7531\u8c46\u5305 AI \u751f\u6210\uff0c\u8bf7\u4ed4\u7ec6\u7504\u522b\s*)?(?:\u4e0b\u8f7d\u7535\u8111\u7248\s*)?(?:\u767b\u5f55)?$/i.test(trimmed)) return false;
        if (/^(?:\u65b0\u5bf9\u8bdd|Ctrl K|AI \u521b\u4f5c|\u66f4\u591a|\u5173\u4e8e\u8c46\u5305|\u4e0b\u8f7d\u7535\u8111\u7248|\u767b\u5f55|\?|\s)+$/i.test(trimmed) && text.length < 200) return false;
        const className = el.className?.toString?.() || '';
        if (className.includes('qwen-chat-message-assistant') && !el.querySelector('[class*="response-message-content"], [class*="qwen-markdown"]')) return false;
        if (trimmed.includes('\u8df3\u8fc7') && !el.querySelector('[class*="response-message-content"], [class*="qwen-markdown"]')) return false;
        if (/^(新对话\s*)?(内容由豆包 AI 生成，请仔细甄别\s*)?(下载电脑版)?$/i.test(text.trim())) return false;
        if (/拖放文件|文件数量|文件类型|drop.*file/i.test(text) && text.length < 200) return false;
        const assistantAncestor = el.closest('[data-message-author-role="assistant"], [class*="qwen-chat-message-assistant"], [class*="assistant"], [class~="bot"], [class*="bot-message"], [class*="message-bot"]');
        const userAncestor = el.closest('[data-message-author-role="user"], [class*="qwen-chat-message-user"], [class*="chat-user-message"], [class*="user-message"], [class*="message-user"]');
        if (userAncestor && !assistantAncestor) return false;
        if (el.closest('textarea, [contenteditable], [class*="upload"], [class*="input-area"], [class*="chat-input"]')) {
          return false;
        }
        return true;
      }

      function extractText(el) {
        if (!el.getElementsByTagName('tool_call').length) return el.innerText || '';
        const clone = el.cloneNode(true);
        for (const tc of Array.from(clone.getElementsByTagName('tool_call'))) {
          const attrs = Array.from(tc.attributes)
            .map(attr => ` ${attr.name}="${attr.value}"`)
            .join('');
          tc.replaceWith(`<tool_call${attrs}>${tc.textContent}</tool_call>`);
        }
        return clone.innerText || '';
      }

      function keepLeafElements(items) {
        return items.filter(el => !items.some(other => other !== el && el.contains(other)));
      }

      const selectors = [
        '[data-message-author-role="assistant"]',
        '[class*="response-message-content"]',
        '[class*="phase-answer"]',
        '[class*="md-box-root"]',
        '[class*="custom-qwen-markdown"]',
        '[class*="qwen-markdown"]',
        '[class*="qwen-chat-message-assistant"]',
        '[class*="markdown"]',
        '[class*="message-content"]',
        '[class*="response"]',
        '[class*="answer"]',
        '[class*="assistant"]',
        '[class~="bot"]',
        '[class*="bot-message"]',
        '[class*="message-bot"]',
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
        if (location.hostname.includes('chat.qwen.ai')) return null;
        function isVisible(el) {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        }

        function isDisabled(el) {
          return el.disabled === true
            || el.getAttribute('aria-disabled') === 'true'
            || el.className?.toString?.().toLowerCase().includes('disabled');
        }

        function hasSendSignal(el) {
          const value = [
            el.textContent || '',
            el.getAttribute?.('aria-label') || '',
            el.getAttribute?.('title') || '',
            el.getAttribute?.('name') || '',
            el.className?.toString?.() || '',
          ].join(' ').toLowerCase();
          return value.includes('send') || value.includes('submit') || value.includes('arrow-up');
        }

        function isNearInputRight(el) {
          const rect = el.getBoundingClientRect();
          return rect.left >= inputRect.left
            && rect.right <= inputRect.right + 220
            && rect.top >= inputRect.top - 80
            && rect.bottom <= inputRect.bottom + 120;
        }

        function clickableParent(el, boundary) {
          let node = el;
          for (let i = 0; i < 5 && node && node !== boundary.parentElement; i++) {
            const role = node.getAttribute?.('role');
            const className = node.className?.toString?.().toLowerCase() || '';
            if (
              node.tagName === 'BUTTON'
              || role === 'button'
              || node.tabIndex >= 0
              || className.includes('send')
              || className.includes('submit')
              || className.includes('activity-card-icon')
              || className.includes('icon')
            ) {
              return node;
            }
            node = node.parentElement;
          }
          return null;
        }

        let container = input.parentElement;
        for (let i = 0; i < 7 && container; i++) {
          const buttons = Array.from(container.querySelectorAll('button, [role="button"]')).filter(btn => !isDisabled(btn) && isVisible(btn));
          for (const button of buttons) {
            if (hasSendSignal(button)) {
              return button;
            }
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
          const sendIcons = Array.from(container.querySelectorAll('svg')).filter(svg => {
            if (!isVisible(svg)) return false;
            if (!hasSendSignal(svg)) return false;
            return isNearInputRight(svg);
          });
          for (const icon of sendIcons) {
            const target = clickableParent(icon, container);
            if (target && !isDisabled(target) && isVisible(target)) return target;
          }
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

  async waitForResponse(page, baseline, callbacks, timeoutMs, responseSettleMs = 12000) {
    const startedAt = Date.now();
    let lastLength = 0;
    let noChangeCount = 0;
    let started = false;
    let finalText = '';
    const pollMs = 500;
    const settleLoops = Math.max(8, Math.ceil(Number(responseSettleMs || 12000) / pollMs));
    const hardSettleLoops = Math.max(settleLoops * 2, 40);

    while (Date.now() - startedAt < timeoutMs) {
      let state;
      try {
        state = await page.evaluate((base) => {
        function isReplyElement(el) {
          const text = el.innerText || '';
          if (text.length < 2) return false;
          const trimmed = text.trim();
          if (/^(?:\u65b0\u5bf9\u8bdd\s*)?(?:\u5185\u5bb9\u7531\u8c46\u5305 AI \u751f\u6210\uff0c\u8bf7\u4ed4\u7ec6\u7504\u522b\s*)?(?:\u4e0b\u8f7d\u7535\u8111\u7248\s*)?(?:\u767b\u5f55)?$/i.test(trimmed)) return false;
          if (/^(?:\u65b0\u5bf9\u8bdd|Ctrl K|AI \u521b\u4f5c|\u66f4\u591a|\u5173\u4e8e\u8c46\u5305|\u4e0b\u8f7d\u7535\u8111\u7248|\u767b\u5f55|\?|\s)+$/i.test(trimmed) && text.length < 200) return false;
          const className = el.className?.toString?.() || '';
          if (className.includes('qwen-chat-message-assistant') && !el.querySelector('[class*="response-message-content"], [class*="qwen-markdown"]')) return false;
          if (trimmed.includes('\u8df3\u8fc7') && !el.querySelector('[class*="response-message-content"], [class*="qwen-markdown"]')) return false;
          if (/^(新对话\s*)?(内容由豆包 AI 生成，请仔细甄别\s*)?(下载电脑版)?$/i.test(text.trim())) return false;
          if (/拖放文件|文件数量|文件类型|drop.*file/i.test(text) && text.length < 200) return false;
          const assistantAncestor = el.closest('[data-message-author-role="assistant"], [class*="qwen-chat-message-assistant"], [class*="assistant"], [class~="bot"], [class*="bot-message"], [class*="message-bot"]');
          const userAncestor = el.closest('[data-message-author-role="user"], [class*="qwen-chat-message-user"], [class*="chat-user-message"], [class*="user-message"], [class*="message-user"]');
          if (userAncestor && !assistantAncestor) return false;
          if (el.closest('textarea, [contenteditable], [class*="upload"], [class*="input-area"], [class*="chat-input"]')) {
            return false;
          }
          return true;
        }

        function extractText(el) {
          if (!el.getElementsByTagName('tool_call').length) return el.innerText || '';
          const clone = el.cloneNode(true);
          for (const tc of Array.from(clone.getElementsByTagName('tool_call'))) {
            const attrs = Array.from(tc.attributes)
              .map(attr => ` ${attr.name}="${attr.value}"`)
              .join('');
            tc.replaceWith(`<tool_call${attrs}>${tc.textContent}</tool_call>`);
          }
          return clone.innerText || '';
        }

        function keepLeafElements(items) {
          return items.filter(el => !items.some(other => other !== el && el.contains(other)));
        }

        const selectors = [
          '[data-message-author-role="assistant"]',
          '[class*="response-message-content"]',
          '[class*="phase-answer"]',
          '[class*="md-box-root"]',
          '[class*="custom-qwen-markdown"]',
          '[class*="qwen-markdown"]',
          '[class*="qwen-chat-message-assistant"]',
          '[class*="markdown"]',
          '[class*="message-content"]',
          '[class*="response"]',
          '[class*="answer"]',
          '[class*="assistant"]',
          '[class~="bot"]',
          '[class*="bot-message"]',
          '[class*="message-bot"]',
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

        const buttons = Array.from(document.querySelectorAll('button, [role="button"]')).filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        });
        const hasStop = buttons.some(btn => {
          const textValue = (btn.textContent || '').toLowerCase();
          const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
          return textValue.includes('stop') || textValue.includes('停止') || aria.includes('stop') || aria.includes('停止');
        });
        const hasInput = document.querySelector('textarea, [contenteditable="true"]') != null;
        const input = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]'))
          .filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          })
          .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
        const inputRect = input?.getBoundingClientRect();
        const inputControls = inputRect
          ? buttons.filter((control) => {
            const rect = control.getBoundingClientRect();
            return rect.left >= inputRect.right - 140
              && rect.top >= inputRect.top - 40
              && rect.bottom <= inputRect.bottom + 70;
          }).sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left)
          : [];
        const primaryInputControl = inputControls[inputControls.length - 1] || null;
        const primaryActive = primaryInputControl
          ? primaryInputControl.disabled !== true
            && primaryInputControl.getAttribute('aria-disabled') !== 'true'
            && !primaryInputControl.className?.toString?.().toLowerCase().includes('disabled')
          : false;
        const generating = hasStop || primaryActive;
        const idle = !generating && hasInput;
        return { text, count, idle, generating };
        }, baseline);
      } catch (error) {
        if (!isNavigationRace(error)) throw error;
        await page.waitForLoadState('domcontentloaded', { timeout: 3000 }).catch(() => {});
        await sleep(pollMs);
        continue;
      }

      if (!started && state.count <= baseline.count && state.text === baseline.lastText) {
        await sleep(pollMs);
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
        if (
          (!state.generating && (noChangeCount >= settleLoops || (state.idle && noChangeCount >= 6)))
          || noChangeCount >= hardSettleLoops
        ) {
          if (hasUnclosedToolCall(state.text) && Date.now() - startedAt < timeoutMs - 5000) {
            await sleep(pollMs);
            continue;
          }
          return finalText || state.text;
        }
      }
      await sleep(pollMs);
    }
    throw new Error('Task timed out waiting for web AI response');
  }
}
