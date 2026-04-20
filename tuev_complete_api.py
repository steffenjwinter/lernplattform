#!/usr/bin/env python3
"""
Ruft update-post-progress direkt aus dem Browser-Kontext auf (frisches JWT, kein CORS).
Fängt zunächst den echten API-Call ab um Payload-Format zu lernen,
dann repliziert es für gesperrte Lektionen.
"""
import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL    = "https://verkaufspsychologie-onlinekurs.mymemberspot.de"
SCHOOL      = "UREn3qhVDnOLNJ7BmKum"
COURSE      = "P9KAAaH3H5J6gwPXiKwN"
CHAPTER     = "OabhDFJDxzr6Cz3elyGZ"
# Available lessons (already scraped):
AVAILABLE   = ["PdUPTd4exhpaGEyUoyyw", "xZinircP4-O2z_ZPW5ckX"]
# Locked lessons in "Starte hier" chapter:
LOCKED      = ["WkbxfHAJCN5X7Jbt1s5Y", "Rvur3xBjTwJ9kl0p2aDP"]  # placeholder IDs

LOGIN_EMAIL = "sw@mitarbeiter.com"
LOGIN_PW    = "Mwiz123!"
LOG_FILE    = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/tuev_complete.log")


def log(msg):
    print(msg)
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


async def login(page):
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


async def get_jwt(page):
    """Extract current Firebase JWT from localStorage or IndexedDB."""
    token = await page.evaluate("""
        () => {
            // Try localStorage keys
            for (let i = 0; i < localStorage.length; i++) {
                const key = localStorage.key(i);
                const val = localStorage.getItem(key);
                if (val && val.startsWith('eyJ')) return val;
            }
            return null;
        }
    """)
    return token


async def complete_lesson_via_fetch(page, lesson_url, post_id, captured_payloads):
    """Navigate to lesson, spy on the update-post-progress call, capture payload."""
    log(f"\n  → Navigiere zu Lektion: {post_id}")

    found_payload = {"data": None}

    async def intercept(route, request):
        if "update-post-progress" in request.url:
            body = request.post_data or ""
            log(f"  !! Abgefangen: {request.method} {request.url}")
            log(f"     Body: {body}")
            try:
                found_payload["data"] = json.loads(body)
            except:
                found_payload["data"] = body
            captured_payloads.append(found_payload["data"])
        await route.continue_()

    await page.route("**/*", intercept)

    try:
        await page.goto(lesson_url, wait_until="domcontentloaded", timeout=45000)

        # Wait for video, seek to end
        for _ in range(30):
            await page.wait_for_timeout(500)
            result = await page.evaluate("() => { const v = document.querySelector('video'); return v && v.duration > 0 ? v.duration : null; }")
            if result: break

        if result:
            log(f"  Video: {result:.1f}s — spule vor...")
            await page.evaluate("""
                (dur) => {
                    const v = document.querySelector('video');
                    v.currentTime = dur - 1.5;
                    v.play();
                }
            """, result)
            await page.wait_for_timeout(5000)
        else:
            log(f"  Kein Video gefunden (Text-Lektion)")
            await page.wait_for_timeout(3000)

    finally:
        await page.unroute("**/*", intercept)

    return found_payload["data"]


async def call_complete_api(page, jwt, payload):
    """Call update-post-progress from browser JS context with custom payload."""
    result = await page.evaluate("""
        async (jwt, payload) => {
            try {
                const resp = await fetch('https://client-api.memberspot.de/school-user-progress/update-post-progress', {
                    method: 'POST',
                    headers: {
                        'Authorization': jwt,
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'app': 'client',
                        'app-version': '2026-04-19-1011/server-client-frontend'
                    },
                    body: JSON.stringify(payload)
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch(e) {
                return { error: e.message };
            }
        }
    """, jwt, payload)
    return result


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

        # ── Step 1: Navigate to a known lesson to capture the real payload ─
        log("\n=== Schritt 1: Echten API-Call abfangen ===")
        captured = []
        lesson1_url = f"{BASE_URL}/library/{COURSE}/{CHAPTER}/{AVAILABLE[0]}"
        payload = await complete_lesson_via_fetch(page, lesson1_url, AVAILABLE[0], captured)

        if payload:
            log(f"\nEchter Payload: {json.dumps(payload, indent=2)}")
        else:
            log("\nKein Payload abgefangen — versuche JS-fetch direkt")

        # ── Step 2: Get current JWT ────────────────────────────────────────
        await page.goto(f"{BASE_URL}/library/{COURSE}", wait_until="domcontentloaded", timeout=30000)
        jwt = await get_jwt(page)
        if not jwt:
            # Extract from request headers via evaluation
            jwt = await page.evaluate("""
                () => {
                    // Angular/Firebase stores token in IndexedDB or memory
                    // Try to get from the app's auth service
                    try {
                        const app = window.ng?.getComponent(document.querySelector('app-root'));
                        return null; // placeholder
                    } catch(e) { return null; }
                }
            """)

        log(f"\nJWT gefunden: {'ja' if jwt else 'nein'}")

        # ── Step 3: Check current chapter to find locked lesson IDs ───────
        await page.goto(f"{BASE_URL}/library/{COURSE}/{CHAPTER}",
                       wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(2000)
        ch_html = await page.content()

        # Find locked lesson IDs from page
        # Locked lessons show as div items, their IDs might be in the DOM
        locked_hrefs = re.findall(
            r'data-cy="mediaLectureUnavailableDivItem"[^>]*id="([^"]+)"', ch_html
        )
        all_available = re.findall(
            r'data-cy="mediaLectureAvailiableLinkItem"[^>]*href="(/library/[^"]+)"', ch_html
        )
        all_titles = re.findall(r'mediaLectureCardTitle[^>]*>([^<]+)', ch_html)

        log(f"\nKapitel-Stand: {len(all_available)} verfügbar, locked IDs: {locked_hrefs}")
        log(f"Alle Titel: {[t.strip() for t in all_titles]}")
        log(f"Captured payload: {captured}")

        # ── Step 4: Call API for lesson 1 (if not already completed) ──────
        if captured and jwt:
            # Use the captured payload structure for other lessons
            for avail_href in all_available:
                post_id = avail_href.strip("/").split("/")[-1]

                # Check if already completed
                prog_result = await call_complete_api(page, jwt, {
                    "schoolId": SCHOOL,
                    "courseId": COURSE,
                    "chapterId": CHAPTER,
                    "postId": post_id,
                    **(captured[0] if captured else {}),
                })
                log(f"\nAPI-Call für {post_id}: {prog_result}")

        await browser.close()

    log(f"\nFertig. Captured payloads: {captured}")


if __name__ == "__main__":
    asyncio.run(main())
