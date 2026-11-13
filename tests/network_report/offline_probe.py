"""Browser blackout/recovery check against the isolated server only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        await page.goto(BASE + "/login")
        await page.locator("#username").fill(USER)
        await page.locator("#password").fill(PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url(BASE + "/")
        await page.locator("#statusLabel:not(.skeleton-text)").wait_for()
        before = await page.locator("#statusLabel").text_content()
        await context.set_offline(True)
        await page.wait_for_timeout(11_000)
        stale = await page.locator("#offlineNotice").is_visible()
        during = await page.locator("#statusLabel").text_content()
        await page.screenshot(path=str(ROOT / "screenshots" / "offline_11s_stale.png"), full_page=True)
        await context.set_offline(False)
        await page.wait_for_timeout(15_000)
        report = {
            "status_before": before,
            "status_during": during,
            "stale_after_11s": stale,
            "recovered_after_15s": not await page.locator("#offlineNotice").is_visible(),
            "console_errors": errors,
        }
        await context.close()
        await browser.close()
    (ROOT / "raw" / "offline_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
