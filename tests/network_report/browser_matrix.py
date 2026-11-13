"""Chrome/Edge layout and console smoke checks on the isolated server."""

from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"
BROWSERS = {
    "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "edge": r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
}
VIEWPORTS = {"desktop": {"width": 1280, "height": 800}, "mobile": {"width": 360, "height": 740}}
PROFILES = {"A": {"latency": 0, "down": -1, "loss": 0}, "C": {"latency": 150, "down": 2 * 1024 * 1024 // 8, "loss": 3}}


async def login(page) -> None:
    await page.goto(BASE + "/login", wait_until="domcontentloaded")
    await page.locator("#username").fill(USER)
    await page.locator("#password").fill(PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url(BASE + "/", timeout=30_000)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=tuple(BROWSERS))
    parser.add_argument("--profile", choices=tuple(PROFILES))
    parser.add_argument("--viewport", choices=tuple(VIEWPORTS))
    args = parser.parse_args()
    results = []
    async with async_playwright() as playwright:
        browser_items = [(args.browser, BROWSERS[args.browser])] if args.browser else BROWSERS.items()
        profile_items = [(args.profile, PROFILES[args.profile])] if args.profile else PROFILES.items()
        viewport_items = [(args.viewport, VIEWPORTS[args.viewport])] if args.viewport else VIEWPORTS.items()
        for browser_name, executable in browser_items:
            for profile_name, profile in profile_items:
                for viewport_name, viewport in viewport_items:
                    browser = await playwright.chromium.launch(executable_path=executable)
                    context = await browser.new_context(viewport=viewport)
                    page = await context.new_page()
                    session = await context.new_cdp_session(page)
                    await session.send("Network.enable")
                    shaped = {"offline": False, "latency": profile["latency"], "downloadThroughput": profile["down"], "uploadThroughput": profile["down"], "connectionType": "wifi", "packetLoss": profile["loss"]}
                    try:
                        await session.send("Network.emulateNetworkConditions", shaped)
                    except Exception:
                        shaped.pop("packetLoss")
                        await session.send("Network.emulateNetworkConditions", shaped)
                    errors: list[str] = []
                    not_found: list[str] = []
                    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
                    page.on("response", lambda response: not_found.append(response.url) if response.status == 404 else None)
                    await login(page)
                    await page.locator("#statusLabel:not(.skeleton-text)").wait_for(timeout=30_000)
                    await page.keyboard.press("Tab")
                    result = {
                        "browser": browser_name, "profile": profile_name, "viewport": viewport_name,
                        "status": await page.locator("#statusLabel").text_content(),
                        "horizontal_overflow": await page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth"),
                        "keyboard_focus_present": await page.evaluate("() => !!document.activeElement && document.activeElement !== document.body"),
                        "console_errors": errors,
                        "not_found": not_found,
                    }
                    if browser_name == "chrome" and profile_name == "C" and viewport_name == "mobile":
                        await page.screenshot(path=str(ROOT / "screenshots" / "matrix_chrome_C_mobile.png"), full_page=True)
                    results.append(result)
                    await context.close()
                    await browser.close()
    suffix = "_".join(part for part in (args.browser, args.profile, args.viewport) if part) or "all"
    (ROOT / "raw" / f"browser_matrix_{suffix}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
