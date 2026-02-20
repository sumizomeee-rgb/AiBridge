from playwright.sync_api import sync_playwright
import json

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe'
    )
    page = browser.new_page()
    page.goto('https://www.doubao.com/chat/', timeout=15000)
    page.wait_for_timeout(5000)
    page.screenshot(path='G:/SuchProject/Other/AiBridge/extension/doubao_shot.png')
    print("URL:", page.url)

    result = page.evaluate('''() => {
        const r = {};
        r.textareas = [...document.querySelectorAll('textarea')].map(e => ({class:e.className, placeholder:e.placeholder}));
        r.editables = [...document.querySelectorAll('[contenteditable="true"]')].map(e => ({tag:e.tagName, class:e.className.slice(0,120)}));
        const btns = [...document.querySelectorAll('button')];
        r.bottomButtons = btns.filter(b => b.getBoundingClientRect().top > window.innerHeight - 300)
            .map(b => ({class:b.className.slice(0,80), ariaLabel:b.getAttribute('aria-label'), text:b.innerText?.slice(0,30), hasSvg:!!b.querySelector('svg')}));
        r.bodySnippet = document.body?.innerText?.slice(0, 300);
        return r;
    }''')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    browser.close()
