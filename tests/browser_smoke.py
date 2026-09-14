"""Run directly: .venv/bin/python tests/browser_smoke.py (requires local Chrome)."""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="moose-browser-") as directory:
        environment = {
            **os.environ,
            "DATA_DIR": directory,
            "ADMIN_PASSWORD": "",
            "ALLOWED_HOSTS": "127.0.0.1",
        }
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-proxy-headers",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    if httpx.get(root).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    headless=True,
                )
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1100}, device_scale_factor=1
                )
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(root)
                page.locator("#setup-count").filter(has_text="0 of 3 complete").wait_for()
                Path("test-results").mkdir(exist_ok=True)
                page.screenshot(path="test-results/dashboard-desktop.png", full_page=True)
                page.locator('nav a[href="#connection"]').click()
                page.locator('[name="bot_token"]').fill("test-token")
                page.locator('[name="api_key"]').fill("test-secret")
                page.locator('[name="model"]').fill("test-model")
                page.get_by_role("button", name="Save connections").click()
                page.locator("#api-key-hint").filter(has_text="API key saved").wait_for()
                expect(page.locator('[name="api_key"]')).to_have_value("")
                page.locator('nav a[href="#chats"]').click()
                page.locator('#chat-form [name="title"]').fill("Test team")
                page.locator('#chat-form [name="id"]').fill("-100999")
                page.get_by_role("button", name="+ Allow chat").click()
                page.get_by_role("button", name="Revoke access").wait_for()
                page.get_by_role("button", name="Revoke access").click()
                page.locator(".chat-toggle").filter(has_text="Allow chat").wait_for()
                page.locator('nav a[href="#agent"]').click()
                page.locator('[name="system_prompt"]').fill("Test system prompt")
                page.get_by_role("button", name="Save agent settings").click()
                page.locator("#toast").filter(has_text="Settings saved").wait_for()
                page.reload()
                expect(page.locator('[name="system_prompt"]')).to_have_value("Test system prompt")
                page.locator('nav a[href="#playground"]').click()
                page.get_by_role("button", name="Run prompt").click()
                page.locator("#playground-output").filter(has_text="Enter a prompt").wait_for()
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('nav a[href="#overview"]').click()
                page.screenshot(path="test-results/dashboard-mobile.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not errors, errors
                browser.close()
                print(
                    "Browser smoke passed: setup, secret masking, chat access, persistence, playground errors, mobile layout; no JS errors."
                )
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
