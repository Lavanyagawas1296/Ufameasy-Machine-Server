"""Verify customer custom-log and file UI flows against localhost only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"


async def login(page) -> None:
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    await page.locator("#username").fill(USER)
    await page.locator("#password").fill(PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url(f"{BASE}/")


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await login(page)

        await page.goto(f"{BASE}/#logs", wait_until="domcontentloaded")
        await page.locator("#logsBody tr").first.wait_for(timeout=20_000)
        default_rows = await page.locator("#logsBody tr").count()
        await page.locator("[data-log-range=custom]").click()
        await page.locator("#logsFrom").fill("2026-10-01T00:00")
        await page.locator("#logsTo").fill("2026-11-11T23:59")
        async with page.expect_response(lambda response: "/api/customer/logs?" in response.url and response.status == 200):
            await page.locator("#applyLogs").click()
        await page.locator("#logsBody tr").first.wait_for(timeout=20_000)
        custom_rows = await page.locator("#logsBody tr").count()
        logs_download_href = await page.locator("#downloadLogs").get_attribute("href")
        await page.screenshot(path=str(ROOT / "screenshots" / "focused_custom_logs.png"), full_page=True)

        await page.goto(f"{BASE}/#history", wait_until="domcontentloaded")
        view = page.locator('#historyBody button:has-text("View"):not([disabled])').first
        await view.wait_for(timeout=20_000)
        available_rows = await page.locator('#historyBody button:has-text("View"):not([disabled])').count()
        await view.click()
        await page.locator("#fileModal[open]").wait_for(timeout=10_000)
        await page.wait_for_function("() => document.querySelector('#filePreview')?.textContent && !document.querySelector('#filePreview').textContent.includes('Loading')")
        preview = (await page.locator("#filePreview").text_content()) or ""
        file_download_href = await page.locator("#historyBody a[download]").first.get_attribute("href")
        await page.screenshot(path=str(ROOT / "screenshots" / "focused_history_file.png"), full_page=True)
        report = {
            "default_log_rows": default_rows,
            "custom_log_rows": custom_rows,
            "logs_download_href_present": bool(logs_download_href),
            "available_history_rows": available_rows,
            "preview_loaded": bool(preview) and "not available" not in preview.lower(),
            "file_download_href_present": bool(file_download_href),
            "console_errors": errors,
        }
        await context.close()
        await browser.close()
    (ROOT / "raw" / "focused_ui_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
