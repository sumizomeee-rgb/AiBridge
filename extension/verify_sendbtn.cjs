const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP('http://localhost:9222');
  let page = null;
  for (const ctx of browser.contexts())
    for (const p of ctx.pages())
      if (p.url().includes('doubao.com')) { page = p; break; }
  if (!page) { console.log('No doubao page'); await browser.close(); return; }

  // 输入文字
  const ta = await page.$('textarea.semi-input-textarea');
  await ta.evaluate((el) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, 'hello test');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForTimeout(800);

  // 模拟修复后的 findSendButton 逻辑
  const result = await page.evaluate(() => {
    const input = document.querySelector('textarea.semi-input-textarea');
    if (!input) return { error: 'no input' };
    const inputRect = input.getBoundingClientRect();

    let container = input.parentElement;
    for (let i = 0; i < 6 && container; i++) {
      const btns = [...container.querySelectorAll('button')];
      // 找输入框右侧的无文字 SVG 按钮
      const iconBtns = btns.filter(b => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && !b.disabled && b.querySelector('svg') &&
          !b.textContent?.trim() && r.left >= inputRect.right - 50;
      });
      if (iconBtns.length) {
        const btn = iconBtns[iconBtns.length - 1];
        const r = btn.getBoundingClientRect();
        return {
          found: true, level: i,
          x: r.x, y: r.y, w: r.width, h: r.height,
          class: btn.className.slice(0, 80),
          inputRight: inputRect.right
        };
      }
      container = container.parentElement;
    }
    return { found: false };
  });
  console.log('Send button:', JSON.stringify(result, null, 2));

  // 清空
  await ta.evaluate((el) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, '');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await browser.close();
})();
