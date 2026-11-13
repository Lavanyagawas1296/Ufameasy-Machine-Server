"""Local browser reliability probe. Writes only report artifacts."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"
PROFILES = {
    "A_good_lan": {"latency": 0, "down": -1, "loss": 0},
    "B_office_wifi": {"latency": 20, "down": 20 * 1024 * 1024 // 8, "loss": 1},
    "C_factory_wifi": {"latency": 150, "down": 2 * 1024 * 1024 // 8, "loss": 3},
    "D_bad": {"latency": 500, "down": 256 * 1024 // 8, "loss": 10},
}


async def shape(page, profile: dict) -> None:
    session = await page.context.new_cdp_session(page)
    await session.send("Network.enable")
    params = {
        "offline": False,
        "latency": profile["latency"],
        "downloadThroughput": profile["down"],
        "uploadThroughput": profile["down"],
        "connectionType": "wifi",
        "packetLoss": profile["loss"],
    }
    try:
        await session.send("Network.emulateNetworkConditions", params)
    except Exception:
        params.pop("packetLoss")
        await session.send("Network.emulateNetworkConditions", params)


async def login(page) -> None:
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30_000)
    await page.locator("#username").fill(USER)
    await page.locator("#password").fill(PASSWORD)
    await page.locator("button[type=submit]").click()
    await page.wait_for_url(f"{BASE}/", timeout=30_000)


async def page_snapshot(page) -> dict:
    return await page.evaluate(
        """() => ({
          resources: performance.getEntriesByType('resource').map(x => ({name:x.name, transferSize:x.transferSize})),
          status: document.querySelector('#statusLabel')?.textContent,
          recent: document.querySelectorAll('#recentEvents li').length,
          rows: document.querySelectorAll('#logsBody tr').length,
          history: document.querySelectorAll('#historyBody tr').length
        })"""
    )


def insert_test_state() -> None:
    db_path = ROOT / "sandbox" / "telemetry.u5d"
    conn = sqlite3.connect(db_path)
    try:
        event_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM telemetry_events").fetchone()[0]
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        timestamp = now.strftime("%d/%m/%Y %I:%M:%S.%f %p")
        payload = {
            "event_id": str(uuid.uuid4()),
            "session_id": "network-report",
            "timestamp": timestamp,
            "category": "telemetry",
            "severity": "INFO",
            "source": "network_report",
            "parameter_key": "execution_state",
            "new_value": "idle",
            "event_type": "StateChangedEvent",
        }
        conn.execute(
            """INSERT INTO telemetry_events
               (id,event_id,timestamp,category,severity,source,session_id,event_type,payload)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (event_id, payload["event_id"], timestamp, "telemetry", "INFO", "network_report", "network-report", "StateChangedEvent", json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()


async def probe_profile(playwright, name: str, profile: dict) -> dict:
    browser = await playwright.chromium.launch()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        record_har_path=str(ROOT / "har" / f"{name}.har"),
        record_har_content="embed",
    )
    page = await context.new_page()
    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    await shape(page, profile)
    started = time.perf_counter()
    await login(page)
    await page.wait_for_selector("#statusLabel:not(.skeleton-text)", timeout=30_000)
    overview_ms = round((time.perf_counter() - started) * 1000, 1)
    await page.screenshot(path=str(ROOT / "screenshots" / f"{name}_overview.png"), full_page=True)
    overview = await page_snapshot(page)

    started = time.perf_counter()
    await page.goto(f"{BASE}/#logs", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1200)
    logs_ms = round((time.perf_counter() - started) * 1000, 1)
    logs = await page_snapshot(page)
    if name == "A_good_lan":
        await page.locator("[data-log-range=custom]").click()
        await page.locator("#logsFrom").fill("2026-11-10T20:00")
        await page.locator("#logsTo").fill("2026-11-10T21:30")
        await page.locator("#applyLogs").click()
        await page.wait_for_timeout(1000)
        logs["custom_rows"] = await page.locator("#logsBody tr").count()
        await page.screenshot(path=str(ROOT / "screenshots" / "A_good_lan_logs_custom.png"), full_page=True)

    started = time.perf_counter()
    await page.goto(f"{BASE}/#history", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(1200)
    history_ms = round((time.perf_counter() - started) * 1000, 1)
    history = await page_snapshot(page)
    if name == "A_good_lan":
        await page.screenshot(path=str(ROOT / "screenshots" / "A_good_lan_history.png"), full_page=True)

    result = {
        "profile": name,
        "overview_ms": overview_ms,
        "logs_ms": logs_ms,
        "history_ms": history_ms,
        "overview": overview,
        "logs": logs,
        "history": history,
        "console_errors": console_errors,
    }
    await context.close()
    await browser.close()
    return result


async def offline_and_websocket(playwright) -> dict:
    browser = await playwright.chromium.launch()
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await context.new_page()
    await login(page)
    await page.wait_for_selector("#statusLabel:not(.skeleton-text)")
    insert_test_state()
    await page.get_by_text("Idle", exact=True).wait_for(timeout=8_000)
    websocket_idle_ms = await page.evaluate("() => document.querySelector('#statusLabel').textContent")
    await context.set_offline(True)
    await page.wait_for_timeout(7_000)
    notice = await page.locator("#offlineNotice").is_visible()
    await page.screenshot(path=str(ROOT / "screenshots" / "F_blackout_stale.png"), full_page=True)
    await context.set_offline(False)
    await page.wait_for_timeout(4_000)
    recovered = not await page.locator("#offlineNotice").is_visible()
    result = {"websocket_state": websocket_idle_ms, "offline_notice_after_7s": notice, "recovered_after_4s": recovered}
    await context.close()
    await browser.close()
    return result


async def main() -> None:
    async with async_playwright() as playwright:
        results = {name: await probe_profile(playwright, name, profile) for name, profile in PROFILES.items()}
        results["offline_websocket"] = await offline_and_websocket(playwright)
    (ROOT / "raw" / "playwright_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
