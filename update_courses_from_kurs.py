#!/usr/bin/env python3
"""
Rebuilds courses.json from the Kurs directory while preserving ALL manual edits:
  - module assignments (niggehoff / primer / onboarding)
  - custom course ordering
  - cleaned chapter titles and ordering

Run AFTER rescrape_all_missing.py.
"""

import json
import re
import html as html_module
import urllib.request
from pathlib import Path


def is_drm_protected(m3u8_url: str) -> bool:
    """Check if an HLS manifest uses SAMPLE-AES (FairPlay DRM)."""
    try:
        req = urllib.request.Request(m3u8_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read(4096).decode("utf-8", errors="ignore")
            return "SAMPLE-AES" in content or "com.apple.streamingkeydelivery" in content
    except Exception:
        return False

KURS_DIR    = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/Kurs")
COURSES_JSON = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/courses.json")

# Courses to skip entirely (Live-Call archives that were intentionally removed)
SKIP_COURSES = {
    "Live-Calls - PRO-TRAINING 2026",
    "Live-Call - SALES & DIREKTAKQUISE",
    "Live-Call - STRATEGIE & MENTALITÄT",
}

# Chapter IDs/patterns that belong to onboarding (hidden from main view)
ONBOARDING_COURSE_IDS = {"COBE special Onboarding", "Starte hier dein Onboarding"}


def lesson_natural_key(name: str):
    m = re.match(r'^lektion_(\d+)', name)
    if m:
        return (0, int(m.group(1)), name)
    m = re.match(r'^(\d+)[_\s-]', name)
    if m:
        return (1, int(m.group(1)), name)
    return (2, 0, name)


def parse_vtt(vtt_path: Path) -> str:
    try:
        text = vtt_path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r'WEBVTT.*?\n\n', '', text, flags=re.DOTALL)
        text = re.sub(r'\d{2}:\d{2}[\d:.,]+ --> [\d:.,]+\s*\n', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        return re.sub(r'\n+', ' ', text).strip()[:5000]
    except Exception:
        return ""


def build_lesson(lesson_dir: Path) -> dict | None:
    meta_path = lesson_dir / "_meta.json"
    if not meta_path.exists():
        return None
    try:
        d = json.loads(meta_path.read_text())
    except:
        return None

    # Must have a valid 3-segment library URL
    segs = d.get("url", "").split("/library/")[-1].split("/")
    if len(segs) != 3:
        return None

    # Title
    name = lesson_dir.name
    if name.startswith("lektion_"):
        raw = re.sub(r'^lektion_\d+\s*', '', name).strip()
    elif re.match(r'^\d+_', name):
        raw = re.sub(r'^\d+_', '', name)
        raw = re.sub(r'\s+\d+ Lektionen.*$', '', raw).strip()
    else:
        raw = name
    title = html_module.unescape(raw)
    if not title or re.match(r'^[A-Za-z0-9_-]{15,}$', title):
        # Fallback to page_title
        pt = d.get("page_title", "").strip()
        if pt and len(pt) > 3:
            title = pt

    # CDN URLs
    video_urls_all = [u for u in d.get("video_urls", []) if "mspot-vod" in u and u.endswith(".mp4")]
    audio_url  = next((u for u in video_urls_all if "audio" in u or "mp4a" in u), None)
    video_url  = next((u for u in video_urls_all if "video" in u or "avc1" in u), None)
    hls_url    = d.get("hls_url") or None

    # Check for DRM-protected HLS (FairPlay SAMPLE-AES)
    if hls_url and is_drm_protected(hls_url):
        hls_url = None
        video_url = None
        audio_url = None
    vtt_url    = d.get("vtt_urls", [None])[0] if d.get("vtt_urls") else None
    img_url    = d.get("img_urls", [None])[0] if d.get("img_urls") else None

    # Local transcript
    local_vtt = lesson_dir / "transkript_1.vtt"
    transcript = parse_vtt(local_vtt) if local_vtt.exists() else ""

    # Materials
    materials = []
    mat_dir = lesson_dir / "material"
    if mat_dir.exists():
        for f in sorted(mat_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                materials.append({
                    "name": f.name,
                    "path": str(f.relative_to(KURS_DIR)),
                })

    return {
        "id": name,
        "title": title,
        "url": d.get("url", ""),
        "audio_url": audio_url,
        "video_url": video_url,
        "hls_url": hls_url,
        "vtt_url": vtt_url,
        "img_url": img_url,
        "transcript": transcript,
        "materials": materials,
        "has_combined": (lesson_dir / "combined.mp4").exists(),
    }


def build_course(course_dir: Path, old_module: str | None) -> dict | None:
    chapters = []

    chapter_dirs = sorted(
        [d for d in course_dir.iterdir() if d.is_dir() and not d.name.startswith("_")]
    )

    # De-duplicate: if both lektion_NNN and 01_NNN exist with same content, prefer 01_
    seen_ids = {}
    deduped_chapters = []
    for ch in chapter_dirs:
        meta = ch / "_meta.json"
        if meta.exists():
            try:
                url = json.loads(meta.read_text()).get("url", "")
                if url:
                    if url in seen_ids:
                        # Keep the one with numeric prefix (01_) over lektion_ at course root
                        existing = seen_ids[url]
                        if re.match(r'^\d+_', ch.name) and ch.name.startswith("lektion_") is False:
                            seen_ids[url] = ch
                            deduped_chapters.remove(existing)
                            deduped_chapters.append(ch)
                        # else keep existing
                        continue
                    seen_ids[url] = ch
            except:
                pass
        deduped_chapters.append(ch)

    for chapter_dir in deduped_chapters:
        chapter_title = re.sub(r'^\d+_', '', chapter_dir.name)
        chapter_title = re.sub(r'\s+\d+ Lektionen.*$', '', chapter_title).strip()
        chapter_title = html_module.unescape(chapter_title)

        lesson_entries = []

        # Collect lesson subdirs
        lesson_dirs = sorted(
            [d for d in chapter_dir.iterdir()
             if d.is_dir() and not d.name.startswith("_")
             and (d / "_meta.json").exists()],
            key=lambda p: lesson_natural_key(p.name)
        )

        # Also handle chapter-as-lesson (meta.json directly in chapter dir)
        if (chapter_dir / "_meta.json").exists() and not lesson_dirs:
            lesson = build_lesson(chapter_dir)
            if lesson:
                lesson_entries.append(lesson)
        elif (chapter_dir / "_meta.json").exists() and lesson_dirs:
            # Chapter has both direct meta AND lesson subdirs (chapter-overview + lessons)
            for ld in lesson_dirs:
                lesson = build_lesson(ld)
                if lesson:
                    lesson_entries.append(lesson)
        else:
            for ld in lesson_dirs:
                lesson = build_lesson(ld)
                if lesson:
                    lesson_entries.append(lesson)

        if lesson_entries:
            chapters.append({
                "id": chapter_dir.name,
                "title": chapter_title,
                "lessons": lesson_entries,
            })

    if not chapters:
        return None

    total = sum(len(c["lessons"]) for c in chapters)
    print(f"  {course_dir.name[:50]}: {len(chapters)} Kapitel, {total} Lektionen")

    # Determine module
    module = old_module
    if module is None:
        if course_dir.name in ONBOARDING_COURSE_IDS:
            module = "onboarding"
        else:
            module = "niggehoff"

    return {
        "id": course_dir.name,
        "title": course_dir.name,
        "chapters": chapters,
        "module": module,
    }


def main():
    # Load current courses.json to preserve module assignments and ordering
    old_courses = json.loads(COURSES_JSON.read_text())
    old_by_id = {c["id"]: c for c in old_courses}

    # Get ordered list of course IDs from old courses.json (for preserving order)
    old_order = [c["id"] for c in old_courses]

    new_courses = []
    processed_dirs = set()

    # First pass: process courses that exist in old courses.json (preserving order)
    for old_id in old_order:
        # Find matching dir
        course_dir = None
        for d in KURS_DIR.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            if d.name == old_id or old_id in d.name or d.name in old_id:
                course_dir = d
                break

        if course_dir is None or course_dir.name in processed_dirs:
            continue

        if any(skip in course_dir.name for skip in SKIP_COURSES):
            processed_dirs.add(course_dir.name)
            continue

        old_module = old_by_id.get(old_id, {}).get("module")
        course = build_course(course_dir, old_module)
        if course:
            # Preserve custom title from old courses.json if different
            old_title = old_by_id.get(old_id, {}).get("title")
            if old_title and old_title != old_id:
                course["title"] = old_title
            new_courses.append(course)
            processed_dirs.add(course_dir.name)

    # Second pass: pick up any new courses (COBE fully scraped, POS, etc.)
    for course_dir in sorted(KURS_DIR.iterdir()):
        if not course_dir.is_dir() or course_dir.name.startswith("_"):
            continue
        if course_dir.name in processed_dirs:
            continue
        if any(skip in course_dir.name for skip in SKIP_COURSES):
            continue

        # Check if it's a Live-Call archive
        if "Live-Call" in course_dir.name or "Live-Calls" in course_dir.name:
            continue

        print(f"  [NEW] {course_dir.name}")
        course = build_course(course_dir, None)
        if course:
            new_courses.append(course)

    COURSES_JSON.write_text(
        json.dumps(new_courses, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    total_lessons = sum(len(c["lessons"]) for course in new_courses for c in course["chapters"])
    print(f"\n✓ courses.json neu geschrieben:")
    print(f"  {len(new_courses)} Kurse")
    print(f"  {total_lessons} Lektionen")
    print(f"  {COURSES_JSON.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
