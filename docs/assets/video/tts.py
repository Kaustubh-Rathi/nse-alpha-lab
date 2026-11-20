#!/usr/bin/env python
"""Narrate the walkthrough with edge-tts and build caption cues.

Inputs
------
    docs/assets/video/narration.json   voice, rate, scenes, sentences, leads

Outputs
-------
    docs/assets/video/audio/<id>.wav   one PCM WAV per scene (gitignored)
    docs/assets/video/subs/<id>.srt    per-scene captions (gitignored)
    docs/assets/video/captions.json    cue times relative to each scene
    docs/assets/video/durations.json   measured audio duration per scene

Why sentence-by-sentence?
-------------------------
edge-tts occasionally returns no audio for a long paragraph. Synthesising one
sentence at a time is reliable, gives us exact word boundaries, and lets us
insert a consistent pause between sentences. Captions are grouped from those
word boundaries, so they follow the voice instead of being one wall of text per
scene.

Usage
-----
    python docs/assets/video/tts.py
    python docs/assets/video/tts.py --only 07_results,08_outro
    python docs/assets/video/tts.py --voice en-US-EmmaNeural --rate +4%
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import edge_tts
except ImportError:  # pragma: no cover - the message is the test
    raise SystemExit("edge-tts is not installed; run: uv pip install edge-tts")

HERE = Path(__file__).resolve().parent
AUDIO = HERE / "audio"
SUBS = HERE / "subs"
NARRATION = HERE / "narration.json"
CAPTIONS = HERE / "captions.json"
DURATIONS = HERE / "durations.json"

MAX_LINE = 48
MAX_LINES = 2
MAX_CUE_SECONDS = 4.6
MIN_CUE_SECONDS = 0.9
SENTENCE_GAP = 0.18

#: Spoken text is written for the voice; captions are written for a reader.
_DISPLAY = (
    ("Nifty 500", "NIFTY 500"),
    ("Nifty 50", "NIFTY 50"),
    ("N S E", "NSE"),
    ("U V", "uv"),
    ("S T T", "STT"),
    ("G S T", "GST"),
    ("J S O N", "JSON"),
    ("C S V", "CSV"),
    ("U I", "UI"),
    ("C L I", "CLI"),
    ("O L S", "OLS"),
    ("pre specified", "pre-specified"),
    ("point in time", "point-in-time"),
    ("split and bonus adjusted", "split-and-bonus-adjusted"),
    ("Newey West", "Newey-West"),
    ("t statistics", "t-statistics"),
    ("p values", "p-values"),
    ("t statistic", "t-statistic"),
    ("p value", "p-value"),
    ("block bootstrap", "block-bootstrap"),
    ("event anchored", "event-anchored"),
    ("fail closed", "fail-closed"),
    ("price only", "price-only"),
    ("benchmark aligned", "benchmark-aligned"),
    ("multi gigabyte", "multi-gigabyte"),
    ("look ahead", "look-ahead"),
    ("T plus one", "T+1"),
    ("63 session", "63-session"),
    ("968 row", "968-row"),
    ("19.3 percent", "19.3%"),
    ("19.45 percent", "19.45%"),
    ("161 megabyte", "161 MB"),
    ("9.73 percentage points", "9.73 pp"),
    ("t equals 1.96", "t = 1.96"),
    ("zero failures", "0 failures"),
)


def display(text: str) -> str:
    for spoken, written in _DISPLAY:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(spoken)}(?![A-Za-z0-9])"
        text = re.sub(pattern, written, text)
    return text


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def balanced_wrap(text: str, limit: int = MAX_LINE) -> list[str]:
    """Wrap a caption into at most two balanced lines."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text]

    words = text.split()
    best_score: tuple[int, int] | None = None
    best_index: int | None = None
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        if len(left) <= limit and len(right) <= limit:
            score = (max(len(left), len(right)), abs(len(left) - len(right)))
            if best_score is None or score < best_score:
                best_score = score
                best_index = index
    if best_index is not None:
        return [" ".join(words[:best_index]), " ".join(words[best_index:])]

    # A single unbreakable caption longer than two lines: fall back to greedy.
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if len(trial) <= limit or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def cue_fits(words: list[dict]) -> bool:
    text = display(" ".join(word["text"] for word in words))
    if len(text) > MAX_LINE * MAX_LINES:
        return False
    return len(balanced_wrap(text)) <= MAX_LINES


def cue_seconds(words: list[dict]) -> float:
    return words[-1]["end"] - words[0]["start"]


def split_clauses(tokens: list[dict]) -> list[list[dict]]:
    clauses: list[list[dict]] = []
    current: list[dict] = []
    for token in tokens:
        current.append(token)
        if re.search(r"[,;:!?\.]$", token["text"]) or token.get("sentence_end"):
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)
    return clauses


def split_long_clause(words: list[dict]) -> list[list[dict]]:
    """Split a clause into balanced pieces instead of orphaning its last word."""
    if cue_fits(words) and cue_seconds(words) <= MAX_CUE_SECONDS:
        return [words]
    if len(words) <= 1:
        return [words]

    best_index: int | None = None
    best_score: tuple[int, int] | None = None
    for index in range(1, len(words)):
        left_len = len(display(" ".join(word["text"] for word in words[:index])))
        right_len = len(display(" ".join(word["text"] for word in words[index:])))
        score = (abs(left_len - right_len), max(left_len, right_len))
        if best_score is None or score < best_score:
            best_score = score
            best_index = index
    if best_index is None:
        return [words]
    return split_long_clause(words[:best_index]) + split_long_clause(words[best_index:])


def build_cues(tokens: list[dict], lead: float) -> list[dict]:
    """Group clauses into short cues, then wrap each cue into two lines."""
    groups: list[list[dict]] = []
    current: list[dict] = []

    for clause in split_clauses(tokens):
        sentence_end = bool(clause[-1].get("sentence_end"))
        if current and (
            not cue_fits(current + clause)
            or cue_seconds(current + clause) > MAX_CUE_SECONDS
        ):
            groups.append(current)
            current = clause
        else:
            current = current + clause

        if current and not cue_fits(current):
            pieces = split_long_clause(current)
            groups.extend(pieces[:-1])
            current = pieces[-1]

        if sentence_end:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    cues: list[dict] = []
    for index, group in enumerate(groups):
        start = max(0.0, group[0]["start"] - 0.08)
        end = group[-1]["end"] + 0.18
        if index + 1 < len(groups):
            end = min(end, groups[index + 1][0]["start"] - 0.04)
        if end - start < MIN_CUE_SECONDS:
            end = start + MIN_CUE_SECONDS
        text = display(" ".join(word["text"] for word in group))
        lines = balanced_wrap(text)
        cues.append(
            {
                "start": round(start + lead, 3),
                "end": round(end + lead, 3),
                "text": "\n".join(lines),
            }
        )

    # Never let two cues overlap.
    for left, right in zip(cues, cues[1:]):
        if left["end"] > right["start"]:
            left["end"] = round(max(left["start"] + 0.5, right["start"] - 0.03), 3)
    return cues


def align_words(sentence: str, events: list[dict], fallback_seconds: float) -> list[dict]:
    """Attach the original punctuation to edge-tts's word boundaries."""
    raw = sentence.split()
    parsed = [
        (
            event["text"],
            float(event["offset"]) / 10_000_000,
            (float(event["offset"]) + float(event["duration"])) / 10_000_000,
        )
        for event in events
    ]

    if not parsed:
        # No boundaries: spread the sentence over its measured duration.
        span = max(0.1, fallback_seconds)
        step = span / max(1, len(raw))
        return [
            {"text": token, "start": i * step, "end": (i + 1) * step}
            for i, token in enumerate(raw)
        ]

    tokens: list[dict] = []
    cursor = 0
    for token in raw:
        wanted = normalize(token)
        found = None
        for index in range(cursor, min(len(parsed), cursor + 3)):
            if normalize(parsed[index][0]) == wanted:
                found = index
                break
        if found is None:
            found = min(cursor, len(parsed) - 1)
        _, start, end = parsed[found]
        tokens.append({"text": token, "start": start, "end": end})
        cursor = found + 1
    if tokens:
        tokens[-1]["sentence_end"] = True
    return tokens


async def synthesise(text: str, voice: str, rate: str, path: Path) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(7):
        try:
            communicate = edge_tts.Communicate(
                text, voice, rate=rate, boundary="WordBoundary"
            )
            audio = bytearray()
            events: list[dict] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    events.append(chunk)
            if audio:
                path.write_bytes(audio)
                return events
            last_error = RuntimeError("edge-tts returned no audio")
        except Exception as exc:  # noqa: BLE001 - retry the service
            last_error = exc
        await asyncio.sleep(min(0.5 * (attempt + 1), 3.0))
    raise RuntimeError(f"edge-tts failed for {text[:48]!r}: {last_error}")


def ffmpeg_wav(source: Path, target: Path, ffmpeg: str) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ar",
            "48000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        check=True,
    )


def probe_duration(path: Path, ffmpeg: str) -> float:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(f"could not read duration of {path}")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def stamp(value: float) -> str:
    value = max(0.0, value)
    hours, rest = divmod(value, 3600)
    minutes, rest = divmod(rest, 60)
    seconds, millis = divmod(rest, 1)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{round(millis * 1000):03d}"


def write_srt(cues: list[dict], path: Path) -> None:
    blocks: list[str] = []
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['text']}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", default="")
    parser.add_argument("--rate", default="")
    parser.add_argument("--only", default="", help="comma-separated scene ids")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    args = parser.parse_args()

    ffmpeg = args.ffmpeg
    if not shutil.which(ffmpeg) and not Path(ffmpeg).is_file():
        print(f"ffmpeg not found: {ffmpeg}", file=sys.stderr)
        return 2

    spec = json.loads(NARRATION.read_text(encoding="utf-8"))
    voice = args.voice or spec.get("voice", "en-US-BrianNeural")
    rate = args.rate or spec.get("rate", "+6%")
    scenes = spec["scenes"]
    wanted = {value.strip() for value in args.only.split(",") if value.strip()}
    selected = [scene for scene in scenes if not wanted or scene["id"] in wanted]
    missing = wanted - {scene["id"] for scene in selected}
    if missing:
        print(f"unknown scene id(s): {', '.join(sorted(missing))}", file=sys.stderr)
        return 2

    AUDIO.mkdir(parents=True, exist_ok=True)
    SUBS.mkdir(parents=True, exist_ok=True)

    existing_captions: dict[str, list[dict]] = {}
    if CAPTIONS.exists():
        existing_captions = json.loads(CAPTIONS.read_text(encoding="utf-8"))
    existing_durations: dict[str, float] = {}
    if DURATIONS.exists():
        existing_durations = json.loads(DURATIONS.read_text(encoding="utf-8-sig"))

    print(f"voice: {voice}  rate: {rate}")
    for scene in selected:
        scene_id = scene["id"]
        lead = float(scene.get("lead", 0.35))
        sentences = scene["sentences"]
        work = Path(tempfile.mkdtemp(prefix=f"{scene_id}-", dir=AUDIO))

        try:
            sentence_wavs: list[Path] = []
            sentence_durations: list[float] = []
            sentence_tokens: list[list[dict]] = []
            for index, sentence in enumerate(sentences):
                print(f"[{scene_id}] sentence {index + 1}/{len(sentences)}: {sentence[:58]}...")
                mp3 = work / f"{index:02d}.mp3"
                wav = work / f"{index:02d}.wav"
                events = await synthesise(sentence, voice, rate, mp3)
                ffmpeg_wav(mp3, wav, ffmpeg)
                duration = probe_duration(wav, ffmpeg)
                sentence_wavs.append(wav)
                sentence_durations.append(duration)
                sentence_tokens.append(align_words(sentence, events, duration))

            silence = work / "silence.wav"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=mono",
                    "-t",
                    f"{SENTENCE_GAP:.3f}",
                    "-c:a",
                    "pcm_s16le",
                    silence,
                ],
                check=True,
            )

            listing = work / "concat.txt"
            entries: list[str] = []
            for index, wav in enumerate(sentence_wavs):
                entries.append(f"file '{wav.as_posix()}'")
                if index + 1 < len(sentence_wavs):
                    entries.append(f"file '{silence.as_posix()}'")
            listing.write_text("\n".join(entries) + "\n", encoding="utf-8")

            scene_wav = AUDIO / f"{scene_id}.wav"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(scene_wav),
                ],
                check=True,
            )

            tokens: list[dict] = []
            clock = 0.0
            for index, sentence in enumerate(sentences):
                for token in sentence_tokens[index]:
                    tokens.append(
                        {
                            "text": token["text"],
                            "start": token["start"] + clock,
                            "end": token["end"] + clock,
                            **(
                                {"sentence_end": True}
                                if token.get("sentence_end")
                                else {}
                            ),
                        }
                    )
                clock += sentence_durations[index] + SENTENCE_GAP

            cues = build_cues(tokens, lead)
            existing_captions[scene_id] = cues
            existing_durations[scene_id] = round(probe_duration(scene_wav, ffmpeg), 3)
            write_srt(cues, SUBS / f"{scene_id}.srt")
            print(
                f"[{scene_id}] {existing_durations[scene_id]:.2f}s audio, "
                f"{len(cues)} caption cues"
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    ordered_captions = {
        scene["id"]: existing_captions[scene["id"]]
        for scene in scenes
        if scene["id"] in existing_captions
    }
    ordered_durations = {
        scene["id"]: existing_durations[scene["id"]]
        for scene in scenes
        if scene["id"] in existing_durations
    }
    CAPTIONS.write_text(json.dumps(ordered_captions, indent=2) + "\n", encoding="utf-8")
    DURATIONS.write_text(json.dumps(ordered_durations, indent=2) + "\n", encoding="utf-8")

    total = sum(ordered_durations.values())
    print(f"\n{len(selected)} scene(s), {total / 60:.2f} minutes of audio")
    print(f"wrote {CAPTIONS.name}, {DURATIONS.name} and {len(selected)} SRT file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
