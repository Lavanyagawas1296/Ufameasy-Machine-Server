"""Restart only the throwaway localhost server while a customer page is open."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


async def main() -> None:
    pids = [int(value) for value in sys.argv[1:]]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(BASE + "/login")
        await page.locator("#username").fill(USER)
        await page.locator("#password").fill(PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url(BASE + "/")
        await page.locator("#statusLabel:not(.skeleton-text)").wait_for()
        before_url = page.url
        started = time.perf_counter()
        ids = ",".join(str(value) for value in pids)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Stop-Process -Id {ids} -Force -ErrorAction Stop"],
            check=True, capture_output=True,
        )
        await page.wait_for_timeout(1000)
        parent = subprocess.Popen(
            [sys.executable, str(ROOT / "isolated_server.py"), "--host", "127.0.0.1", "--port", "18000", "--timeout-keep-alive", "5"],
            cwd=ROOT.parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not healthy():
            await page.wait_for_timeout(250)
        server_ready_ms = round((time.perf_counter() - started) * 1000, 1)
        await page.wait_for_timeout(8_000)
        report = {
            "server_ready_ms": server_ready_ms,
            "page_url_before": before_url,
            "page_url_after": page.url,
            "still_authenticated": "/login" not in page.url,
            "status_after": await page.locator("#statusLabel").text_content(),
            "offline_banner_visible": await page.locator("#offlineNotice").is_visible(),
            "replacement_parent_pid": parent.pid,
        }
        await context.close()
        await browser.close()
    (ROOT / "raw" / "restart_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
