const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP('http://localhost:9222');
  let page = null;
  for (const ctx of browser.contexts())
    for (const p of ctx.pages())
      if (p.url().includes('doubao.com')) { page = p; break; }
  if (!page) { console.log('No doubao page'); await browser.close(); return; }

  // 找发送按钮的精确选择器
  const sendBtnInfo = await page.evaluate(() => {
    const ta = document.querySelector('textarea.semi-input-textarea');
    if (!ta) return { error: 'no textarea' };

    // 从 textarea 向上找容器
    let container = ta.parentElement;
    const results = [];
    for (let i = 0; i < 8 && container; i++) {
      const btns = container.querySelectorAll('button');
      for (const btn of btns) {
        const r = btn.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          results.push({
            level: i,
            class: btn.className.slice(0, 120),
            id: btn.id,
            dataTestId: btn.getAttribute('data-testid'),
            ariaLabel: btn.getAttribute('aria-label'),
            text: btn.innerText?.slice(0, 30),
            hasSvg: !!btn.querySelector('svg'),
            svgPath: btn.querySelector('svg path')?.getAttribute('d')?.slice(0, 50),
            x: r.x, y: r.y, w: r.width, h: r.height
          });
        }
      }
      container = container.parentElement;
    }
    return results;
  });
  console.log(JSON.stringify(sendBtnInfo, null, 2));
  await browser.close();
})();
