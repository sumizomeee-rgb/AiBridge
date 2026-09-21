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
        assert page.locator(".source-card").count() >= 5
        deepseek = page.locator(".source-card", has_text="DeepSeek 官方")
        assert deepseek.locator(".status").inner_text() == "可用"
        qwen = page.locator(".source-card", has_text="千问 Web")
        qwen.get_by_role("button", name="配置").click()
        assert "F12" in page.locator("#web-guide").inner_text()
        page.locator("[data-close]").first.click()
        page.get_by_role("button", name="添加标准 API").click()
        assert page.locator("#source-base").is_visible()
        page.locator("[data-close]").first.click()
        page.screenshot(path=f"{temp_dir}/admin-smoke.png", full_page=True)
        browser.close()
    if console_errors:
        raise AssertionError(f"浏览器控制台错误：{console_errors}")
    print("ui-smoke-ok")


if __name__ == "__main__":
    main()
