#!/usr/bin/env python3
"""
scrape_v2.py — Niggehoff Memberspot Scraper (v2)

Vollständige Kursstruktur gemäß modules.yaml whitelist.
Hierarchie: Modul → Kapitel → Lektion

Pro Lektion erfasst:
  - lesson_title, description_text, lesson_url
  - video_url (nur URL, kein Download)
  - audio_url → Download als .m4a
  - vtt_url   → Download mit Timecodes; fehlt VTT → Whisper-Fallback
  - pdf_urls  → Download

Run:   python3 scrape_v2.py
Resume: gleicher Befehl — bereits gescrapte Lektionen werden übersprungen.
"""

import asyncio
import json
import re
import time
import random
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import aiofiles
import yaml
from playwright.async_api import async_playwright, Page, Browser

# ── Pfade ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
CONFIG_FILE  = SCRIPT_DIR / "modules.yaml"
STATE_FILE   = None   # wird nach Config-Load gesetzt

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Konvertiert beliebigen Text in einen URL-freundlichen Slug."""
    text = str(text).lower()
    for a, b in [("ä","ae"),("ö","oe"),("ü","ue"),("ß","ss"),
                 ("Ä","ae"),("Ö","oe"),("Ü","ue")]:
        text = text.replace(a, b)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:60]


def sanitize_filename(name: str) -> str:
    """Bereinigt Dateinamen von ungültigen Zeichen."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    return name.strip()[:80]


def format_vtt_time(seconds: float) -> str:
    """Sekunden → VTT-Timestamp HH:MM:SS.mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fuzzy_match(title: str, hint: Optional[str]) -> bool:
    """Prüft ob hint (Regex) im Kapitel-Titel vorkommt."""
    if not hint:
        return False
    return bool(re.search(hint, title, re.IGNORECASE))


# ── Konfiguration ──────────────────────────────────────────────────────────────

@dataclass
class ChapterExpectation:
    hint: Optional[str]
    expected: Optional[int]


@dataclass
class ModuleConfig:
    name: str
    course_id: str
    slug: str
    expected_total: Optional[int]
    expected_chapters: list[ChapterExpectation]
    skip_chapter_patterns: list[str]


@dataclass
class Config:
    base_url: str
    output_dir: Path
    credentials: dict
    rate_limit: dict
    blacklist_course_ids: set[str]
    blacklist_chapter_patterns: list[str]
    modules: list[ModuleConfig]


def load_config(path: Path) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    modules = []
    for m in raw.get("modules", []):
        ch_list = []
        for ch in m.get("expected_chapters", []):
            ch_list.append(ChapterExpectation(
                hint=ch.get("hint"),
                expected=ch.get("expected"),
            ))
        modules.append(ModuleConfig(
            name=m["name"],
            course_id=m["course_id"],
            slug=m["slug"],
            expected_total=m.get("expected_total"),
            expected_chapters=ch_list,
            skip_chapter_patterns=m.get("skip_chapter_patterns", []),
        ))

    return Config(
        base_url=raw["base_url"],
        output_dir=SCRIPT_DIR / raw["output_dir"],
        credentials=raw["credentials"],
        rate_limit=raw["rate_limit"],
        blacklist_course_ids=set(raw["blacklist"]["course_ids"]),
        blacklist_chapter_patterns=raw["blacklist"].get("chapter_title_patterns", []),
        modules=modules,
    )


# ── State Management (Resume-Fähigkeit) ────────────────────────────────────────

def load_state(state_file: Path) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict, state_file: Path):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_scraped(state: dict, lesson_url: str) -> bool:
    return state.get(lesson_url, {}).get("status") == "ok"


def mark_scraped(state: dict, lesson_url: str, data: dict):
    state[lesson_url] = {"status": "ok", "scraped_at": datetime.now(timezone.utc).isoformat(), **data}


def mark_error(state: dict, lesson_url: str, error: str):
    state[lesson_url] = {"status": "error", "error": error, "scraped_at": datetime.now(timezone.utc).isoformat()}


# ── Rate Limiter ───────────────────────────────────────────────────────────────

class RateLimiter:
    def __init__(self, min_s: float, max_s: float):
        self.min_s = min_s
        self.max_s = max_s
        self._last = 0.0

    async def wait(self):
        since = time.monotonic() - self._last
        delay = random.uniform(self.min_s, self.max_s)
        remaining = delay - since
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last = time.monotonic()


# ── Netzwerk-Interceptor ───────────────────────────────────────────────────────

class MediaCollector:
    """Horcht auf Network-Requests und sammelt Media-URLs."""

    def __init__(self, page: Page):
        self.page = page
        self.video_urls: set[str] = set()
        self.audio_urls: set[str] = set()
        self.vtt_urls:   set[str] = set()
        self.hls_urls:   set[str] = set()
        self._handler = self._on_request

    def _on_request(self, req):
        url = req.url
        if "mspot-vod" not in url and "b-cdn.net" not in url:
            return
        if ".vtt" in url:
            self.vtt_urls.add(url)
            return
        if "hls.m3u8" in url or re.search(r'/hls[^/]*\.m3u8', url):
            self.hls_urls.add(url)
            return
        if ".m3u8" in url:
            return   # Sub-Manifeste ignorieren
        if ".mp4" in url or ".m4a" in url or ".m4v" in url:
            low = url.lower()
            if "audio" in low or "mp4a" in low:
                self.audio_urls.add(url)
            elif "video" in low or "avc1" in low:
                self.video_urls.add(url)

    def attach(self):
        self.page.on("request", self._handler)

    def detach(self):
        try:
            self.page.remove_listener("request", self._handler)
        except Exception:
            pass

    def best_video_url(self) -> Optional[str]:
        """Wählt höchste Qualität aus den gesammelten Video-URLs."""
        if not self.video_urls:
            return None
        # Bevorzuge avc1-4 > avc1-3 > avc1-2 > avc1-1
        for quality in ["avc1-4", "avc1-3", "avc1-2", "avc1-1"]:
            for url in self.video_urls:
                if quality in url:
                    return url
        return next(iter(self.video_urls))

    def best_audio_url(self) -> Optional[str]:
        if not self.audio_urls:
            return None
        # Bevorzuge mp4a-Streams
        for url in self.audio_urls:
            if "mp4a" in url:
                return url
        return next(iter(self.audio_urls))

    def best_vtt_url(self) -> Optional[str]:
        return next(iter(self.vtt_urls)) if self.vtt_urls else None

    def best_hls_url(self) -> Optional[str]:
        return next(iter(self.hls_urls)) if self.hls_urls else None


# ── DRM-Prüfung ───────────────────────────────────────────────────────────────

async def is_drm_protected(hls_url: str, session: aiohttp.ClientSession) -> bool:
    """Liest die ersten 4KB des HLS-Manifests und prüft auf SAMPLE-AES."""
    try:
        async with session.get(hls_url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            chunk = await resp.content.read(4096)
            text = chunk.decode("utf-8", errors="ignore")
            return "SAMPLE-AES" in text or "com.apple.streamingkeydelivery" in text
    except Exception:
        return False


# ── Download ───────────────────────────────────────────────────────────────────

async def download_file(
    url: str,
    dest: Path,
    session: aiohttp.ClientSession,
    retry_max: int = 3,
    backoff_base: float = 2.0,
) -> bool:
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retry_max):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status == 200:
                    async with aiofiles.open(dest, "wb") as f:
                        await f.write(await resp.read())
                    return True
                if resp.status in (429, 500, 502, 503, 504):
                    wait = backoff_base ** attempt + random.uniform(0, 1)
                    print(f"    [HTTP {resp.status}] Retry {attempt+1}/{retry_max} in {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    print(f"    [HTTP {resp.status}] {url[:70]}")
                    return False
        except asyncio.TimeoutError:
            print(f"    [timeout] {url[:70]}")
        except Exception as e:
            print(f"    [err] {e}")
        await asyncio.sleep(backoff_base ** attempt)
    return False


# ── DOM-Hilfen ─────────────────────────────────────────────────────────────────

async def wait_for_page_ready(page: Page):
    """Wartet bis Haupt-Content geladen ist — kein fixer Sleep."""
    try:
        await page.wait_for_selector(
            "video, [class*='player'], [class*='lecture'], [class*='lesson'], "
            "[class*='content'], appc-media-lecture-available, [class*='description']",
            timeout=20000, state="attached"
        )
    except Exception:
        pass
    # Kurze Pause damit Netzwerk-Requests feuern können
    await page.wait_for_load_state("networkidle", timeout=10000)


async def expand_all_chapters(page: Page) -> int:
    """Expandiert alle zusammengeklappten Kapitel-Accordions."""
    expanded = 0
    # Iteriere mehrfach, da manche Accordions erst nach vorherigen sichtbar werden
    for _round in range(4):
        newly_expanded = 0
        buttons = await page.query_selector_all(
            "button[aria-expanded='false'], "
            "[role='button'][aria-expanded='false'], "
            "[class*='chapter'][class*='collapsed'] button, "
            "[class*='accordion']:not([class*='open']) [class*='header'], "
            "[class*='module-item']:not([class*='active']) [class*='toggle']"
        )
        for btn in buttons:
            try:
                visible = await btn.is_visible()
                if not visible:
                    continue
                await btn.scroll_into_view_if_needed()
                await btn.click()
                # Warte auf tatsächliche DOM-Änderung statt fixem Sleep
                try:
                    await page.wait_for_function(
                        "(el) => el.getAttribute('aria-expanded') !== 'false'",
                        arg=btn, timeout=2000
                    )
                except Exception:
                    await asyncio.sleep(0.3)
                newly_expanded += 1
            except Exception:
                pass
        expanded += newly_expanded
        if newly_expanded == 0:
            break
        # Kurz warten damit neue Elemente rendern
        await page.wait_for_timeout(500)

    # Einmal durch die Seite scrollen (lazy loading)
    for _ in range(8):
        await page.mouse.wheel(0, 700)
        await page.wait_for_timeout(300)

    return expanded


async def extract_chapter_and_lesson_links(page: Page) -> list[dict]:
    """
    Extrahiert alle Kapitel→Lektion-Zuordnungen aus der Sidebar.
    Gibt Liste von {chapter_title, chapter_id, lesson_url, lesson_id, lesson_title} zurück.
    """
    return await page.evaluate("""
    () => {
        const BASE = "https://online-verkaufspsychologie.mymemberspot.de";
        const results = [];

        // Memberspot-Sidebar: Kapitel sind oft <li> oder <div> mit class "chapter"/"section"
        // Lektionen sind <a> mit 4-Segment /library/... Pfad
        const allLinks = document.querySelectorAll('a[href]');
        const seen = new Set();

        // Baue eine Eltern-Kapitel-Map auf
        function findChapterTitle(el) {
            let parent = el.parentElement;
            let depth = 0;
            while (parent && depth < 12) {
                const cls = (parent.className || '') + (parent.id || '');
                const tag = parent.tagName;
                if (['H2','H3','H4'].includes(tag) || /chapter|section|module|group|item/i.test(cls)) {
                    // Suche Heading-Text
                    const heading = parent.querySelector('h2,h3,h4,[class*="title"],[class*="name"]');
                    if (heading) {
                        const t = heading.innerText?.trim();
                        if (t && t.length > 1 && t.length < 150) return t;
                    }
                    const t = parent.innerText?.split('\\n')[0]?.trim();
                    if (t && t.length > 1 && t.length < 150) return t;
                }
                parent = parent.parentElement;
                depth++;
            }
            return null;
        }

        for (const a of allLinks) {
            const href = a.getAttribute('href')?.split('#')[0];
            if (!href) continue;
            const segs = href.replace(/\\/+$/, '').split('/').filter(Boolean);
            // 4-Segment: library/courseId/chapterId/lessonId
            if (segs.length !== 4 || segs[0] !== 'library') continue;
            if (seen.has(href)) continue;
            seen.add(href);

            const lessonTitle = (a.innerText || a.getAttribute('aria-label') || '').trim()
                .replace(/\\s+/g, ' ').slice(0, 120);
            const chapterTitle = findChapterTitle(a) || segs[2];

            results.push({
                chapter_id:    segs[2],
                chapter_title: chapterTitle,
                lesson_id:     segs[3],
                lesson_url:    BASE + href,
                lesson_title:  lessonTitle,
            });
        }
        return results;
    }
    """)


async def navigate_to_chapter_and_collect(page: Page, chapter_url: str) -> list[dict]:
    """Fallback: Navigiere direkt zu Kapitel-URL und sammle Lektions-Links."""
    await page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await asyncio.sleep(1.5)
    await expand_all_chapters(page)
    return await extract_chapter_and_lesson_links(page)


async def extract_description(page: Page) -> str:
    """Extrahiert den Beschreibungstext einer Lektion (mehrere Selector-Fallbacks)."""
    selectors = [
        # Memberspot Angular-Komponenten (Quill-Editor View)
        "appc-course-description-quill-view .ql-editor",
        "appc-course-description-quill-view",
        "[class*='quill-view'] .ql-editor",
        ".ql-editor",
        # Generische Beschreibungs-Container
        "[class*='lecture-description']",
        "[class*='lesson-description']",
        "[class*='description-body']",
        "[class*='content-description']",
        "[id*='description']",
        # Memberspot-spezifische IDs die in HTMLs auftauchten
        "[id='appcCourseDescriptionQuillView']",
        "appc-media-lecture-available [class*='description']",
        "[class*='lecture-body']",
        "[class*='overview-text']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 15:
                    return text
        except Exception:
            pass
    return ""


async def extract_lesson_title(page: Page, fallback: str = "") -> str:
    """Extrahiert den Lektions-Titel von der Seite."""
    selectors = [
        "h1",
        "[class*='lecture-title']",
        "[class*='lesson-title']",
        "[class*='content-title']",
        "[class*='media-title']",
        "title",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 1 and len(text) < 200:
                    # Memberspot hat oft "Titel | Kursname" in <title>
                    text = text.split("|")[0].split("–")[0].strip()
                    if text:
                        return text
        except Exception:
            pass
    return fallback


async def extract_pdf_links(page: Page) -> list[str]:
    """Extrahiert alle Download-PDF-Links von der Lektionsseite."""
    return await page.evaluate("""
    () => {
        const urls = new Set();
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href;
            if (!href) continue;
            const lower = href.toLowerCase();
            if (lower.includes('.pdf') || lower.includes('/download') ||
                lower.includes('.docx') || lower.includes('.pptx') ||
                lower.includes('.xlsx') || lower.includes('.zip')) {
                urls.add(href);
            }
        }
        return [...urls];
    }
    """)


# ── Haupt-Scraper ──────────────────────────────────────────────────────────────

class MemberspotScraper:

    def __init__(self, config: Config, state: dict):
        self.cfg   = config
        self.state = state
        self.rl    = RateLimiter(config.rate_limit["min_seconds"],
                                  config.rate_limit["max_seconds"])

    # ── Login ──────────────────────────────────────────────────────────────────

    async def login(self, page: Page):
        await page.goto(f"{self.cfg.base_url}/library",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        if not any(k in page.url for k in ["login", "auth", "sign-in"]):
            print("  → Bereits eingeloggt.")
            return

        print("  → Login-Seite erkannt, logge ein…")
        try:
            await page.wait_for_selector(
                "input[type='email'], input[name='email'], input.min-w-input",
                timeout=10000
            )
            for sel in ["input[type='email']", "input[name='email']", "input.min-w-input"]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(self.cfg.credentials["email"])
                    break
            for sel in ["input[type='password']", "input[name='password']",
                        "input.cypress__login__password"]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(self.cfg.credentials["password"])
                    break
            for sel in ["button[type='submit']", "button:has-text('Login')",
                        "button:has-text('Anmelden')"]:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    break
            await page.wait_for_url(lambda url: "library" in url or "dashboard" in url,
                                     timeout=15000)
            print("  → Login erfolgreich.")
        except Exception as e:
            print(f"  ⚠ Auto-Login fehlgeschlagen: {e}")
            print("  → Bitte manuell einloggen und Enter drücken…")
            input()

    # ── Chapter-Skip-Prüfung ───────────────────────────────────────────────────

    def _should_skip_chapter(self, title: str, module: ModuleConfig) -> bool:
        all_patterns = (
            self.cfg.blacklist_chapter_patterns
            + module.skip_chapter_patterns
        )
        for pat in all_patterns:
            if re.search(pat, title, re.IGNORECASE):
                return True
        return False

    # ── Kurs-Struktur ermitteln ────────────────────────────────────────────────

    async def discover_structure(
        self, page: Page, module: ModuleConfig
    ) -> list[dict]:
        """
        Gibt {chapter_id, chapter_title, lesson_id, lesson_url, lesson_title}
        für jede Lektion zurück (dedupliziert, sortiert).
        """
        course_url = f"{self.cfg.base_url}/library/{module.course_id}"
        print(f"  → Navigiere zu Kurs-Seite…")
        await page.goto(course_url, wait_until="domcontentloaded", timeout=90000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Accordions aufklappen
        n_expanded = await expand_all_chapters(page)
        print(f"  → {n_expanded} Accordions geöffnet")

        lessons = await extract_chapter_and_lesson_links(page)

        if len(lessons) < 3:
            # Fallback: 3-Segment Kapitel-Links direkt navigieren
            print("  → Wenige Links auf Kurs-Seite — navigiere zu Kapitel-Seiten…")
            chapter_links = await page.evaluate("""
            () => {
                const seen = new Set();
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const href = a.getAttribute('href')?.split('#')[0] || '';
                    const segs = href.replace(/\\/+$/, '').split('/').filter(Boolean);
                    if (segs.length === 3 && segs[0] === 'library') {
                        if (!seen.has(href)) {
                            seen.add(href);
                            out.push({
                                href: href,
                                title: (a.innerText || '').trim().split('\\n')[0].slice(0,120)
                            });
                        }
                    }
                }
                return out;
            }
            """)
            base = self.cfg.base_url
            for ch in chapter_links:
                ch_url = base + ch["href"]
                ch_title = ch["title"]
                if self._should_skip_chapter(ch_title, module):
                    print(f"    [SKIP Kapitel] {ch_title}")
                    continue
                ch_lessons = await navigate_to_chapter_and_collect(page, ch_url)
                # Füge Kapitel-Infos aus der URL ein falls nicht erkannt
                for l in ch_lessons:
                    if not l.get("chapter_title") or l["chapter_title"] == l["chapter_id"]:
                        l["chapter_title"] = ch_title or l["chapter_id"]
                lessons.extend(ch_lessons)

        # Deduplizierung per Lektion-URL
        seen_urls = set()
        unique = []
        for l in lessons:
            url = l.get("lesson_url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(l)

        # Blacklist-Kapitel filtern
        filtered = []
        skipped_chapters = set()
        for l in unique:
            ch = l.get("chapter_title", "")
            if self._should_skip_chapter(ch, module):
                skipped_chapters.add(ch)
            else:
                filtered.append(l)

        if skipped_chapters:
            print(f"  → Übersprungene Kapitel: {skipped_chapters}")

        # Sortierung: nach chapter_id, dann lesson_id
        filtered.sort(key=lambda l: (l.get("chapter_id", ""), l.get("lesson_id", "")))

        print(f"  → {len(filtered)} Lektionen gefunden")
        return filtered

    # ── Eine Lektion scrapen ───────────────────────────────────────────────────

    async def scrape_lesson(
        self,
        page: Page,
        lesson: dict,
        module: ModuleConfig,
        cookies: list,
    ) -> dict:
        """
        Navigiert zur Lektionsseite, erfasst alle Daten und lädt Dateien herunter.
        Gibt Lektion-Dict zurück.
        """
        lesson_url = lesson["lesson_url"]
        chapter_title = lesson.get("chapter_title", "unknown")
        lesson_id = lesson.get("lesson_id", "")

        # Pfade berechnen
        mod_slug = module.slug
        ch_slug  = slugify(chapter_title)
        le_slug  = slugify(lesson.get("lesson_title", "") or lesson_id)
        if not le_slug:
            le_slug = lesson_id[:30]

        audio_dest = self.cfg.output_dir / "audio" / mod_slug / ch_slug / f"{le_slug}.m4a"
        vtt_dest   = self.cfg.output_dir / "transcripts" / mod_slug / ch_slug / f"{le_slug}.vtt"
        pdf_dir    = self.cfg.output_dir / "pdfs" / mod_slug / ch_slug / le_slug

        collector = MediaCollector(page)
        collector.attach()

        try:
            await page.goto(lesson_url, wait_until="domcontentloaded", timeout=60000)
            await wait_for_page_ready(page)
            # Zusätzliche Wartezeit damit Netzwerk-Interceptor feuert
            await asyncio.sleep(3)
            await page.mouse.wheel(0, 400)
            await asyncio.sleep(0.5)

        except Exception as e:
            collector.detach()
            return {"error": str(e), "lesson_url": lesson_url}

        finally:
            collector.detach()

        # Seiten-Content extrahieren
        title        = await extract_lesson_title(page, fallback=lesson.get("lesson_title", le_slug))
        description  = await extract_description(page)
        pdf_urls     = await extract_pdf_links(page)

        # Media-URLs
        video_url = collector.best_video_url()
        audio_url = collector.best_audio_url()
        vtt_url   = collector.best_vtt_url()
        hls_url   = collector.best_hls_url()

        # HLS-Fallback falls kein direktes MP4
        drm_protected = False
        if not video_url and hls_url:
            # DRM-Prüfung
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
            async with aiohttp.ClientSession(headers=headers) as sess:
                drm_protected = await is_drm_protected(hls_url, sess)
            if drm_protected:
                print(f"    ⚑ DRM erkannt — kein Download möglich")
                hls_url = None
            else:
                video_url = hls_url  # HLS als Video-URL nutzen
                hls_url = None

        if drm_protected:
            audio_url = None
            vtt_url = None

        # Downloads
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        headers = {"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
        rl = self.cfg.rate_limit

        async with aiohttp.ClientSession(headers=headers) as sess:
            if audio_url:
                ok = await download_file(audio_url, audio_dest, sess,
                                         retry_max=rl["retry_max"],
                                         backoff_base=rl["retry_backoff_base"])
                if not ok:
                    audio_url = None

            if vtt_url:
                ok = await download_file(vtt_url, vtt_dest, sess,
                                         retry_max=rl["retry_max"],
                                         backoff_base=rl["retry_backoff_base"])
                if not ok:
                    vtt_url = None

            # Whisper-Fallback wenn kein VTT aber Audio vorhanden
            if not vtt_url and audio_dest.exists():
                print(f"    → Kein VTT — starte Whisper-Transkription…")
                try:
                    from whisper_fallback import transcribe_to_vtt
                    ok = transcribe_to_vtt(str(audio_dest), str(vtt_dest))
                    if ok:
                        vtt_url = "[whisper]"
                        print(f"    → Whisper fertig")
                    else:
                        print(f"    ⚠ Whisper fehlgeschlagen")
                except ImportError:
                    print(f"    ⚠ whisper_fallback.py nicht gefunden — VTT übersprungen")

            # PDFs herunterladen
            downloaded_pdfs = []
            for pdf_url in pdf_urls:
                fname = sanitize_filename(
                    urllib.parse.unquote(pdf_url.split("/")[-1].split("?")[0]) or "material"
                )
                if not fname:
                    continue
                pdf_dest = pdf_dir / fname
                ok = await download_file(pdf_url, pdf_dest, sess,
                                          retry_max=rl["retry_max"],
                                          backoff_base=rl["retry_backoff_base"])
                if ok:
                    downloaded_pdfs.append(str(pdf_dest.relative_to(self.cfg.output_dir.parent)))

        result = {
            "lesson_id":       lesson_id,
            "lesson_title":    title,
            "lesson_url":      lesson_url,
            "chapter_id":      lesson.get("chapter_id", ""),
            "chapter_title":   chapter_title,
            "description_text": description,
            "video_url":       video_url,
            "audio_path":      str(audio_dest.relative_to(self.cfg.output_dir.parent)) if audio_dest.exists() else None,
            "vtt_path":        str(vtt_dest.relative_to(self.cfg.output_dir.parent)) if vtt_dest.exists() else None,
            "pdf_paths":       downloaded_pdfs,
            "thumbnail_url":   None,   # wird weiter unten gesetzt
            "drm_protected":   drm_protected,
            "transcript_source": ("vtt" if (vtt_url and vtt_url != "[whisper]") else
                                   ("whisper" if vtt_url == "[whisper]" else None)),
        }

        # Thumbnail
        thumb = await page.evaluate("""
        () => {
            const v = document.querySelector('video[poster]');
            if (v) return v.getAttribute('poster');
            const og = document.querySelector('meta[property="og:image"]');
            if (og) return og.getAttribute('content');
            return null;
        }
        """)
        result["thumbnail_url"] = thumb

        return result

    # ── Validierung ────────────────────────────────────────────────────────────

    def validate(self, module: ModuleConfig, chapters: dict[str, list]) -> dict:
        """
        Vergleicht Ist vs. Soll-Zahlen.
        chapters: {chapter_title: [lesson_dict, ...]}
        """
        total_actual = sum(len(v) for v in chapters.values())
        total_expected = module.expected_total

        chap_validation = []
        unmatched_actuals = list(chapters.items())

        for exp in module.expected_chapters:
            if exp.hint is None:
                # Letztes/unbenanntes Kapitel
                match_title = "?"
                actual = None
                for ct, ls in unmatched_actuals:
                    if not any(fuzzy_match(ct, e.hint) for e in module.expected_chapters
                               if e.hint and e is not exp):
                        actual = len(ls)
                        match_title = ct
                        break
            else:
                match_title = "?"
                actual = None
                for ct, ls in unmatched_actuals:
                    if fuzzy_match(ct, exp.hint):
                        actual = len(ls)
                        match_title = ct
                        break

            ok = (actual == exp.expected) if (actual is not None and exp.expected is not None) else None
            chap_validation.append({
                "hint":           exp.hint or "[unbenannt]",
                "matched_title":  match_title,
                "expected":       exp.expected,
                "actual":         actual,
                "ok":             ok,
            })

        return {
            "total_expected": total_expected,
            "total_actual":   total_actual,
            "total_ok":       (total_actual == total_expected) if total_expected else None,
            "chapters":       chap_validation,
        }

    # ── Modul scrapen ──────────────────────────────────────────────────────────

    async def scrape_module(self, page: Page, module: ModuleConfig, cookies: list):
        print(f"\n{'='*64}")
        print(f"  MODUL: {module.name}")
        print(f"  Slug : {module.slug}")
        print(f"  URL  : {self.cfg.base_url}/library/{module.course_id}")
        print(f"{'='*64}")

        # Kurs-Struktur ermitteln
        all_lessons = await self.discover_structure(page, module)

        if not all_lessons:
            print("  ✗ Keine Lektionen gefunden — bitte manuell prüfen!")
            return {}

        # Lektionen pro Kapitel gruppieren (für JSON + Validierung)
        chapters_map: dict[str, list] = {}
        for l in all_lessons:
            ct = l.get("chapter_title", "")
            chapters_map.setdefault(ct, [])

        # Scrape-Schleife
        total = len(all_lessons)
        scraped = 0
        skipped = 0
        errors  = 0

        for idx, lesson in enumerate(all_lessons):
            lesson_url = lesson["lesson_url"]
            ch_title   = lesson.get("chapter_title", "")
            le_title   = lesson.get("lesson_title", lesson.get("lesson_id", ""))

            prefix = f"  [{idx+1:3d}/{total}]"

            if is_scraped(self.state, lesson_url):
                print(f"{prefix} [skip] {le_title[:55]}")
                # Aus State wiederherstellen
                chapters_map.setdefault(ch_title, []).append(
                    self.state[lesson_url]
                )
                skipped += 1
                continue

            print(f"{prefix} {le_title[:55]}")
            await self.rl.wait()

            try:
                result = await self.scrape_lesson(page, lesson, module, cookies)
                result["chapter_title"] = ch_title
                mark_scraped(self.state, lesson_url, result)
                chapters_map.setdefault(ch_title, []).append(result)
                scraped += 1
                v = "✓" if result.get("video_url") else "○"
                a = "✓" if result.get("audio_path") else "○"
                t = "✓" if result.get("vtt_path") else "○"
                d = "⚑" if result.get("drm_protected") else " "
                print(f"         video={v} audio={a} vtt={t} drm={d}")

            except Exception as e:
                err_msg = str(e)
                print(f"{prefix} ✗ Error: {err_msg[:80]}")
                traceback.print_exc()
                mark_error(self.state, lesson_url, err_msg)
                errors += 1

            # State alle 10 Lektionen speichern
            if (idx + 1) % 10 == 0:
                save_state(self.state, STATE_FILE)

        print(f"\n  → Fertig: {scraped} gescrapt, {skipped} übersprungen, {errors} Fehler")

        # Validierung
        validation = self.validate(module, chapters_map)
        self._print_validation(module, validation)

        # JSON-Output schreiben
        json_path = self.cfg.output_dir / "json" / f"{module.slug}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "module":      module.name,
            "module_slug": module.slug,
            "course_url":  f"{self.cfg.base_url}/library/{module.course_id}",
            "scraped_at":  datetime.now(timezone.utc).isoformat(),
            "validation":  validation,
            "chapters": [
                {
                    "chapter_title": ct,
                    "lesson_count":  len(lessons),
                    "lessons":       lessons,
                }
                for ct, lessons in chapters_map.items()
                if lessons
            ],
        }
        json_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        print(f"  → JSON: {json_path.relative_to(SCRIPT_DIR)}")

        return chapters_map

    # ── Validierungs-Ausgabe ───────────────────────────────────────────────────

    def _print_validation(self, module: ModuleConfig, v: dict):
        ok_sym = "✓" if v["total_ok"] else ("?" if v["total_ok"] is None else "✗")
        exp = v["total_expected"] or "?"
        act = v["total_actual"]
        print(f"\n  ── Validierung: {module.name} ──")
        print(f"  Gesamt:  erwartet={exp}  tatsächlich={act}  {ok_sym}")
        if v["chapters"]:
            print(f"  {'Kapitel-Hint':<30} {'Match':<35} {'Erw':>4} {'Ist':>4} {'':>2}")
            print(f"  {'-'*30} {'-'*35} {'-'*4} {'-'*4} {'-'*2}")
            for c in v["chapters"]:
                sym = ("✓" if c["ok"] else ("?" if c["ok"] is None else "✗"))
                exp_c = str(c["expected"]) if c["expected"] is not None else "?"
                act_c = str(c["actual"])   if c["actual"]   is not None else "?"
                match = c["matched_title"][:35]
                hint  = (c["hint"] or "?")[:30]
                print(f"  {hint:<30} {match:<35} {exp_c:>4} {act_c:>4} {sym:>2}")


# ── Gesamt-Zusammenfassung ─────────────────────────────────────────────────────

def print_summary(results: dict):
    print(f"\n{'='*64}")
    print("  SCRAPE ABGESCHLOSSEN — ZUSAMMENFASSUNG")
    print(f"{'='*64}")
    print(f"  {'Modul':<40} {'Erw':>5} {'Ist':>5} {'':>2}")
    print(f"  {'-'*40} {'-'*5} {'-'*5} {'-'*2}")
    total_exp = 0
    total_act = 0
    for slug, v in results.items():
        exp = v.get("total_expected")
        act = v.get("total_actual", 0)
        sym = "✓" if (exp and act == exp) else ("?" if not exp else "✗")
        exp_s = str(exp) if exp else "?"
        total_exp += exp or 0
        total_act += act
        # Slug → schöner Name aus results
        print(f"  {slug:<40} {exp_s:>5} {act:>5} {sym:>2}")
    print(f"  {'─'*40} {'─'*5} {'─'*5}")
    print(f"  {'GESAMT':<40} {total_exp:>5} {total_act:>5}")


# ── Einstiegspunkt ─────────────────────────────────────────────────────────────

async def main():
    global STATE_FILE

    config = load_config(CONFIG_FILE)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    STATE_FILE = config.output_dir / "state" / "progress.json"
    state = load_state(STATE_FILE)

    print(f"Niggehoff Scraper v2 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Output-Dir : {config.output_dir}")
    print(f"State-File : {STATE_FILE}")
    print(f"Module     : {len(config.modules)}")
    scraped_count = sum(1 for v in state.values() if v.get("status") == "ok")
    print(f"Bereits fertig (State): {scraped_count} Lektionen\n")

    # Blacklist prüfen
    scraper = MemberspotScraper(config, state)
    summary_results = {}

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=False, args=["--window-size=1440,900"]
        )
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        await scraper.login(page)
        cookies = await context.cookies()

        for module in config.modules:
            if module.course_id in config.blacklist_course_ids:
                print(f"\n[BLACKLIST] Überspringe: {module.name}")
                continue

            try:
                chapters_map = await scraper.scrape_module(page, module, cookies)
                save_state(state, STATE_FILE)
                total_actual = sum(len(v) for v in chapters_map.values())
                summary_results[module.name] = {
                    "total_expected": module.expected_total,
                    "total_actual":   total_actual,
                }
            except Exception as e:
                print(f"\n✗ Modul {module.name} abgebrochen: {e}")
                traceback.print_exc()
                save_state(state, STATE_FILE)
                summary_results[module.name] = {"total_expected": module.expected_total, "total_actual": 0}

        await browser.close()

    save_state(state, STATE_FILE)
    print_summary(summary_results)


if __name__ == "__main__":
    asyncio.run(main())
