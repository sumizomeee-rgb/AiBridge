const { chromium } = require('playwright');
(async () => {
  const b = await chromium.connectOverCDP('http://localhost:9222');
  const ctx = b.contexts()[0];
  // 打开 extensions 页面
  const page = await ctx.newPage();
  await page.goto('chrome://extensions', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  // 检查是否有已安装的扩展
  const info = await page.evaluate(() => {
    const mgr = document.querySelector('extensions-manager');
    if (!mgr) return { error: 'no manager' };
    const items = mgr.shadowRoot?.querySelector('extensions-item-list');
    if (!items) return { error: 'no item-list' };
    const cards = items.shadowRoot?.querySelectorAll('extensions-item');
    if (!cards || !cards.length) return { error: 'no extensions', count: 0 };
    return [...cards].map(c => ({
      name: c.shadowRoot?.querySelector('#name')?.textContent?.trim(),
      id: c.id
    }));
  });
  console.log('Extensions:', JSON.stringify(info, null, 2));
  await page.close();
  await b.close();
})();
