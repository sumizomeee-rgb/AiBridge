const { chromium } = require('playwright');
(async () => {
  const b = await chromium.connectOverCDP('http://localhost:9222');
  const pages = [];
  for (const c of b.contexts())
    for (const p of c.pages()) pages.push(p.url());
  console.log('Open tabs:', JSON.stringify(pages));
  await b.close();
})();
