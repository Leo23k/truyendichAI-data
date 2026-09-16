#!/usr/bin/env python3
"""Generate cached Edge TTS audio for synced LeoTruyen chapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

import edge_tts


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DEFAULT_VOICE = "vi-VN-HoaiMyNeural"
SUPPORTED_VOICES = {DEFAULT_VOICE, "vi-VN-NamMinhNeural"}
CHUNK_SIZE = 2_500


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, value: dict) -> bool:
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    path.write_text(rendered, encoding="utf-8")
    return True


def audio_path(chapter_number: int, voice: str) -> str:
    base = f"audio/{chapter_number:03d}"
    return f"{base}.mp3" if voice == DEFAULT_VOICE else f"{base}-namminh.mp3"


def split_text(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in (part.strip() for part in text.splitlines() if part.strip()):
        while len(paragraph) > CHUNK_SIZE:
            cut = paragraph.rfind(" ", 0, CHUNK_SIZE)
            if cut < CHUNK_SIZE // 2:
                cut = CHUNK_SIZE
            piece, paragraph = paragraph[:cut].strip(), paragraph[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        candidate = f"{current}\n{paragraph}".strip()
        if current and len(candidate) > CHUNK_SIZE:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def synthesize(text: str, destination: Path, voice: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    pieces = split_text(text)
    if not pieces:
        raise ValueError("Chapter has no readable content")

    with tempfile.TemporaryDirectory(prefix="leotruyen-tts-") as temp_dir:
        temp_path = Path(temp_dir)
        for index, piece in enumerate(pieces):
            segment = temp_path / f"{index:04d}.mp3"
            for attempt in range(1, 4):
                try:
                    await edge_tts.Communicate(piece, voice).save(str(segment))
                    if segment.exists() and segment.stat().st_size > 0:
                        break
                except Exception as error:  # Network and provider failures are transient.
                    logging.warning("TTS attempt %s/3 failed: %s", attempt, error)
                await asyncio.sleep(attempt)
            else:
                raise RuntimeError(f"Could not synthesize audio segment {index + 1}")

        with destination.open("wb") as output:
            for segment in sorted(temp_path.glob("*.mp3")):
                with segment.open("rb") as source:
                    shutil.copyfileobj(source, output)

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("TTS output file is empty")


async def generate(voice: str) -> int:
    metadata_path = DATA_DIR / "metadata.json"
    if not metadata_path.exists():
        logging.info("No metadata found at %s", metadata_path)
        return 0

    generated = 0
    metadata = read_json(metadata_path)
    for novel in metadata.get("novels", []):
        slug = novel.get("slug")
        if not slug:
            continue
        novel_dir = DATA_DIR / "novels" / slug
        manifest_path = novel_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        manifest_changed = False

        for entry in manifest.get("chapters", []):
            chapter_url = entry.get("url")
            if not entry.get("synced") or not chapter_url:
                continue
            chapter_path = novel_dir / chapter_url
            if not chapter_path.exists():
                continue
            chapter = read_json(chapter_path)
            text = str(chapter.get("content", "")).strip()
            if not text:
                continue

            number = int(entry.get("number", chapter.get("number", 0)))
            relative_audio = audio_path(number, voice)
            target = novel_dir / relative_audio
            if not target.exists() or target.stat().st_size == 0:
                logging.info("Generating %s with %s", target, voice)
                await synthesize(text, target, voice)
                generated += 1
            else:
                logging.info("Audio cache hit: %s", target)

            entry_voices = entry.setdefault("audio_by_voice", {})
            chapter_voices = chapter.setdefault("audio_by_voice", {})
            if entry_voices.get(voice) != relative_audio:
                entry_voices[voice] = relative_audio
                manifest_changed = True
            if chapter_voices.get(voice) != relative_audio:
                chapter_voices[voice] = relative_audio
                write_json(chapter_path, chapter)
            if voice == DEFAULT_VOICE:
                if entry.get("audio") != relative_audio:
                    entry["audio"] = relative_audio
                    manifest_changed = True
                if entry.get("audio_url") != relative_audio:
                    entry["audio_url"] = relative_audio
                    manifest_changed = True
                if chapter.get("audio") != relative_audio:
                    chapter["audio"] = relative_audio
                    write_json(chapter_path, chapter)
                if chapter.get("audio_url") != relative_audio:
                    chapter["audio_url"] = relative_audio
                    write_json(chapter_path, chapter)

        if manifest_changed:
            write_json(manifest_path, manifest)

    return generated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default=DEFAULT_VOICE, choices=sorted(SUPPORTED_VOICES))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    count = asyncio.run(generate(args.voice))
    logging.info("Generated %s audio file(s)", count)


if __name__ == "__main__":
    main()
