const { chromium } = require('playwright');
(async () => {
  const b = await chromium.connectOverCDP('http://localhost:9222');
  let page = null;
  for (const ctx of b.contexts())
    for (const p of ctx.pages())
      if (p.url().includes('doubao.com')) { page = p; break; }
  if (!page) { console.log('No doubao page'); await b.close(); return; }
  const text = await page.evaluate(() => document.body.innerText.slice(0, 300));
  const loggedIn = text.indexOf('登录') === -1;
  console.log('Logged in:', loggedIn);
  if (!loggedIn) console.log('Page text:', text);
  await b.close();
})();
