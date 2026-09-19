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
                page.locator('[name="news_api_key"]').fill("browser-news-token")
                page.get_by_role("button", name="Test saved news connection").click()
                expect(page.locator("#toast")).to_contain_text("Save connections first")
                page.locator('[name="model"]').fill("test-model")
                page.get_by_role("button", name="Save connections").click()
                page.locator("#api-key-hint").filter(has_text="API key saved").wait_for()
                expect(page.locator('[name="api_key"]')).to_have_value("")
                page.get_by_role("button", name="Generate / rotate debug token").click()
                expect(page.locator("#debug-token-status")).to_contain_text("Debug token active")
                debug_token = page.locator("#debug-token").input_value()
                assert debug_token.startswith("moose_debug_")
                assert (
                    httpx.get(
                        root + "/api/debug", headers={"Authorization": "Bearer " + debug_token}
                    ).status_code
                    == 200
                )
                page.get_by_role("button", name="Revoke token", exact=True).click()
                expect(page.locator("#debug-token-status")).to_contain_text("No debug token")
                assert (
                    httpx.get(
                        root + "/api/debug", headers={"Authorization": "Bearer " + debug_token}
                    ).status_code
                    == 401
                )
                page.locator('nav a[href="#chats"]').click()
                page.locator('#chat-form [name="title"]').fill("Test team")
                page.locator('#chat-form [name="id"]').fill("-100999")
                page.get_by_role("button", name="+ Allow chat").click()
                page.get_by_role("button", name="Revoke access").wait_for()
                page.get_by_role("button", name="Revoke access").click()
                page.locator(".chat-toggle").filter(has_text="Allow chat").wait_for()
                page.locator('nav a[href="#agent"]').click()
                page.get_by_role("button", name="Refresh from conversation history").click()
                expect(page.locator("#toast")).to_contain_text("No retained conversation text")
                page.locator('[name="system_prompt"]').fill("Test system prompt")
                page.locator('[name="agent_names"]').fill("moose, лось, лосик")
                page.locator('[name="name_mention_probability"]').fill("0.65")
                page.locator('[name="natural_reply_min_context"]').fill("150")
                page.locator('[name="memory_review_max_tokens"]').fill("6000")
                expect(page.locator('[name="memory_review_context_chars"]')).to_have_value("4000")
                page.locator('[name="memory_review_context_chars"]').fill("3000")
                page.locator('[name="news_enabled"]').check()
                page.locator('[name="news_daily_limit"]').fill("0")
                page.locator('[name="consciousness"]').fill("Friends enjoy tea.")
                page.locator('[name="consciousness_prompt"]').fill("Remember useful reflections.")
                page.locator('[name="proactive_timezone"]').fill("Europe/London")
                page.locator('[name="daytime_start"]').fill("10")
                page.locator('[name="daytime_end"]').fill("20")
                page.locator('[name="history_tool_calls"]').fill("4")
                page.locator('[name="media_tool_calls"]').fill("2")
                page.locator('[name="participation_probability"]').fill("0.08")
                page.locator('[name="wake_after_hours"]').fill("72")
                page.locator('[name="wake_prompt"]').fill("Offer a cheerful hello.")
                page.locator('[name="image_delivery"]').select_option("document")
                page.get_by_role("button", name="Save agent settings").click()
                page.locator("#toast").filter(has_text="Settings saved").wait_for()
                page.reload()
                expect(page.locator('[name="system_prompt"]')).to_have_value("Test system prompt")
                expect(page.locator('[name="agent_names"]')).to_have_value("moose, лось, лосик")
                expect(page.locator('[name="name_mention_probability"]')).to_have_value("0.65")
                expect(page.locator('[name="natural_reply_min_context"]')).to_have_value("150")
                expect(page.locator('[name="news_api_key"]')).to_have_value("")
                expect(page.locator('[name="news_enabled"]')).to_be_checked()
                expect(page.locator("#news-api-key-hint")).to_contain_text("News token saved")
                page.locator('nav a[href="#connection"]').click()
                page.get_by_role("button", name="Test saved news connection").click()
                expect(page.locator("#toast")).to_contain_text("daily_limit")
                page.locator('nav a[href="#agent"]').click()
                expect(page.locator('[name="consciousness"]')).to_have_value("Friends enjoy tea.")
                expect(page.locator("#prefill-consciousness")).to_be_enabled()
                expect(page.locator('[name="memory_review_max_tokens"]')).to_have_value("6000")
                expect(page.locator('[name="memory_review_context_chars"]')).to_have_value("3000")
                expect(page.locator('[name="consciousness_prompt"]')).to_have_value(
                    "Remember useful reflections."
                )
                # An agent update must survive a save from a dashboard with older settings.
                httpx.patch(
                    root + "/api/settings",
                    headers={"X-Moose-Request": "1"},
                    json={"consciousness": "New agent reflection."},
                ).raise_for_status()
                page.locator('[name="temperature"]').fill("0.9")
                page.get_by_role("button", name="Save agent settings").click()
                expect(page.locator('[name="consciousness"]')).to_have_value(
                    "New agent reflection."
                )
                expect(page.locator('[name="proactive_timezone"]')).to_have_value("Europe/London")
                expect(page.locator('[name="daytime_start"]')).to_have_value("10")
                expect(page.locator('[name="daytime_end"]')).to_have_value("20")
                expect(page.locator('[name="history_tool_calls"]')).to_have_value("4")
                expect(page.locator('[name="media_tool_calls"]')).to_have_value("2")
                expect(page.locator('[name="wake_after_hours"]')).to_have_value("72")
                expect(page.locator('[name="participation_probability"]')).to_have_value("0.08")
                expect(page.locator('[name="wake_prompt"]')).to_have_value(
                    "Offer a cheerful hello."
                )
                expect(page.locator('[name="image_delivery"]')).to_have_value("document")
                page.screenshot(path="test-results/agent-settings-desktop.png", full_page=True)
                page.locator('nav a[href="#playground"]').click()
                page.get_by_role("button", name="Run prompt").click()
                page.locator("#playground-output").filter(has_text="Enter a prompt").wait_for()
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('nav a[href="#overview"]').click()
                page.screenshot(path="test-results/dashboard-mobile.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator('nav a[href="#agent"]').click()
                page.screenshot(path="test-results/agent-settings-mobile.png", full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                def activity_state(route):
                    response = route.fetch()
                    data = response.json()
                    data["activity"] = [
                        {
                            "id": 123,
                            "time": "2026-09-19T10:00:00Z",
                            "chat": "Test chat",
                            "status": "success",
                            "detail": "Replied · 0 files · 2 tool calls",
                            "reply_messages": [
                                "Hello <script>window.activityInjected=true</script>",
                                "Second reply",
                            ],
                            "tools_used": ["read_news", "read_consciousness"],
                        }
                    ]
                    route.fulfill(response=response, json=data)

                page.route("**/api/state", activity_state)
                page.evaluate("refresh()")
                page.locator('nav a[href="#activity"]').click()
                details = page.locator("#activity-list .event-details")
                details.locator("summary").click()
                expect(details.locator(".event-reply").first).to_have_text(
                    "Hello <script>window.activityInjected=true</script>"
                )
                expect(details.locator(".event-tools")).to_contain_text(
                    "read_news → read_consciousness"
                )
                page.evaluate("refresh()")
                expect(details).to_have_attribute("open", "")
                assert not page.evaluate("Boolean(window.activityInjected)")
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
