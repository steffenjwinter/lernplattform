#!/usr/bin/env python3
"""
whisper_fallback.py — Transkribiert Audio via OpenAI Whisper → VTT (mit Timecodes)

Voraussetzungen:
  pip install openai-whisper
  # Für GPU-Beschleunigung: pip install torch  (optional)

Standalone-Nutzung:
  python3 whisper_fallback.py data/audio/perfect-pitch/.../lektion.m4a

Wird auch von scrape_v2.py aufgerufen wenn kein VTT verfügbar.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path


def format_vtt_time(seconds: float) -> str:
    """Sekunden → VTT-Timestamp HH:MM:SS.mmm"""
    h   = int(seconds // 3600)
    m   = int((seconds % 3600) // 60)
    s   = seconds % 60
    ms  = int(round((s - int(s)) * 1000))
    return f"{h:02d}:{m:02d}:{int(s):02d}.{ms:03d}"


def transcribe_to_vtt(
    audio_path: str | Path,
    output_path: str | Path,
    model_name: str = "medium",
    language: str = "de",
) -> bool:
    """
    Transkribiert eine Audiodatei und schreibt das Ergebnis als .vtt mit Timecodes.

    Args:
        audio_path:  Pfad zur Audiodatei (.m4a, .mp3, .wav …)
        output_path: Zielpfad für die .vtt-Datei
        model_name:  Whisper-Modell ("tiny","base","small","medium","large")
                     Empfehlung: "medium" für Deutsch, "large" für beste Qualität
        language:    ISO-Sprachcode ("de" = Deutsch)

    Returns:
        True bei Erfolg, False bei Fehler
    """
    audio_path  = Path(audio_path)
    output_path = Path(output_path)

    if not audio_path.exists():
        print(f"[whisper] Datei nicht gefunden: {audio_path}")
        return False

    try:
        import whisper
    except ImportError:
        print("[whisper] openai-whisper nicht installiert. Bitte: pip install openai-whisper")
        return False

    try:
        print(f"[whisper] Lade Modell '{model_name}'…")
        model = whisper.load_model(model_name)

        print(f"[whisper] Transkribiere: {audio_path.name}")
        result = model.transcribe(
            str(audio_path),
            language=language,
            task="transcribe",
            verbose=False,
            word_timestamps=False,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["WEBVTT", ""]
        for i, seg in enumerate(result.get("segments", []), start=1):
            start = format_vtt_time(seg["start"])
            end   = format_vtt_time(seg["end"])
            text  = seg["text"].strip()
            # Sonderzeichen bereinigen
            text  = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            if text:
                lines.append(str(i))
                lines.append(f"{start} --> {end}")
                lines.append(text)
                lines.append("")

        output_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[whisper] VTT geschrieben: {output_path} ({len(result['segments'])} Segmente)")
        return True

    except Exception as e:
        print(f"[whisper] Fehler: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 whisper_fallback.py <audio_file> [output.vtt] [model=medium]")
        sys.exit(1)

    audio  = Path(sys.argv[1])
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else audio.with_suffix(".vtt")
    model  = sys.argv[3] if len(sys.argv) > 3 else "medium"

    success = transcribe_to_vtt(audio, output, model_name=model)
    sys.exit(0 if success else 1)
