#!/usr/bin/env python3
"""
Generiert KI-Zusammenfassungen für alle Lektionen mit Transkript.
Modell: gpt-4o-mini (günstig, schnell)
Output: summaries.json  { lessonId: { bullets, key_takeaway, concepts } }
"""

import json
import os
import time
from pathlib import Path
from openai import OpenAI

COURSES_JSON = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/courses.json")
OUT          = Path("/Users/steffenwinter/Documents/Claude/Niggehoff Videokurs/summaries.json")
MIN_CHARS    = 150  # Transkripte kürzer als das überspringen

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMPT = """Du bist ein präziser Lernassistent. Du analysierst Kurs-Transkripte und extrahierst das Wesentliche.

Antworte IMMER als valides JSON in exakt diesem Format:
{
  "bullets": ["Punkt 1", "Punkt 2", "Punkt 3"],
  "key_takeaway": "Das eine Ding das bleibt — max. 1 Satz.",
  "concepts": [
    { "term": "Priming", "wikipedia_de": "Priming_(Psychologie)" },
    { "term": "Social Proof", "wikipedia_de": "Soziale_Bewährtheit" }
  ]
}

Regeln:
- bullets: 3–5 konkrete Aussagen, keine Floskeln, kein "In diesem Video..."
- key_takeaway: das eine Kernprinzip, aktionsorientiert
- concepts: NUR echte psychologische/fachliche Begriffe die im Text vorkommen (Priming, Anchoring, Framing, Ego Labeling, Reziprozität etc.) — leer lassen wenn keine vorhanden
- wikipedia_de: exakter deutscher Wikipedia-Artikelname (ohne /wiki/) — nur wenn du sicher bist
- Sprache: Deutsch
- Kein Padding, keine Begrüßung, direkt zum Punkt"""

USER_TEMPLATE = """Kurs: {course}
Lektion: {title}

Transkript:
{transcript}"""


def summarize(course_title, lesson_title, transcript):
    prompt = USER_TEMPLATE.format(
        course=course_title,
        title=lesson_title,
        transcript=transcript[:4000]
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt}
        ],
        temperature=0.2,
        max_tokens=600,
        response_format={"type": "json_object"}
    )
    return json.loads(resp.choices[0].message.content)


def main():
    data = json.load(open(COURSES_JSON))

    # Load existing summaries (resume support)
    existing = {}
    if OUT.exists():
        existing = json.load(open(OUT))
    print(f"Bereits vorhanden: {len(existing)} Zusammenfassungen")

    # Collect all lessons with transcripts
    queue = []
    for course in data:
        for ch in course["chapters"]:
            for lesson in ch["lessons"]:
                t = lesson.get("transcript", "")
                if len(t) < MIN_CHARS:
                    continue
                if lesson["id"] in existing:
                    continue
                queue.append((course["title"], lesson["title"], lesson["id"], t))

    total = len(queue)
    print(f"Zu verarbeiten: {total} Lektionen\n")
    if not total:
        print("Nichts zu tun — alles fertig!")
        return

    done = 0
    errors = 0

    for course_title, lesson_title, lesson_id, transcript in queue:
        try:
            result = summarize(course_title, lesson_title, transcript)
            existing[lesson_id] = result
            done += 1
            pct = done / total * 100
            print(f"[{done:03d}/{total}] ✓ {lesson_title[:55]}")

            # Save every 10 to not lose progress
            if done % 10 == 0:
                OUT.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
                print(f"  → gespeichert ({done} bisher)")

            time.sleep(0.3)  # Rate limiting

        except Exception as e:
            errors += 1
            print(f"  ✗ Fehler bei '{lesson_title[:40]}': {e}")
            time.sleep(1)

    OUT.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"\n✓ Fertig: {done} generiert, {errors} Fehler")
    print(f"Dateigröße: {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
