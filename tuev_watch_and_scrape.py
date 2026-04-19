#!/usr/bin/env python3
"""
TÜV-Kurs: Watch all available lessons (seek video to end → triggers completion),
then rescan for newly unlocked content. Repeats until all 6 chapters are done.
Scrapt parallel alles was gefunden wird.
"""
import asyncio, json, re, html as html_module
import aiohttp, aiofiles
from pathlib import Path
from playwright.async_api import async_playwright

BASE_URL    = "https://verkaufspsychologie-onlinekurs.mymemberspot.de"
COURSE_URL  = f"{BASE_URL}/library/P9KAAaH3H5J6gwPXiKwN"
SCHOOL      = "UREn3qhVDnOLNJ7BmKum"
LOGIN_EMAIL = "sw@mitarbeiter.com"
LOGIN_PW    = "Mwiz123!"
OUTPUT_DIR  = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/Kurs_TUV")
LOG_FILE    = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/scrape_tuev.log")

CDN_PATTERN = re.compile(r'https://mspot-vod[^"\'>\s]+\.(mp4|m4[as]|m3u8|ts)', re.I)
VTT_PATTERN = re.compile(r'https://[^\s"\'<>]+\.vtt[^\s"\'<>]*', re.I)


def sanitize(name):
    name = html_module.unescape(name)
    return re.sub(r'[<>:"/\\|?*\n\r\t]', "", name).strip()[:80]


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
    log(f"Eingeloggt: {page.url}")


async def scroll_fully(page):
    prev = 0
    for _ in range(15):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)
        h = await page.evaluate("document.body.scrollHeight")
        if h == prev: break
        prev = h


async def watch_and_complete(page, url: str) -> bool:
    """Navigate to lesson, seek video to end, wait for completion API. Returns True if completed."""
    completed_event = asyncio.Event()

    async def on_response(resp):
        if "update-post-progress" in resp.url and resp.status == 200:
            completed_event.set()

    page.on("response", on_response)
    success = False

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Wait up to 12s for video to have duration
        video_dur = None
        for _ in range(24):
            await page.wait_for_timeout(500)
            video_dur = await page.evaluate("""
                () => { const v = document.querySelector('video'); return (v && v.duration > 0) ? v.duration : null; }
            """)
            if video_dur: break

        if video_dur:
            log(f"    Video: {video_dur:.1f}s — spule auf 97%")
            await page.evaluate("""
                (dur) => {
                    const v = document.querySelector('video');
                    v.muted = true;
                    v.currentTime = dur * 0.97;
                    v.play();
                }
            """, video_dur)
            # Wait for completion event (up to 8s)
            try:
                await asyncio.wait_for(completed_event.wait(), timeout=8.0)
                log(f"    ✓ Completion API ausgelöst")
                success = True
            except asyncio.TimeoutError:
                log(f"    ⚠ Kein Completion-Event in 8s")
        else:
            # Text/page lesson — just wait a moment, scroll to bottom
            log(f"    Keine Video-Lektion — scroll + wait")
            await scroll_fully(page)
            await page.wait_for_timeout(3000)
            try:
                await asyncio.wait_for(completed_event.wait(), timeout=3.0)
                log(f"    ✓ Text-Lektion abgeschlossen")
                success = True
            except asyncio.TimeoutError:
                pass  # OK for text lessons

    except Exception as e:
        log(f"    [ERR] {e}")
    finally:
        page.remove_listener("response", on_response)

    return success


async def scrape_lesson(page, url: str, lesson_dir: Path, cookies: list):
    lesson_dir.mkdir(parents=True, exist_ok=True)
    if (lesson_dir / "_meta.json").exists():
        return

    cdn_urls, vtt_found = [], []

    def on_req(req):
        u = req.url
        if CDN_PATTERN.search(u): cdn_urls.append(u)
        if VTT_PATTERN.search(u) and ("mspot-vod" in u or "b-cdn.net" in u):
            vtt_found.append(u)

    page.on("request", on_req)
    page_title = ""
    download_urls, external_links = [], []

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(24):
            await page.wait_for_timeout(500)
            if cdn_urls: break

        content = await page.content()
        for m in CDN_PATTERN.finditer(content): cdn_urls.append(m.group(0))
        for m in VTT_PATTERN.finditer(content):
            u = m.group(0)
            if "mspot-vod" in u or "b-cdn.net" in u: vtt_found.append(u)

        for sel in ["h1", ".lesson-title"]:
            el = await page.query_selector(sel)
            if el:
                t = sanitize((await el.inner_text()).strip())
                if t: page_title = t; break

        for el in await page.query_selector_all("a[href]"):
            href = await el.get_attribute("href")
            if not href: continue
            if any(ext in href.lower() for ext in [".pdf", ".docx", ".xlsx", ".pptx", ".zip"]):
                download_urls.append(href if href.startswith("http") else BASE_URL + href)
            elif href.startswith("http") and BASE_URL not in href:
                text = (await el.inner_text()).strip()
                if text and href not in [l["url"] for l in external_links]:
                    external_links.append({"url": href, "text": sanitize(text)})

        async with aiofiles.open(lesson_dir / "_page.html", "w", encoding="utf-8") as f:
            await f.write(content)

        # Also seek video to trigger completion while scraping
        video_dur = await page.evaluate("""
            () => { const v = document.querySelector('video'); return (v && v.duration > 0) ? v.duration : null; }
        """)
        if video_dur:
            await page.evaluate("""
                (dur) => {
                    const v = document.querySelector('video');
                    v.muted = true;
                    v.currentTime = dur * 0.97;
                    v.play();
                }
            """, video_dur)
            await page.wait_for_timeout(5000)  # wait for completion event

    except Exception as e:
        log(f"    [ERR] {e}")
    finally:
        page.remove_listener("request", on_req)

    def dedup(lst):
        seen = set(); out = []
        for u in lst:
            if u not in seen: seen.add(u); out.append(u)
        return out

    cdn_urls = dedup(cdn_urls)
    vtt_found = dedup(vtt_found)
    video_urls = [u for u in cdn_urls if 'video' in u.lower() or '.mp4' in u.lower()]
    audio_urls = [u for u in cdn_urls if 'audio' in u.lower()]
    hls_urls   = [u for u in cdn_urls if '.m3u8' in u.lower()]

    if not video_urls and vtt_found:
        base = vtt_found[0].split('/caption.vtt')[0]
        if base.startswith('https://mspot-vod'):
            video_urls = [f"{base}/media-video-avc1-3.mp4"]
            audio_urls = [f"{base}/media-audio-de-mp4a.mp4"]
            cdn_urls = video_urls + audio_urls

    meta = {
        "url": url,
        "page_title": page_title,
        "video_urls": cdn_urls,
        "vtt_urls": vtt_found,
        "download_urls": list(dict.fromkeys(download_urls)),
        "external_links": external_links,
        "video_url_clean": video_urls[0] if video_urls else None,
        "audio_url_clean": audio_urls[0] if audio_urls else None,
        "hls_url_clean":   hls_urls[0]   if hls_urls else None,
    }
    async with aiofiles.open(lesson_dir / "_meta.json", "w", encoding="utf-8") as f:
        await f.write(json.dumps(meta, indent=2, ensure_ascii=False))

    log(f"    CDN:{len(cdn_urls)} VTT:{len(vtt_found)} DL:{len(download_urls)} Ext:{len(external_links)}")

    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for i, u in enumerate(vtt_found[:3]):
            if not (lesson_dir / f"transkript_{i+1}.vtt").exists():
                try:
                    async with session.get(u) as resp:
                        if resp.status == 200:
                            async with aiofiles.open(lesson_dir / f"transkript_{i+1}.vtt", "wb") as f:
                                await f.write(await resp.read())
                            log(f"    [dl] transkript_{i+1}.vtt")
                except: pass


async def get_course_snapshot(page) -> dict:
    """Load course page (with lazy-load scrolling), return chapters & lessons."""
    await page.goto(COURSE_URL, wait_until="domcontentloaded", timeout=60000)
    prev = 0
    for _ in range(15):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(600)
        h = await page.evaluate("document.body.scrollHeight")
        if h == prev: break
        prev = h

    content = await page.content()
    # Chapter links (3-segment = course/chapter)
    ch_hrefs = re.findall(
        r'data-cy="mediaLectureAvailiableLinkItem"[^>]*href="(/library/[^"]+)"', content
    )
    ch_hrefs = [h for h in ch_hrefs if len(h.strip("/").split("/")) == 3]
    ch_titles = re.findall(r'mediaLectureCardTitle[^>]*>([^<]+)', content)
    locked_ch = content.count('mediaLectureUnavailableDivItem')
    return {"chapter_hrefs": ch_hrefs, "chapter_titles": ch_titles, "locked": locked_ch}


async def get_chapter_snapshot(page, ch_url: str) -> dict:
    """Load chapter page, return available lesson hrefs + all titles."""
    await page.goto(ch_url, wait_until="domcontentloaded", timeout=45000)
    prev = 0
    for _ in range(10):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)
        h = await page.evaluate("document.body.scrollHeight")
        if h == prev: break
        prev = h

    content = await page.content()
    lesson_hrefs = re.findall(
        r'data-cy="mediaLectureAvailiableLinkItem"[^>]*href="(/library/[^"]+)"', content
    )
    lesson_hrefs = [h for h in lesson_hrefs if len(h.strip("/").split("/")) == 4]
    titles = re.findall(r'mediaLectureCardTitle[^>]*>([^<]+)', content)
    locked = content.count('mediaLectureUnavailableDivItem')
    return {"lesson_hrefs": lesson_hrefs, "titles": titles, "locked": locked}


async def main():
    LOG_FILE.write_text("")
    course_dir = OUTPUT_DIR / "Vorbereitung auf die TÜV-Zertifizierung"
    course_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=20)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await login(page)
        cookies = await context.cookies()

        scraped = set()

        for outer in range(40):
            snap = await get_course_snapshot(page)
            log(f"\n{'═'*60}")
            log(f"Runde {outer+1}: {len(snap['chapter_hrefs'])} Kapitel verfügbar, {snap['locked']} gesperrt")

            new_work = False

            for ci, ch_href in enumerate(snap["chapter_hrefs"]):
                ch_title = snap["chapter_titles"][ci].strip() if ci < len(snap["chapter_titles"]) else f"kapitel_{ci+1}"
                ch_dir   = course_dir / sanitize(ch_title)
                ch_dir.mkdir(parents=True, exist_ok=True)
                ch_url   = BASE_URL + ch_href

                log(f"\n  Kapitel [{ci+1}]: {ch_title}")
                ch_snap = await get_chapter_snapshot(page, ch_url)
                log(f"    {len(ch_snap['lesson_hrefs'])} verfügbar, {ch_snap['locked']} gesperrt")

                for li, l_href in enumerate(ch_snap["lesson_hrefs"]):
                    if l_href in scraped:
                        continue

                    l_title = ch_snap["titles"][li].strip() if li < len(ch_snap["titles"]) else f"lektion_{li+1}"
                    l_dir   = ch_dir / f"{li+1:02d} {sanitize(l_title)}"
                    l_url   = BASE_URL + l_href

                    log(f"\n    [{li+1}] {l_title}")
                    await scrape_lesson(page, l_url, l_dir, cookies)
                    scraped.add(l_href)
                    cookies = await context.cookies()
                    new_work = True

                    # After scraping, also check if new items unlocked in this chapter
                    await page.wait_for_timeout(1000)

            if not new_work:
                log(f"\nKeine neuen Lektionen in Runde {outer+1}.")
                if snap["locked"] == 0:
                    log("Alle Kapitel abgeschlossen!")
                    break
                else:
                    log(f"Noch {snap['locked']} gesperrte Kapitel — Kurs ggf. manuell fortführen.")
                    break

        log(f"\n{'='*60}")
        log(f"FERTIG: {len(scraped)} Lektionen gescrapt.")
        log(f"Gespeichert: {OUTPUT_DIR}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
