from __future__ import annotations

import uuid

from playwright.sync_api import sync_playwright


def main() -> None:
    name = f"ui-token-smoke-{uuid.uuid4().hex[:8]}"
    key_id: str | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        try:
            page.goto("http://127.0.0.1:7009", wait_until="networkidle")
            legacy = page.locator("#keys [data-key-import]").first
            if legacy.count():
                legacy.click()
                assert page.locator("#token-import-dialog[open]").is_visible()
                page.locator("#token-import-value").fill("invalid-smoke-token")
                page.locator('#token-import-form [type="submit"]').click()
                page.get_by_text("Token 与现有记录不匹配").wait_for()
                page.locator("[data-close-token-import]").first.click()

            page.locator("#key-name").fill(name)
            page.locator("#create-key").click()
            page.locator("#token-dialog[open]").wait_for()
            token = page.locator("#new-token").inner_text()
            assert token.startswith("ab_")
            page.locator("[data-close-token]").click()

            page.reload(wait_until="networkidle")
            row = page.locator("#keys .list-row").filter(has_text=name)
            view = row.get_by_role("button", name=f"查看 {name} Token")
            key_id = view.get_attribute("data-key-view")
            assert key_id
            view.click()
            page.locator("#token-dialog[open]").wait_for()
            assert page.locator("#new-token").inner_text() == token
            page.locator("#token-dialog [data-copy]").click()
            assert page.evaluate("navigator.clipboard.readText()") == token
            page.locator("[data-close-token]").click()
            page.wait_for_function("document.querySelector('#new-token')?.textContent === ''")
            assert page.locator("#new-token").inner_text() == ""
        finally:
            if key_id:
                response = context.request.delete(f"http://127.0.0.1:7009/api/keys/{key_id}")
                assert response.ok
            browser.close()
    print("ui-token-smoke-ok")


if __name__ == "__main__":
    main()
