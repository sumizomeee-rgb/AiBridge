import os
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright


def main() -> None:
    console_errors: list[str] = []
    with TemporaryDirectory(prefix="aibridge-ui-") as temp_dir, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto("http://127.0.0.1:7009", wait_until="networkidle")
        assert page.title() == "AiBridge 控制台"
        assert page.locator('link[rel="icon"]').get_attribute("href") == "/assets/favicon.svg"
        assert page.locator("#lan-url").is_visible()
        assert page.locator("#local-url").count() == 0
        assert page.get_by_role("heading", name="模型连接", exact=True).is_visible()
        assert page.get_by_text("统一模型入口").count() == 0
        assert page.get_by_text("一个地址，接入所有模型。").count() == 0
        assert page.locator(".source-row").count() >= 7
        assert page.locator(".source-switch").count() == page.locator(".source-row").count()
        assert page.locator(".source-row .provider-icon img").count() >= 6
        assert page.locator(".source-row").first.get_attribute("data-source") == "web-auto"
        auto = page.locator('.source-row[data-source="web-auto"]')
        assert "池" in auto.locator(".capacity").inner_text()
        auto.get_by_role("button", name="配置来源").click()
        assert page.locator("#web-public-name").input_value() == "web-auto"
        assert not page.locator("#source-base").is_visible()
        assert not page.locator("#web-credential-fields").is_visible()
        assert not page.locator("#web-max-concurrency").is_visible()
        if page.locator('input[name="auto-routing-mode"][value="smart"]').is_checked():
            page.locator(".route-mode-options label").filter(has_text="自定义").click()
        assert page.locator('input[name="auto-routing-mode"][value="custom"]').is_checked()
        if not page.locator('input[name="auto-dispatch-mode"][value="priority"]').is_checked():
            page.locator(".dispatch-mode label").filter(has_text="优先来源").click()
        assert page.locator('input[name="auto-dispatch-mode"][value="priority"]').is_checked()
        page.locator(".dispatch-mode label").filter(has_text="均衡轮询").click()
        assert page.locator('input[name="auto-dispatch-mode"][value="balanced"]').is_checked()
        assert page.locator("#auto-custom-routing").is_visible()
        assert page.locator("[data-auto-source]").count() >= 6
        assert page.locator('[data-auto-source="web-kimi"]').is_enabled()
        kimi_route = page.locator('[data-auto-source="web-kimi"]')
        if not kimi_route.is_checked():
            kimi_route.check()
        assert int(page.locator("#auto-source-count").inner_text().split()[0]) >= 1
        page.locator("[data-close]").first.click()
        deepseek = page.locator('.source-row[data-source="api-deepseek"]')
        assert deepseek.locator(".status").inner_text() == "可用"
        qwen = page.locator('.source-row[data-source="web-qwen"]')
        qwen.get_by_role("button", name="配置来源").click()
        assert "F12 → 网络 → Fetch/XHR" in page.locator("#web-guide").inner_text()
        assert "api/v2/chat/completions" in page.locator("#web-guide").inner_text()
        assert page.locator("#source-curl").get_attribute("placeholder").startswith(page.locator("#web-guide").inner_text())
        assert page.locator("#source-base").is_visible()
        assert page.locator("#web-public-name").is_visible()
        assert page.locator("#web-max-concurrency").input_value() == "3"
        assert not page.locator("#source-protocol").is_visible()
        page.locator("[data-close]").first.click()
        doubao = page.locator('.source-row[data-source="web-doubao"]')
        doubao.get_by_role("button", name="配置来源").click()
        assert "过滤 completion" in page.locator("#web-guide").inner_text()
        assert "发送一条新消息" in page.locator("#web-guide").inner_text()
        page.locator("[data-close]").first.click()
        kimi = page.locator('.source-row[data-source="web-kimi"]')
        kimi.get_by_role("button", name="配置来源").click()
        assert "ChatService/Chat" in page.locator("#web-guide").inner_text()
        assert "必须包含 Authorization" in page.locator("#web-guide").inner_text()
        page.locator("[data-close]").first.click()
        perplexity = page.locator('.source-row[data-source="web-perplexity"]')
        assert perplexity.locator('img[src="/assets/providers/perplexity.svg"]').count() == 1
        perplexity.get_by_role("button", name="配置来源").click()
        assert "过滤 perplexity_ask" in page.locator("#web-guide").inner_text()
        assert "x-pplx-account" in page.locator("#web-guide").inner_text()
        page.locator("[data-close]").first.click()
        wenxin = page.locator('.source-row[data-source="web-wenxin"]')
        assert "baidu_ai_logo" in (wenxin.locator(".provider-icon img").get_attribute("src") or "")
        wenxin.get_by_role("button", name="配置来源").click()
        assert "过滤 /aichat/api/conversation" in page.locator("#web-guide").inner_text()
        assert "chat_token" in page.locator("#source-curl").get_attribute("placeholder")
        page.locator("[data-close]").first.click()
        deepseek.get_by_role("button", name="配置来源").click()
        assert "deepseek-flash = deepseek-flash" in page.locator("#source-models").input_value()
        assert "claude-sonnet-4-5-20250929" not in page.locator("#source-models").input_value()
        assert page.locator("#source-icon-svg").input_value().startswith("<svg")
        page.locator("[data-close]").first.click()
        page.get_by_role("button", name="添加 API").click()
        assert page.locator("#source-base").is_visible()
        assert page.locator("#api-icon-editor").is_visible()
        assert page.locator("#source-icon-svg").input_value() == ""
        page.locator("[data-close]").first.click()
        kimi_switch = kimi.get_by_role("switch")
        initial_enabled = kimi_switch.get_attribute("aria-checked")
        toggled_enabled = "false" if initial_enabled == "true" else "true"
        kimi_switch.click()
        page.wait_for_function(f"document.querySelector('[data-source=\"web-kimi\"] [role=\"switch\"]')?.getAttribute('aria-checked') === '{toggled_enabled}'")
        page.locator('.source-row[data-source="web-kimi"]').get_by_role("switch").click()
        page.wait_for_function(f"document.querySelector('[data-source=\"web-kimi\"] [role=\"switch\"]')?.getAttribute('aria-checked') === '{initial_enabled}'")
        page.locator("#logs-toggle").click()
        assert "collapsed" in (page.locator("#logs-content").get_attribute("class") or "")
        page.reload(wait_until="networkidle")
        assert "collapsed" in (page.locator("#logs-content").get_attribute("class") or "")
        page.locator("#logs-toggle").click()
        assert "collapsed" not in (page.locator("#logs-content").get_attribute("class") or "")
        screenshot = os.environ.get("AIBRIDGE_UI_SCREENSHOT") or f"{temp_dir}/admin-smoke.png"
        page.screenshot(path=screenshot, full_page=True)
        browser.close()
    if console_errors:
        raise AssertionError(f"浏览器控制台错误：{console_errors}")
    print("ui-smoke-ok")


if __name__ == "__main__":
    main()
