#!/usr/bin/env python3
"""
Transkribiert alle Lektionen ohne Transkript via mlx-whisper (Apple M2).
Audio wird direkt vom CDN gestreamt — keine MP4-Datei wird gespeichert.
"""
import json, subprocess, tempfile, os, re, time
from pathlib import Path
import mlx_whisper

COURSES_JSON  = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/courses.json")
SUMMARIES_F   = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/summaries.json")
LOG_FILE      = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/transcribe.log")

# Kurse komplett überspringen
SKIP_COURSES = {
    "Niggehoff TÜV Zertifikat",
    "Starte hier mit dem Training (Onboarding)",
    "Live-Call & Aufzeichnungen - SALES & DIREKTAKQUISE",
    "Live-Call & Aufzeichnungen - STRATEGIE & MENTALITÄT",
    "Live-Calls & Aufzeichnungen - Online-Verkaufspsychologie PRO-TRAINING",
}

# Einzelne Lektions-Titel überspringen
SKIP_TITLE_KEYWORDS = {"herzlich willkommen", "grundmentalität"}

MODEL = "mlx-community/whisper-large-v3-turbo"
MIN_CHARS = 100


def log(msg):
    print(msg, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(msg + "\n")


def stream_transcribe(audio_url: str) -> str:
    """Stream audio from URL via ffmpeg, transcribe with mlx-whisper. No file saved."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", audio_url,
            "-vn",                    # audio only — skip video segments
            "-acodec", "pcm_s16le",  # WAV PCM
            "-ar", "16000",           # 16kHz (Whisper standard)
            "-ac", "1",               # mono
            "-t", "3600",             # max 1h safety cap
            tmp_path
        ], capture_output=True, timeout=300)

        if result.returncode != 0 or not os.path.exists(tmp_path):
            log(f"    [ffmpeg error] {result.stderr.decode()[:200]}")
            return ""

        size_mb = os.path.getsize(tmp_path) / 1024 / 1024
        log(f"    audio: {size_mb:.1f} MB — transcribing...")

        out = mlx_whisper.transcribe(
            tmp_path,
            path_or_hf_repo=MODEL,
            language="de",
            word_timestamps=False,
        )
        text = out.get("text", "").strip()
        return text

    except subprocess.TimeoutExpired:
        log("    [timeout]")
        return ""
    except Exception as e:
        log(f"    [ERR] {e}")
        return ""
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    LOG_FILE.write_text("")
    data = json.loads(COURSES_JSON.read_text())

    # Collect lessons to process
    todo = []
    for c in data:
        if c["title"] in SKIP_COURSES:
            continue
        for ch in c["chapters"]:
            for l in ch["lessons"]:
                # Skip if already has transcript
                if l.get("transcript") and len(l["transcript"]) > MIN_CHARS:
                    continue
                # Skip if no video
                audio_url = l.get("audio_url") or l.get("hls_url")
                if not audio_url:
                    continue
                # Skip by title keyword
                title_lower = l["title"].lower()
                if any(kw in title_lower for kw in SKIP_TITLE_KEYWORDS):
                    continue
                todo.append((c["title"], ch["title"], l["id"], l["title"], audio_url))

    log(f"{len(todo)} Lektionen zu transkribieren\n")

    updated = 0
    for i, (course, chapter, lid, title, audio_url) in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {course[:35]} / {title[:50]}")
        t0 = time.time()
        transcript = stream_transcribe(audio_url)

        if not transcript or len(transcript) < MIN_CHARS:
            log(f"    → kein/kurzes Ergebnis ({len(transcript)} Zeichen)")
            continue

        # Write into courses.json lesson
        for c in data:
            if c["title"] != course: continue
            for ch in c["chapters"]:
                if ch["title"] != chapter: continue
                for l in ch["lessons"]:
                    if l["id"] == lid:
                        l["transcript"] = transcript[:5000]
                        updated += 1
                        break

        elapsed = time.time() - t0
        log(f"    → {len(transcript)} Zeichen in {elapsed:.0f}s")

    # Save
    COURSES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log(f"\n{'='*60}")
    log(f"Fertig: {updated} Transkripte gespeichert")


if __name__ == "__main__":
    main()
