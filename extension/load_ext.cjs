const { chromium } = require('playwright');
(async () => {
  const b = await chromium.connectOverCDP('http://localhost:9222');
  const ctx = b.contexts()[0];
  const page = await ctx.newPage();
  await page.goto('chrome://extensions', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

  // 开启开发者模式
  await page.evaluate(() => {
    const mgr = document.querySelector('extensions-manager');
    const toolbar = mgr?.shadowRoot?.querySelector('extensions-toolbar');
    const toggle = toolbar?.shadowRoot?.querySelector('#devMode');
    if (toggle && !toggle.checked) toggle.click();
  });
  await page.waitForTimeout(500);

  // 点击"加载已解压的扩展程序"并处理文件选择器
  const [fileChooser] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 5000 }),
    page.evaluate(() => {
      const mgr = document.querySelector('extensions-manager');
      const toolbar = mgr?.shadowRoot?.querySelector('extensions-toolbar');
      const loadBtn = toolbar?.shadowRoot?.querySelector('#loadUnpacked');
      if (loadBtn) loadBtn.click();
    })
  ]);
  await fileChooser.setFiles('G:\\SuchProject\\Other\\AiBridge\\extension\\dist');
  await page.waitForTimeout(2000);

  // 验证
  const exts = await page.evaluate(() => {
    const mgr = document.querySelector('extensions-manager');
    const items = mgr?.shadowRoot?.querySelector('extensions-item-list');
    const cards = items?.shadowRoot?.querySelectorAll('extensions-item');
    return [...(cards || [])].map(c => ({
      name: c.shadowRoot?.querySelector('#name')?.textContent?.trim(),
      id: c.id
    }));
  });
  console.log('Extensions after load:', JSON.stringify(exts, null, 2));
  await page.close();
  await b.close();
})();
