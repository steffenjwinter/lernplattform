#!/usr/bin/env python3
"""
Fängt POST-Body von update-post-progress ab um die Completion-API zu verstehen.
"""
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL    = "https://verkaufspsychologie-onlinekurs.mymemberspot.de"
LESSON_URL  = f"{BASE_URL}/library/P9KAAaH3H5J6gwPXiKwN/OabhDFJDxzr6Cz3elyGZ/PdUPTd4exhpaGEyUoyyw"
LOGIN_EMAIL = "sw@mitarbeiter.com"
LOGIN_PW    = "Mwiz123!"
LOG_FILE    = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/tuev_api.log")


async def main():
    LOG_FILE.write_text("")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=30)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()

        # Intercept ALL requests to memberspot API
        async def handle_request(route, request):
            if "client-api.memberspot.de" in request.url:
                body = ""
                try:
                    body = request.post_data or ""
                except:
                    pass
                with open(LOG_FILE, "a") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"METHOD: {request.method}\n")
                    f.write(f"URL: {request.url}\n")
                    f.write(f"HEADERS: {json.dumps(dict(request.headers), indent=2)}\n")
                    f.write(f"BODY: {body}\n")
                print(f"[API] {request.method} {request.url}")
                if body:
                    print(f"      BODY: {body[:200]}")
            await route.continue_()

        await page.route("**/*", handle_request)

        # Login
        await page.goto(f"{BASE_URL}/library", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        if "login" in page.url:
            await page.wait_for_selector("input.min-w-input", timeout=15000)
            await page.fill("input.min-w-input", LOGIN_EMAIL)
            await page.fill("input.cypress__login__password", LOGIN_PW)
            btn = await page.query_selector("button[type='submit']")
            if btn: await btn.click()
            else: await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        print(f"Eingeloggt: {page.url}")

        # Navigate to lesson and watch video to completion
        print(f"\nNavigiere zu Lektion...")
        await page.goto(LESSON_URL, wait_until="domcontentloaded", timeout=45000)

        # Wait for video to load then seek to near end
        for attempt in range(20):
            await page.wait_for_timeout(500)
            result = await page.evaluate("""
                () => {
                    const v = document.querySelector('video');
                    if (!v || !v.duration || v.duration === 0) return null;
                    return v.duration;
                }
            """)
            if result:
                print(f"Video geladen: {result:.1f}s")
                break

        if result:
            seek_result = await page.evaluate("""
                (dur) => {
                    const v = document.querySelector('video');
                    v.currentTime = dur - 2;
                    v.play();
                    return true;
                }
            """, result)
            print(f"Video auf {result-2:.1f}s gesetzt, spielt ab...")
            await page.wait_for_timeout(6000)
        else:
            print("Kein Video gefunden, warte 5s...")
            await page.wait_for_timeout(5000)

        print("\nAlle API-Calls in tuev_api.log gespeichert.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
