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
        assert page.get_by_role("heading", name="统一模型入口").is_visible()
        assert page.get_by_text("一个地址，接入所有模型。").count() == 0
        assert page.locator(".source-row").count() >= 5
        assert page.locator(".source-switch").count() == page.locator(".source-row").count()
        assert page.locator(".source-row .provider-icon svg").count() >= 5
        deepseek = page.locator(".source-row", has_text="DeepSeek 官方")
        assert deepseek.locator(".status").inner_text() == "可用"
        qwen = page.locator(".source-row", has_text="千问 Web")
        qwen.get_by_role("button", name="配置来源").click()
        assert "F12" in page.locator("#web-guide").inner_text()
        assert "api/v2/chat/completions" in page.locator("#web-guide").inner_text()
        assert page.locator("#source-base").is_visible()
        assert page.locator("#web-public-name").is_visible()
        assert not page.locator("#source-protocol").is_visible()
        page.locator("[data-close]").first.click()
        doubao = page.locator(".source-row", has_text="豆包 Web")
        doubao.get_by_role("button", name="配置来源").click()
        assert "历史回复不会补录" in page.locator("#web-guide").inner_text()
        assert "发送一条全新消息" in page.locator("#web-guide").inner_text()
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
        kimi = page.locator(".source-row", has_text="Kimi Web")
        kimi_switch = kimi.get_by_role("switch")
        initial_enabled = kimi_switch.get_attribute("aria-checked")
        toggled_enabled = "false" if initial_enabled == "true" else "true"
        kimi_switch.click()
        page.wait_for_function(f"document.querySelector('[data-source=\"web-kimi\"] [role=\"switch\"]')?.getAttribute('aria-checked') === '{toggled_enabled}'")
        page.locator(".source-row", has_text="Kimi Web").get_by_role("switch").click()
        page.wait_for_function(f"document.querySelector('[data-source=\"web-kimi\"] [role=\"switch\"]')?.getAttribute('aria-checked') === '{initial_enabled}'")
        page.locator("#logs-toggle").click()
        assert not page.locator("#logs-content").is_visible()
        page.reload(wait_until="networkidle")
        assert not page.locator("#logs-content").is_visible()
        page.locator("#logs-toggle").click()
        assert page.locator("#logs-content").is_visible()
        page.screenshot(path=f"{temp_dir}/admin-smoke.png", full_page=True)
        browser.close()
    if console_errors:
        raise AssertionError(f"浏览器控制台错误：{console_errors}")
    print("ui-smoke-ok")


if __name__ == "__main__":
    main()
