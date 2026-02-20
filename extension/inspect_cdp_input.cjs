const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP('http://localhost:9222');
  const contexts = browser.contexts();
  let page = null;
  for (const ctx of contexts)
    for (const p of ctx.pages())
      if (p.url().includes('doubao.com')) { page = p; break; }

  if (!page) { console.log('No doubao page found'); await browser.close(); return; }

  // 找到 textarea 并输入文字
  const ta = await page.$('textarea.semi-input-textarea');
  if (!ta) { console.log('No textarea found'); await browser.close(); return; }

  await ta.focus();
  await ta.evaluate((el) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, 'test');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.waitForTimeout(1000);

  // 截图看输入后的状态
  await page.screenshot({ path: 'G:/SuchProject/Other/AiBridge/extension/cdp_after_input.png' });

  // 重新扫描底部按钮
  const btns = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')];
    return btns
      .filter(b => b.getBoundingClientRect().top > window.innerHeight - 200)
      .map(b => ({
        class: b.className.slice(0, 100),
        ariaLabel: b.getAttribute('aria-label'),
        text: b.innerText?.slice(0, 40),
        hasSvg: !!b.querySelector('svg'),
        disabled: b.disabled,
        rect: b.getBoundingClientRect()
      }));
  });
  console.log('Buttons after input:', JSON.stringify(btns, null, 2));

  // 清空输入
  await ta.evaluate((el) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(el, '');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });

  await browser.close();
})();
