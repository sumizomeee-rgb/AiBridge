const { chromium } = require('playwright');
(async () => {
  const ctx = await chromium.launchPersistentContext(
    'C:\\Users\\sumizome\\AppData\\Local\\Google\\Chrome\\User Data',
    {
      headless: false,
      channel: 'chrome',
      args: [
        '--remote-debugging-port=9222',
        '--disable-extensions-except=G:\\SuchProject\\Other\\AiBridge\\extension\\dist',
        '--load-extension=G:\\SuchProject\\Other\\AiBridge\\extension\\dist',
      ],
    }
  );
  const page = await ctx.newPage();
  await page.goto('https://www.doubao.com/chat/', { waitUntil: 'domcontentloaded' });
  console.log('Chrome launched with extension. Page:', page.url());
  console.log('Press Ctrl+C to close.');
  // 保持运行
  await new Promise(() => {});
})();
