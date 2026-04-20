#!/usr/bin/env python3
"""
Analysiert Memberspot Completion-API und versucht Lektionen zu entsperren.
Öffnet die Lektionsseite, spielt Video bis Ende, fängt API-Call ab.
"""

import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL    = "https://verkaufspsychologie-onlinekurs.mymemberspot.de"
LESSON_URLS = [
    f"{BASE_URL}/library/P9KAAaH3H5J6gwPXiKwN/OabhDFJDxzr6Cz3elyGZ/PdUPTd4exhpaGEyUoyyw",  # Starte hier
    f"{BASE_URL}/library/P9KAAaH3H5J6gwPXiKwN/OabhDFJDxzr6Cz3elyGZ/xZinircP4-O2z_ZPW5ckX",  # Tutorial
]
LOGIN_EMAIL = "sw@mitarbeiter.com"
LOGIN_PW    = "Mwiz123!"
LOG_FILE    = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/tuev_unlock.log")


def log(msg):
    print(msg)
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


async def login(page):
    await page.goto(f"{BASE_URL}/library", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    if "login" in page.url or "auth" in page.url:
        await page.wait_for_selector("input.min-w-input", timeout=15000)
        await page.fill("input.min-w-input", LOGIN_EMAIL)
        await page.fill("input.cypress__login__password", LOGIN_PW)
        await page.wait_for_timeout(400)
        btn = await page.query_selector("button[type='submit']")
        if btn: await btn.click()
        else: await page.keyboard.press("Enter")
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    log(f"Eingeloggt: {page.url}")


async def try_complete_lesson(page, url: str):
    log(f"\nNavigiere zu: {url}")
    api_calls = []

    def on_request(req):
        u = req.url
        if any(x in u for x in ["firestore", "firebase", "progress", "complete", "watched", "mspot"]):
            if "b-cdn.net" not in u and "vtt" not in u and ".mp4" not in u:
                api_calls.append({"url": u, "method": req.method})

    def on_response(resp):
        u = resp.url
        if any(x in u for x in ["firestore", "firebase", "progress", "complete", "watched"]):
            if "b-cdn.net" not in u:
                log(f"  API Response: {resp.status} {u[:120]}")

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)

        # Wait for video to load, then seek to near end
        video_ok = await page.evaluate("""
            async () => {
                const v = document.querySelector('video');
                if (!v) return 'no-video';
                // Wait up to 5s for video to have a duration
                for (let i = 0; i < 50; i++) {
                    if (v.duration && v.duration > 0) break;
                    await new Promise(r => setTimeout(r, 100));
                }
                if (!v.duration) return 'no-duration';
                const dur = v.duration;
                v.currentTime = Math.max(0, dur - 3);
                v.play();
                return `seeked to ${v.currentTime.toFixed(1)}s of ${dur.toFixed(1)}s`;
            }
        """)
        log(f"  Video: {video_ok}")

        # Wait for video to finish and completion event to fire
        await page.wait_for_timeout(8000)

        # Also try clicking any complete/next button
        for sel in [
            "button[data-cy='markCompleteButton']",
            "button:has-text('Abschließen')",
            "button:has-text('Als abgeschlossen')",
            "button:has-text('Weiter')",
            "button:has-text('Nächste')",
            "button:has-text('Mark complete')",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    text = (await btn.inner_text()).strip()
                    log(f"  Klicke Button: '{text}'")
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                pass

    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)

    log(f"  API-Calls abgefangen: {len(api_calls)}")
    for c in api_calls[:10]:
        log(f"    {c['method']} {c['url'][:120]}")


async def check_unlocked(page) -> list:
    """Navigate back to course and check how many chapters/lessons are now available."""
    await page.goto(f"{BASE_URL}/library/P9KAAaH3H5J6gwPXiKwN", wait_until="domcontentloaded", timeout=45000)
    # Scroll to load all
    for _ in range(10):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)

    content = await page.content()
    available = re.findall(
        r'data-cy="mediaLectureAvailiableLinkItem"[^>]*href="(/library/[^"]+)"',
        content
    )
    titles = re.findall(r'mediaLectureCardTitle[^>]*>([^<]+)', content)
    locked = content.count('mediaLectureUnavailableDivItem')
    log(f"\nKursstand: {len(available)} verfügbar, {locked} gesperrt")
    log(f"Titel: {[t.strip() for t in titles]}")
    return available


async def main():
    LOG_FILE.write_text("")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=30)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await login(page)

        log("\nVorher:")
        before = await check_unlocked(page)

        for url in LESSON_URLS:
            await try_complete_lesson(page, url)

        log("\nNachher:")
        after = await check_unlocked(page)

        new = set(after) - set(before)
        log(f"\nNeu freigeschaltet: {len(new)} Lektionen")
        for u in new:
            log(f"  {u}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
