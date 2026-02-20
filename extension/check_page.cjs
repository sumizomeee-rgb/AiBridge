const { chromium } = require('playwright');
(async () => {
  const b = await chromium.connectOverCDP('http://localhost:9222');
  let page = null;
  for (const ctx of b.contexts())
    for (const p of ctx.pages())
      if (p.url().includes('doubao.com')) { page = p; break; }
  if (!page) { console.log('No doubao page'); await b.close(); return; }

  console.log('URL:', page.url());
  // 检查页面是否有输入框
  const hasInput = await page.evaluate(() => !!document.querySelector('textarea'));
  console.log('Has textarea:', hasInput);
  // 检查页面文本片段
  const snippet = await page.evaluate(() => document.body?.innerText?.slice(0, 200));
  console.log('Body:', snippet);
  // 截图
  await page.screenshot({ path: 'G:/SuchProject/Other/AiBridge/extension/cdp_current.png' });
  console.log('Screenshot saved');
  await b.close();
})();
