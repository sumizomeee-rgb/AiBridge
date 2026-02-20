// 通过 CDP 连接已有 Chrome，检测 AI 聊天页面的 DOM 结构
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP('http://localhost:9222');
  const contexts = browser.contexts();
  if (!contexts.length) { console.log('No browser contexts'); return; }

  // 找到目标页面
  const targetHosts = ['doubao.com', 'chatgpt.com', 'tongyi.aliyun.com', 'gemini.google.com', 'claude.ai'];
  let page = null;
  for (const ctx of contexts) {
    for (const p of ctx.pages()) {
      const url = p.url();
      if (targetHosts.some(h => url.includes(h))) {
        page = p;
        break;
      }
    }
    if (page) break;
  }

  if (!page) {
    console.log('No AI chat page found. Open pages:');
    for (const ctx of contexts)
      for (const p of ctx.pages())
        console.log(' ', p.url());
    await browser.close();
    return;
  }

  console.log('Target:', page.url());
  await page.screenshot({ path: 'G:/SuchProject/Other/AiBridge/extension/cdp_shot.png' });
  console.log('Screenshot saved.');

  const result = await page.evaluate(() => {
    const r = {};
    r.textareas = [...document.querySelectorAll('textarea')].map(e => ({
      class: e.className.slice(0, 120),
      placeholder: e.placeholder?.slice(0, 60),
      rect: e.getBoundingClientRect()
    }));
    r.editables = [...document.querySelectorAll('[contenteditable="true"]')].map(e => ({
      tag: e.tagName, class: e.className.slice(0, 120),
      rect: e.getBoundingClientRect()
    }));
    // 底部按钮
    const btns = [...document.querySelectorAll('button')];
    r.bottomButtons = btns
      .filter(b => b.getBoundingClientRect().top > window.innerHeight - 300)
      .map(b => ({
        class: b.className.slice(0, 80),
        ariaLabel: b.getAttribute('aria-label'),
        text: b.innerText?.slice(0, 30),
        hasSvg: !!b.querySelector('svg'),
        disabled: b.disabled
      }));
    // 消息容器候选
    const selectors = ['[class*="markdown"]','[class*="message"]','[class*="response"]','[class*="answer"]','[class*="assistant"]','[class*="bot"]','[class*="content"]'];
    r.messageContainers = {};
    for (const sel of selectors) {
      const els = document.querySelectorAll(sel);
      if (els.length) {
        r.messageContainers[sel] = els.length;
      }
    }
    r.bodySnippet = document.body?.innerText?.slice(0, 200);
    return r;
  });

  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})();
