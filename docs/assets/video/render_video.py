#!/usr/bin/env python
"""Render the NSE Alpha Lab demo video.

Inputs
------
    docs/assets/video/narration.json     scene narration + captions
    docs/assets/video/audio/<id>.wav     one WAV per scene (build_audio.ps1)
    docs/assets/video/durations.json     measured WAV durations
    docs/assets/images/*.png             optional design assets
    docs/assets/ui/*.png                 optional real Streamlit screenshots

Outputs
-------
    docs/assets/demo.mp4                 1080p H.264 + AAC, faststart
    docs/assets/demo.gif                 12-second README preview loop
    docs/assets/demo-poster.png          poster frame
    docs/assets/demo.srt                 captions

Run with the media venv (Pillow, qrcode) and ffmpeg on PATH:

    C:\\path\\to\\media-venv\\Scripts\\python.exe docs/assets/video/render_video.py
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
FPS = 30
ROOT = Path(__file__).resolve().parents[3]          # repository root
ASSETS = ROOT / "docs" / "assets"
VIDEO_DIR = ASSETS / "video"
IMG_DIR = ASSETS / "images"
UI_DIR = ASSETS / "ui"
OUT_MP4 = ASSETS / "demo.mp4"
OUT_GIF = ASSETS / "demo.gif"
OUT_POSTER = ASSETS / "demo-poster.png"
OUT_SRT = ASSETS / "demo.srt"

# Palette
BG_TOP = (7, 14, 26)
BG_BOTTOM = (13, 23, 40)
PANEL = (17, 28, 46)
PANEL_2 = (13, 22, 37)
BORDER = (34, 50, 77)
TEXT = (230, 237, 247)
MUTED = (143, 163, 191)
FAINT = (91, 110, 138)
SAFFRON = (255, 153, 51)
TEAL = (45, 212, 191)
GREEN = (52, 211, 153)
RED = (248, 113, 113)
AMBER = (251, 191, 36)

FONT_DIR = Path("C:/Windows/Fonts")
FONT_FILES = {
    "regular": FONT_DIR / "segoeui.ttf",
    "semibold": FONT_DIR / "seguisb.ttf",
    "bold": FONT_DIR / "segoeuib.ttf",
    "light": FONT_DIR / "segoeuil.ttf",
    "mono": FONT_DIR / "CascadiaMono.ttf",
    "mono_bold": FONT_DIR / "CascadiaCode.ttf",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_FILES[name]), size)
    return _font_cache[key]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def mix(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    words = text.split()
    lines, line = [], ""
    for word in words:
        trial = word if not line else line + " " + word
        if draw.textlength(trial, font=fnt) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def rr(draw, box, radius, fill=None, outline=None, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw, xy, value, fnt, fill=TEXT, anchor="la"):
    draw.text(xy, value, font=fnt, fill=fill, anchor=anchor)


def sentence(draw, xy, value, fnt, fill, width, line_gap=10):
    x, y = xy
    for line in wrap(draw, value, fnt, width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += fnt.size + line_gap
    return y


def paste_contain(img: Image.Image, path: Path, box, radius=18):
    """Paste an image inside box with contain semantics and a rounded mask."""
    if not path.is_file():
        return False
    x, y, w, h = box
    src = Image.open(path).convert("RGB")
    scale = min(w / src.width, h / src.height)
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    resized = src.resize((nw, nh), Image.LANCZOS)
    px, py = x + (w - nw) // 2, y + (h - nh) // 2
    mask = Image.new("L", (nw, nh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, nw - 1, nh - 1), radius=radius, fill=255)
    img.paste(resized, (px, py), mask)
    return True


def paste_cover(img: Image.Image, path: Path, box, radius=18, zoom=1.0):
    if not path.is_file():
        return False
    x, y, w, h = box
    src = Image.open(path).convert("RGB")
    scale = max(w / src.width, h / src.height) * zoom
    nw, nh = max(1, int(src.width * scale)), max(1, int(src.height * scale))
    resized = src.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - w) // 2)
    top = max(0, (nh - h) // 2)
    crop = resized.crop((left, top, left + w, top + h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    img.paste(crop, (x, y), mask)
    return True


# ---------------------------------------------------------------------------
# Background and chrome
# ---------------------------------------------------------------------------

def build_background() -> Image.Image:
    bg = Image.new("RGB", (W, H), BG_TOP)
    px = bg.load()
    for y in range(H):
        t = y / (H - 1)
        color = mix(BG_TOP, BG_BOTTOM, t)
        for x in range(W):
            px[x, y] = color
    draw = ImageDraw.Draw(bg)
    for x in range(0, W, 48):
        for y in range(0, H, 48):
            draw.point((x, y), fill=(24, 37, 58))
    # Soft glows: saffron top-right, teal lower-left.
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W - 760, -460, W + 320, 520), fill=(70, 38, 12))
    gd.ellipse((-420, H - 520, 520, H + 260), fill=(5, 48, 44))
    glow = glow.filter(ImageFilter.GaussianBlur(150))
    return Image.blend(bg, glow, 0.55)


BACKGROUND = build_background()


def chrome(img, draw, caption, index, total, progress):
    # top-left chapter label
    draw.rounded_rectangle((80, 52, 96, 68), radius=4, fill=SAFFRON)
    text(draw, (112, 50), caption.upper(), font("semibold", 21), MUTED)
    # top-right slug
    text(draw, (W - 80, 50), "NSE ALPHA LAB  /  2026", font("semibold", 20),
         FAINT, anchor="ra")
    # bottom rule + progress
    draw.line((80, 1006, W - 80, 1006), fill=(28, 42, 64), width=2)
    draw.line((80, 1006, 80 + int((W - 160) * progress), 1006), fill=SAFFRON, width=3)
    text(draw, (80, 1022), "github.com/Kaustubh-Rathi/nse-alpha-lab",
         font("regular", 18), FAINT)
    text(draw, (W - 80, 1022), "research preview  ·  not investment advice",
         font("regular", 18), FAINT, anchor="ra")
    text(draw, (W / 2, 1022), f"{index + 1} / {total}", font("mono", 17),
         FAINT, anchor="ma")


def fade(img, t, dur, fade_in=0.35, fade_out=0.45):
    alpha = 1.0
    if t < fade_in:
        alpha = t / fade_in
    elif t > dur - fade_out:
        alpha = max(0.0, (dur - t) / fade_out)
    if alpha >= 1.0:
        return img
    black = Image.new("RGB", img.size, (0, 0, 0))
    return Image.blend(black, img, smooth(alpha))


def header(draw, eyebrow, title, sub=None):
    text(draw, (80, 104), eyebrow.upper(), font("semibold", 20), SAFFRON)
    text(draw, (80, 140), title, font("semibold", 54), TEXT)
    if sub:
        text(draw, (80, 210), sub, font("regular", 28), MUTED)


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------

def terminal_frame(img, box, lines, elapsed, title="PowerShell — nse-alpha-lab",
                   cps_cmd=42, cps_out=280, lead=0.0):
    """Draw a terminal and reveal lines on a schedule. Returns total height used."""
    x, y, w, h = box
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x + 10, y + 16, x + w + 10, y + h + 16), radius=20,
                         fill=(0, 0, 0, 120))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.paste(Image.new("RGB", img.size, (0, 0, 0)), (0, 0), shadow)
    d = ImageDraw.Draw(img)
    rr(d, (x, y, x + w, y + h), 18, fill=(8, 14, 25), outline=BORDER, width=2)
    # window chrome
    d.rounded_rectangle((x, y, x + w, y + 48), radius=18, fill=(15, 25, 42))
    d.rectangle((x, y + 30, x + w, y + 48), fill=(15, 25, 42))
    for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse((x + 20 + i * 22, y + 17, x + 30 + i * 22, y + 27), fill=color)
    text(d, (x + 92, y + 12), title, font("mono", 19), MUTED)
    text(d, (x + w - 22, y + 12), "—  □  ×", font("mono", 19), FAINT, anchor="ra")

    # Layout reveal schedule
    schedule = []
    clock = lead
    for kind, value in lines:
        cps = cps_cmd if kind == "cmd" else cps_out
        schedule.append((kind, value, clock, cps))
        clock += len(value) / cps
        clock += 0.22 if kind == "cmd" else 0.06
        if kind == "gap":
            clock += 0.25

    line_h = 33
    top = y + 72
    max_lines = int((h - 96) // line_h)
    visible = []
    for kind, value, start, cps in schedule:
        if elapsed < start:
            continue
        chars = min(len(value), max(0, int((elapsed - start) * cps)))
        shown = value[:chars]
        visible.append((kind, shown, start, chars < len(value)))
    visible = visible[-max_lines:]
    cy = top
    for kind, shown, start, typing in visible:
        if kind == "gap":
            cy += line_h
            continue
        if kind == "cmd":
            text(d, (x + 30, cy), "PS>", font("mono_bold", 24), TEAL)
            text(d, (x + 92, cy), shown, font("mono", 24), TEXT)
            if typing:
                cx = x + 92 + d.textlength(shown, font=font("mono", 24))
                d.rectangle((cx + 2, cy + 2, cx + 12, cy + 26), fill=SAFFRON)
        elif kind == "ok":
            text(d, (x + 30, cy), shown, font("mono", 22), GREEN)
        elif kind == "warn":
            text(d, (x + 30, cy), shown, font("mono", 22), AMBER)
        elif kind == "err":
            text(d, (x + 30, cy), shown, font("mono", 22), RED)
        else:
            text(d, (x + 30, cy), shown, font("mono", 22), MUTED)
        cy += line_h
    return cy


def chip(draw, x, y, label, fill=PANEL, outline=BORDER, color=TEXT, size=21, pad=18):
    fnt = font("semibold", size)
    tw = draw.textlength(label, font=fnt)
    box = (x, y, x + tw + pad * 2, y + size + pad)
    rr(draw, box, (size + pad) // 2, fill=fill, outline=outline, width=1)
    text(draw, (x + pad, y + pad / 2 - 1), label, fnt, color)
    return box[2]


def metric_card(draw, x, y, w, h, label, value, sub, color=TEXT):
    rr(draw, (x, y, x + w, y + h), 16, fill=PANEL, outline=BORDER, width=1)
    text(draw, (x + 26, y + 22), label.upper(), font("semibold", 17), MUTED)
    text(draw, (x + 26, y + 54), value, font("semibold", 52), color)
    text(draw, (x + 26, y + 116), sub, font("regular", 21), MUTED)


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

def scene_title(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)

    # Left copy
    text(d, (110, 250), "OPEN RESEARCH  ·  NSE INDIA", font("semibold", 22), SAFFRON)
    f1, f2 = font("semibold", 116), font("semibold", 116)
    text(d, (108, 300), "NSE", f1, SAFFRON)
    nse_w = d.textlength("NSE ", font=f1)
    text(d, (108 + nse_w, 300), "Alpha Lab", f2, TEXT)
    sentence(d, (112, 452),
             "Point-in-time, charge-aware momentum backtesting for NSE India.",
             font("regular", 38), MUTED, 880, 14)

    y = 620
    for i, (label, color) in enumerate((
            ("968 configurations published", SAFFRON),
            ("Null result stated first", TEAL),
            ("Apache-2.0", TEXT))):
        x = 112
        if i == 1:
            x = 500
        elif i == 2:
            x = 790
        chip(d, x, y, label, fill=(18, 29, 48), outline=BORDER, color=color)

    text(d, (112, 760), "A short tour  →", font("semibold", 26), TEXT)
    text(d, (112, 806), "what it does  ·  setup  ·  write an alpha  ·  test it",
         font("regular", 23), FAINT)

    # Right: schematic equity path (clearly labelled illustrative)
    panel = (1090, 260, 720, 500)
    x0, y0, w, h = panel
    rr(d, (x0, y0, x0 + w, y0 + h), 20, fill=(10, 18, 31), outline=BORDER, width=2)
    for i in range(1, 5):
        d.line((x0 + 30, y0 + i * h / 5, x0 + w - 30, y0 + i * h / 5),
               fill=(22, 35, 55), width=1)
    pts = [0.08, 0.11, 0.09, 0.15, 0.13, 0.20, 0.28, 0.24, 0.32, 0.29,
           0.38, 0.47, 0.43, 0.52, 0.61, 0.57, 0.64, 0.72, 0.68, 0.76,
           0.84, 0.80, 0.88, 0.94]
    reveal = smooth((t - 0.6) / 2.4)
    count = max(2, int(len(pts) * reveal))
    xy = [(x0 + 34 + i * (w - 68) / (len(pts) - 1),
           y0 + h - 40 - pts[i] * (h - 90)) for i in range(len(pts))]
    if count >= 2:
        # glow
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.line(xy[:count], fill=(255, 153, 51, 90), width=10, joint="curve")
        glow = glow.filter(ImageFilter.GaussianBlur(12))
        img.paste(Image.new("RGB", img.size, (0, 0, 0)), (0, 0), glow)
        d = ImageDraw.Draw(img)
        d.line(xy[:count], fill=SAFFRON, width=5, joint="curve")
        ex, ey = xy[count - 1]
        d.ellipse((ex - 7, ey - 7, ex + 7, ey + 7), fill=SAFFRON)
    text(d, (x0 + 28, y0 + 22), "schematic path  ·  not data",
         font("mono", 17), FAINT)
    text(d, (x0 + 28, y0 + h - 42), "one spec  ·  13 years  ·  every charge",
         font("semibold", 20), MUTED)

    chrome(img, d, "A short tour", 0, 9, min(1, t / dur))
    return fade(img, t, dur)


def scene_why(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Why this exists", "Optimistic by accident",
           "The lab makes the hidden choices explicit, in code.")

    panels = [
        (80, 300, 860, 470, "Typical backtest", RED, (38, 22, 30), [
            ("✕", "Today's index members, applied to 2013"),
            ("✕", "Fill at the close that produced the signal"),
            ("✕", "Charges mentioned in a footnote"),
        ]),
        (980, 300, 860, 470, "NSE Alpha Lab", TEAL, (16, 38, 40), [
            ("✓", "Point-in-time index membership"),
            ("✓", "Signal on close T, fill at open T+1"),
            ("✓", "Six charges on every buy and sell"),
        ]),
    ]
    for pi, (x, y, w, h, title, accent, fill, rows) in enumerate(panels):
        appear = smooth((t - 0.2 - pi * 0.18) / 0.55)
        if appear <= 0:
            continue
        rr(d, (x, y, x + w, y + h), 20, fill=fill, outline=mix(fill, accent, 0.45), width=2)
        text(d, (x + 34, y + 30), title.upper(), font("semibold", 24), accent)
        d.line((x + 34, y + 74, x + w - 34, y + 74), fill=mix(fill, accent, 0.35), width=1)
        for ri, (mark, value) in enumerate(rows):
            ra = smooth((t - 0.55 - pi * 0.18 - ri * 0.16) / 0.45)
            if ra <= 0:
                continue
            ry = y + 110 + ri * 105
            d.ellipse((x + 34, ry + 4, x + 66, ry + 36), fill=mix(fill, accent, 0.22))
            if mark == "✓":
                d.line([(x + 42, ry + 20), (x + 48, ry + 26), (x + 58, ry + 12)],
                       fill=accent, width=4, joint="curve")
            else:
                d.line([(x + 43, ry + 13), (x + 57, ry + 27)], fill=accent, width=4)
                d.line([(x + 57, ry + 13), (x + 43, ry + 27)], fill=accent, width=4)
            text(d, (x + 88, ry + 2), value, font("regular", 29), TEXT)

    # Causal timeline strip
    ty = 840
    appear = smooth((t - 1.6) / 0.6)
    if appear > 0:
        d.line((150, ty + 54, W - 150, ty + 54), fill=(32, 48, 72), width=3)
        for cx, label, sub, color in (
                (300, "close T", "signal is formed", SAFFRON),
                (1120, "open T+1", "order is filled", TEAL),
                (1660, "close T+1", "result is marked", MUTED)):
            d.ellipse((cx - 9, ty + 45, cx + 9, ty + 63), fill=color)
            text(d, (cx, ty + 4), label, font("semibold", 25), color, anchor="ma")
            text(d, (cx, ty + 78), sub, font("regular", 19), MUTED, anchor="ma")
        # moving arrow
        start, end = 330, 1085
        pos = int(lerp(start, end, smooth((t - 2.0) / 2.2)))
        if pos > start:
            d.line((start, ty + 54, pos, ty + 54), fill=SAFFRON, width=4)
            d.polygon([(pos, ty + 44), (pos + 16, ty + 54), (pos, ty + 64)], fill=SAFFRON)
        text(d, (W / 2, 970),
             "No look-ahead. No invented prices. No invented corporate actions.",
             font("semibold", 25), TEXT, anchor="ma")

    chrome(img, d, "The defaults that flatter a backtest", 1, 9, (1 + t / dur) / 9)
    return fade(img, t, dur)


def scene_pipeline(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "How it works", "One spec in, one auditable result out")

    box = (60, 270, 1800, 660)
    rr(d, (box[0] - 12, box[1] - 12, box[0] + box[2] + 12, box[1] + box[3] + 12),
       24, fill=(9, 16, 28), outline=BORDER, width=2)
    zoom = 1.0 + 0.018 * smooth(t / dur)
    if not paste_cover(img, IMG_DIR / "architecture.png", box, radius=16, zoom=zoom):
        # Fallback: a clean 4x2 pipeline
        stages = [
            ("01", "DATA PACK", "checksummed, versioned"),
            ("02", "POINT-IN-TIME", "membership as of T"),
            ("03", "ADJUSTED BARS", "splits + bonuses"),
            ("04", "MOMENTUM RANK", "close T / close T-63"),
            ("05", "NEXT-OPEN FILL", "open of T+1 only"),
            ("06", "SIX CHARGES", "buy and sell, itemised"),
            ("07", "METRICS + CHECKS", "alpha, t-stats, 348 checks"),
            ("08", "PUBLISHED", "968 rows + Streamlit UI"),
        ]
        for i, (num, name, sub) in enumerate(stages):
            col, row = i % 4, i // 4
            x = box[0] + 34 + col * 440
            y = box[1] + 50 + row * 290
            appear = smooth((t - 0.25 - i * 0.10) / 0.45)
            if appear <= 0:
                continue
            rr(d, (x, y, x + 390, y + 220), 16, fill=PANEL, outline=BORDER, width=2)
            text(d, (x + 24, y + 20), num, font("mono_bold", 22), SAFFRON)
            text(d, (x + 24, y + 62), name, font("semibold", 27), TEXT)
            text(d, (x + 24, y + 104), sub, font("regular", 20), MUTED)
            if col < 3:
                d.line((x + 396, y + 110, x + 430, y + 110), fill=SAFFRON, width=3)
                d.polygon([(x + 430, y + 102), (x + 444, y + 110), (x + 430, y + 118)],
                          fill=SAFFRON)

    text(d, (80, 955),
         "The engine is pure: it performs no I/O and reads no clock. Two runs over identical inputs are byte-identical.",
         font("regular", 24), MUTED)
    chrome(img, d, "One spec in, one auditable result out", 2, 9, (2 + t / dur) / 9)
    return fade(img, t, dur)


def scene_setup(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Setup", "Code and tests first. Data second.")

    lines = [
        ("cmd", "git clone https://github.com/Kaustubh-Rathi/nse-alpha-lab.git"),
        ("out", "Cloning into 'nse-alpha-lab'..."),
        ("cmd", "cd nse-alpha-lab"),
        ("cmd", "uv sync"),
        ("gap", ""),
        ("cmd", "uv run pytest -q tests/test_backtest_momentum.py \\"),
        ("cmd", "  tests/test_leakage_causality.py tests/test_backtest_exits.py"),
        ("ok", "84 passed, 1 xfailed in 5.68s"),
        ("gap", ""),
        ("cmd", "uv sync --extra lab"),
        ("out", "Installed streamlit ..."),
        ("cmd", "uv run streamlit run apps/lab/app.py"),
        ("out", "Local URL: http://localhost:8501"),
    ]
    terminal_frame(img, (70, 290, 1100, 620), lines, t, lead=0.15)

    # Right: real UI screenshot if captured, else an honest page map.
    x, y, w, h = 1215, 290, 635, 620
    if not paste_cover(img, UI_DIR / "home.png", (x, y, w, h), radius=16):
        rr(d, (x, y, x + w, y + h), 18, fill=PANEL_2, outline=BORDER, width=2)
        d.rounded_rectangle((x, y, x + w, y + 46), radius=18, fill=(15, 25, 42))
        d.rectangle((x, y + 28, x + w, y + 46), fill=(15, 25, 42))
        for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            d.ellipse((x + 18 + i * 20, y + 16, x + 27 + i * 20, y + 25), fill=c)
        text(d, (x + 84, y + 11), "Streamlit  ·  research UI", font("mono", 17), MUTED)
        text(d, (x + 30, y + 80), "Home", font("semibold", 26), SAFFRON)
        for i, page in enumerate(("Data", "Create", "Backtest", "Results",
                                  "Compare", "Docs")):
            text(d, (x + 30, y + 140 + i * 56), page, font("regular", 27), TEXT)
            d.line((x + 30, y + 178 + i * 56, x + w - 30, y + 178 + i * 56),
                   fill=(26, 40, 60), width=1)
        text(d, (x + 30, y + h - 62), "Run with: uv sync --extra lab",
             font("mono", 17), FAINT)
    else:
        d.rounded_rectangle((x - 6, y - 6, x + w + 6, y + h + 6), radius=20,
                            outline=BORDER, width=2)

    text(d, (80, 950), "No data pack is needed to install, import the package, or run the test suite.",
         font("regular", 24), MUTED)
    text(d, (80, 986), "The data pack is a separate, checksummed download. Backtests never touch the network.",
         font("regular", 24), FAINT)
    chrome(img, d, "Setup: two commands to a green test run", 3, 9, (3 + t / dur) / 9)
    return fade(img, t, dur)


SPEC_LINES = [
    "{",
    '  "name": "primary",',
    '  "universe": "nifty500",',
    '  "benchmark": "nifty50",',
    '  "top_n": 10,',
    '  "direction": "top",',
    '  "lookback_sessions": 63,',
    '  "cadence": "monthly",',
    '  "stop_loss_pct": null,',
    '  "take_profit_pct": null,',
    '  "cost_model": "full_charges",',
    '  "start": "2013-01-31",',
    '  "end": "2026-09-30"',
    "}",
]

CALLOUTS = [
    ("universe", "Label only in the CLI; the study applies membership."),
    ("top_n", "Equal-weight holdings; integer shares only."),
    ("direction", "top = momentum; bottom = sign test."),
    ("lookback_sessions", "63 sessions ≈ three months."),
    ("cadence", "Monthly rebalance; signal dates are explicit."),
    ("stop_loss_pct", "Causal exit overlay; null = none."),
    ("cost_model", "full_charges, or a zero-cost counterfactual."),
]


def draw_json_line(d, x, y, line, fnt):
    m = re.match(r'^(\s*)"([^"]+)"(\s*:\s*)(.*)$', line)
    if m:
        indent, key, sep, value = m.groups()
        cx = x + d.textlength(indent, font=fnt)
        text(d, (cx, y), f'"{key}"', fnt, TEAL)
        cx += d.textlength(f'"{key}"', font=fnt)
        text(d, (cx, y), sep, fnt, MUTED)
        cx += d.textlength(sep, font=fnt)
        if value.startswith('"'):
            color = SAFFRON
        elif value.startswith("null"):
            color = RED
        elif re.match(r"^-?\d", value):
            color = GREEN
        else:
            color = TEXT
        text(d, (cx, y), value, fnt, color)
    else:
        text(d, (x, y), line, fnt, MUTED)


def scene_spec(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Write your first alpha", "A JSON specification, not a class")

    # code panel
    x, y, w, h = 70, 280, 1010, 620
    rr(d, (x, y, x + w, y + h), 18, fill=(8, 14, 25), outline=BORDER, width=2)
    d.rounded_rectangle((x, y, x + w, y + 46), radius=18, fill=(15, 25, 42))
    d.rectangle((x, y + 28, x + w, y + 46), fill=(15, 25, 42))
    text(d, (x + 20, y + 11), "examples/specs/primary.json", font("mono", 18), MUTED)
    text(d, (x + w - 18, y + 11), "JSON", font("mono", 17), FAINT, anchor="ra")

    line_h = 38
    fnt = font("mono", 27)
    for i, line in enumerate(SPEC_LINES):
        appear = smooth((t - 0.25 - i * 0.055) / 0.35)
        if appear <= 0:
            continue
        ly = y + 66 + i * line_h
        draw_json_line(d, x + 30, ly, line, fnt)
        if line.strip().startswith('"top_n"') and t > 1.4:
            d.rectangle((x + 18, ly - 3, x + 430, ly + 31),
                        outline=SAFFRON, width=2)

    # callouts
    cx = 1130
    for i, (key, explain) in enumerate(CALLOUTS):
        appear = smooth((t - 0.9 - i * 0.20) / 0.45)
        if appear <= 0:
            continue
        yy = 300 + i * 88
        d.ellipse((cx, yy + 6, cx + 12, yy + 18), fill=SAFFRON)
        text(d, (cx + 26, yy), key, font("mono_bold", 24), TEXT)
        text(d, (cx + 26, yy + 32), explain, font("regular", 21), MUTED)

    text(d, (70, 932),
         "Unknown fields are an error, never ignored. A typo can never silently change the result.",
         font("semibold", 24), SAFFRON)
    text(d, (70, 968),
         "CLI scope: engine over pack bars. The research harness applies point-in-time membership.",
         font("regular", 20), MUTED)
    chrome(img, d, "An alpha is a specification, not a class", 4, 9, (4 + t / dur) / 9)
    return fade(img, t, dur)


def scene_run(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Run it", "One command. Every trade is auditable.")

    lines = [
        ("cmd", "$env:PYTHONPATH='src'"),
        ("cmd", "uv run python -m nse_pipeline backtest \\"),
        ("cmd", "  examples/synthetic_demo/hello_alpha.json \\"),
        ("cmd", "  --pack examples/synthetic_demo/pack \\"),
        ("cmd", "  --out examples/synthetic_demo/result.json"),
        ("out", "Backtest: hello_alpha"),
        ("out", "  universe           synthetic_demo"),
        ("out", "  top_n              10"),
        ("out", "  lookback_sessions  63"),
        ("out", "  cadence            monthly"),
        ("out", "  cost_model         full_charges"),
        ("gap", ""),
        ("ok", "Ran 1304 sessions, 60 rebalances."),
        ("out", "  trades            533"),
        ("out", "  exits             0"),
        ("out", "  skips             16"),
        ("out", "  final equity      8886353.0450245146"),
        ("warn", "  total return      -11.1365%"),
        ("out", "  written to        examples/.../result.json"),
    ]
    terminal_frame(img, (60, 285, 1160, 650), lines, t, lead=0.1)

    # right: synthetic warning + result JSON
    x, y, w, h = 1260, 285, 600, 650
    rr(d, (x, y, x + w, y + h), 18, fill=(28, 20, 18), outline=(120, 70, 20), width=2)
    text(d, (x + 26, y + 24), "SYNTHETIC DEMO", font("semibold", 24), AMBER)
    text(d, (x + 26, y + 60), "NOT A RESULT · DO NOT CITE", font("semibold", 19), RED)
    d.line((x + 26, y + 94, x + w - 26, y + 94), fill=(90, 55, 20), width=1)

    json_lines = [
        ("{", MUTED),
        ('  "summary": {', TEXT),
        ('    "sessions": 1304,', TEXT),
        ('    "rebalances": 60,', TEXT),
        ('    "trades": 533,', TEXT),
        ('    "skips": 16,', TEXT),
        ('    "total_return_pct": -11.1365', AMBER),
        ("  },", TEXT),
        ('  "result": {', TEXT),
        ('    "equity": [...],', MUTED),
        ('    "trades": [...],', MUTED),
        ('    "exits": [...],', MUTED),
        ('    "skips": [...]', MUTED),
        ("  }", TEXT),
        ("}", MUTED),
    ]
    for i, (line, color) in enumerate(json_lines):
        appear = smooth((t - 1.2 - i * 0.06) / 0.35)
        if appear <= 0:
            continue
        text(d, (x + 26, y + 120 + i * 31), line, font("mono", 20), color)
    text(d, (x + 26, y + h - 58), "Synthetic bars generated by a seeded RNG.",
         font("regular", 18), MUTED)

    text(d, (60, 960),
         "The real pack is checksummed and not in git. This run only proves the mechanics.",
         font("regular", 23), MUTED)
    chrome(img, d, "Run one spec, audit every trade", 5, 9, (5 + t / dur) / 9)
    return fade(img, t, dur)


def scene_test(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Test it", "Causality, accounting, and published numbers")

    cards = [
        ("LEVEL 1", "Targeted tests", "84 passed", "1 xfailed", "engine + causality", GREEN),
        ("LEVEL 2", "Full suite", "1,952 passed", "68 skipped", "offline, no data pack", TEAL),
        ("LEVEL 3", "Artifact validation", "348 / 348", "0 failed", "independent recomputation", SAFFRON),
    ]
    for i, (level, title, big, small, sub, color) in enumerate(cards):
        appear = smooth((t - 0.3 - i * 0.22) / 0.5)
        if appear <= 0:
            continue
        x = 80 + i * 600
        y = 300
        rr(d, (x, y, x + 550, y + 260), 18, fill=PANEL, outline=BORDER, width=2)
        text(d, (x + 26, y + 22), level, font("mono_bold", 17), color)
        text(d, (x + 26, y + 56), title, font("semibold", 30), TEXT)
        text(d, (x + 26, y + 116), big, font("semibold", 46), color)
        text(d, (x + 300, y + 132), small, font("regular", 20), MUTED)
        text(d, (x + 26, y + 196), sub, font("regular", 20), MUTED)

    # real pytest line
    x, y, w, h = 80, 630, 1050, 250
    rr(d, (x, y, x + w, y + h), 16, fill=(8, 14, 25), outline=BORDER, width=2)
    text(d, (x + 24, y + 18), "PowerShell  ·  $ uv run pytest -q", font("mono", 18), MUTED)
    py_lines = [
        ("PS>", "uv run pytest -q tests/test_backtest_momentum.py \\", TEXT),
        ("", "      tests/test_leakage_causality.py tests/test_backtest_exits.py", TEXT),
        ("", "................................................x.......................", FAINT),
        ("", "84 passed, 1 xfailed in 5.68s", GREEN),
    ]
    for i, (prompt, line, color) in enumerate(py_lines):
        text(d, (x + 24, y + 62 + i * 40), prompt, font("mono_bold", 22), TEAL)
        text(d, (x + 92, y + 62 + i * 40), line, font("mono", 22), color)

    # real assertions
    x2, y2, w2, h2 = 1180, 630, 660, 250
    rr(d, (x2, y2, x2 + w2, y2 + h2), 16, fill=PANEL_2, outline=BORDER, width=2)
    text(d, (x2 + 24, y2 + 18), "assertions that matter", font("semibold", 19), MUTED)
    asserts = [
        "assert sum(parts.values(), D('0')) == config.cost_for(...)",
        "assert signal.executes_next_open is True",
        "def test_future_perturbation_invariance():",
    ]
    for i, a in enumerate(asserts):
        color = GREEN if a.startswith("assert") else SAFFRON
        text(d, (x2 + 24, y2 + 62 + i * 52), a, font("mono", 17), color)

    text(d, (80, 960),
         "The published record (research/validation.json) is 348 checks, 0 failures. A failed check is never rounded away.",
         font("regular", 22), MUTED)
    chrome(img, d, "Three levels of testing", 6, 9, (6 + t / dur) / 9)
    return fade(img, t, dur)


def scene_results(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    header(d, "Read the result honestly", "The headline is a null result")

    # Phase A: scorecard
    if t < 15.0:
        a = smooth(t / 0.6) * smooth((15.0 - t) / 0.6)
        box = (120, 290, 1680, 650)
        if paste_contain(img, IMG_DIR / "headline-scorecard.png", box, radius=18):
            pass
        else:
            rr(d, (box[0], box[1], box[0] + box[2], box[1] + box[3]), 18,
               fill=PANEL, outline=BORDER, width=2)
            text(d, (box[0] + 40, box[1] + 40), "Pre-specified primary", font("semibold", 26), MUTED)
            for i, (label, value) in enumerate((
                    ("Total return", "907.97%"), ("CAGR", "19.30%"),
                    ("Sharpe", "0.778"), ("Max drawdown", "−55.30%"))):
                text(d, (box[0] + 40 + i * 400, box[1] + 100), label, font("regular", 20), MUTED)
                text(d, (box[0] + 40 + i * 400, box[1] + 140), value, font("semibold", 46), TEXT)
            text(d, (box[0] + 40, box[1] + 260), "Alpha t = 1.62 (Newey-West)   ·   bootstrap p = 0.096",
                 font("semibold", 30), RED)
            text(d, (box[0] + 40, box[1] + 320), "NOT STATISTICALLY SIGNIFICANT AT 5%",
                 font("semibold", 38), RED)

    # Phase B: sweep chart
    if t >= 14.2:
        box = (80, 280, 1760, 690)
        if paste_contain(img, IMG_DIR / "sweep-distribution.png", box, radius=18):
            pass
        else:
            rr(d, (box[0], box[1], box[0] + box[2], box[1] + box[3]), 18,
               fill=PANEL, outline=BORDER, width=2)
            text(d, (box[0] + 40, box[1] + 40), "968 configurations", font("semibold", 30), TEXT)
            text(d, (box[0] + 40, box[1] + 100), "The top of the table is the maximum of 968 trials.",
                 font("regular", 26), MUTED)

    # callout chips
    if t >= 18.0:
        for i, (label, color) in enumerate((
                ("median: −9.73 pp/yr vs NIFTY 50", AMBER),
                ("26 / 968 clear t = 1.96", RED),
                ("~48 expected by chance", MUTED),
                ("max Sharpe 1.3362 · 4.66 yrs", SAFFRON))):
            chip(d, 90 + i * 440, 920, label, fill=(18, 29, 48), outline=BORDER, color=color, size=19)

    text(d, (80, 970),
         "No multiple-testing correction is applied anywhere in this repository.",
         font("regular", 22), FAINT)
    chrome(img, d, "The finding is a null result", 7, 9, (7 + min(t, dur) / dur) / 9)
    return fade(img, t, dur)


def qr_image(url: str, size: int) -> Image.Image | None:
    try:
        import qrcode  # type: ignore
    except Exception:
        return None
    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=(230, 237, 247), back_color=(11, 18, 32)).convert("RGB")
    return img.resize((size, size), Image.NEAREST)


def scene_outro(t, dur):
    img = BACKGROUND.copy()
    d = ImageDraw.Draw(img)
    text(d, (110, 250), "READ IT  ·  RUN IT  ·  ATTACK IT", font("semibold", 22), SAFFRON)
    text(d, (108, 300), "NSE Alpha Lab", font("semibold", 92), TEXT)
    text(d, (112, 420), "github.com/Kaustubh-Rathi/nse-alpha-lab",
         font("mono", 32), TEAL)

    cards = [
        ("README + tutorial", "docs/HELLO_ALPHA.md"),
        ("The published study", "research/REPORT.md"),
        ("What is not validated", "STATUS.md"),
        ("Not investment advice", "DISCLAIMER.md"),
    ]
    for i, (title, path) in enumerate(cards):
        x = 110 + (i % 2) * 560
        y = 560 + (i // 2) * 150
        appear = smooth((t - 0.5 - i * 0.12) / 0.45)
        if appear <= 0:
            continue
        rr(d, (x, y, x + 520, y + 118), 14, fill=PANEL, outline=BORDER, width=1)
        text(d, (x + 24, y + 20), title, font("semibold", 25), TEXT)
        text(d, (x + 24, y + 62), path, font("mono", 20), MUTED)

    qr = qr_image("https://github.com/Kaustubh-Rathi/nse-alpha-lab", 260)
    if qr is not None:
        qx, qy = 1510, 560
        rr(d, (qx - 18, qy - 18, qx + 278, qy + 278), 16, fill=TEXT)
        img.paste(qr, (qx, qy))
        text(d, (qx + 130, qy + 300), "Scan to open", font("regular", 19), MUTED, anchor="ma")

    text(d, (110, 930), "Apache-2.0  ·  research preview  ·  not investment advice",
         font("regular", 22), FAINT)
    chrome(img, d, "Research preview. Not investment advice.", 8, 9, (8 + t / dur) / 9)
    return fade(img, t, dur)


SCENES = {
    "00_title": scene_title,
    "01_why": scene_why,
    "02_pipeline": scene_pipeline,
    "03_setup": scene_setup,
    "04_spec": scene_spec,
    "05_run": scene_run,
    "06_test": scene_test,
    "07_results": scene_results,
    "08_outro": scene_outro,
}


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def render_scene(scene_id, audio_path, duration, out_path):
    fn = SCENES[scene_id]
    frames = int(round(duration * FPS))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "-",
        "-i", str(audio_path),
        "-filter_complex", "[1:a]adelay=450|450,apad[a]",
        "-map", "0:v", "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for i in range(frames):
        t = i / FPS
        frame = fn(t, duration)
        proc.stdin.write(frame.tobytes())
        if i % 300 == 0:
            print(f"  {scene_id}: {i}/{frames}", flush=True)
    proc.stdin.close()
    code = proc.wait()
    if code != 0:
        raise SystemExit(f"ffmpeg failed for {scene_id} (exit {code})")


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg is not on PATH", file=sys.stderr)
        return 2
    spec = json.loads((VIDEO_DIR / "narration.json").read_text(encoding="utf-8"))
    durations = json.loads((VIDEO_DIR / "durations.json").read_text(encoding="utf-8-sig"))

    scenes = []
    for scene in spec["scenes"]:
        sid = scene["id"]
        audio = VIDEO_DIR / "audio" / f"{sid}.wav"
        if not audio.is_file():
            print(f"missing audio: {audio}", file=sys.stderr)
            return 2
        lead = 0.8 if sid == "00_title" else 0.45
        tail = 0.9 if sid == "00_title" else 0.55
        dur = float(durations[sid]) + lead + tail
        scenes.append((sid, audio, dur, scene))

    total = sum(d for _, _, d, _ in scenes)
    print(f"total duration: {total:.1f}s ({total / 60:.2f} min)")

    tmp = Path(tempfile.mkdtemp(prefix="nse-video-"))
    parts = []
    starts = []
    clock = 0.0
    for sid, audio, dur, scene in scenes:
        starts.append((clock, sid, scene))
        out = tmp / f"{sid}.mp4"
        print(f"rendering {sid} ({dur:.1f}s)")
        render_scene(sid, audio, dur, out)
        parts.append(out)
        clock += dur

    concat = tmp / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    print("concatenating")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(concat),
         "-c", "copy", "-movflags", "+faststart", str(OUT_MP4)],
        check=True,
    )

    # Poster
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "1.6", "-i", str(OUT_MP4), "-frames:v", "1", str(OUT_POSTER)],
        check=True,
    )

    # README GIF preview: five short highlights, not just the title card.
    print("building GIF preview")
    segments = ((1.5, 2.4), (43.5, 2.4), (94.0, 2.4), (155.5, 2.4), (184.5, 2.4))
    clips = []
    for i, (start, length) in enumerate(segments):
        clip = tmp / f"gif_{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", str(start), "-t", str(length), "-i", str(OUT_MP4),
             "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             "-pix_fmt", "yuv420p", str(clip)],
            check=True,
        )
        clips.append(clip)
    gif_concat = tmp / "gif_concat.txt"
    gif_concat.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8"
    )
    palette = tmp / "palette.png"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(gif_concat),
         "-vf", "fps=12,scale=960:-1:flags=lanczos,palettegen", str(palette)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(gif_concat), "-i", str(palette),
         "-lavfi", "fps=12,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse",
         "-loop", "0", str(OUT_GIF)],
        check=True,
    )

    # Captions
    lines = []
    for i, (start, sid, scene) in enumerate(starts, 1):
        end = start + scenes[i - 1][2]
        def ts(value):
            h = int(value // 3600)
            m = int((value % 3600) // 60)
            s = int(value % 60)
            ms = int(round((value - int(value)) * 1000))
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        subtitle = scene["text"]
        for src, dst in (("N S E", "NSE"), ("U V", "uv"), ("S T T", "STT"),
                         ("G S T", "GST"), ("J S O N", "JSON"), ("C S V", "CSV"),
                         ("T plus one", "T+1")):
            subtitle = subtitle.replace(src, dst)
        lines.append(f"{i}\n{ts(start + 0.3)} --> {ts(end - 0.2)}\n{subtitle}\n")
    OUT_SRT.write_text("\n".join(lines), encoding="utf-8")

    size_mb = OUT_MP4.stat().st_size / (1024 * 1024)
    print(f"wrote {OUT_MP4} ({size_mb:.1f} MB)")
    print(f"wrote {OUT_GIF} ({OUT_GIF.stat().st_size / (1024 * 1024):.1f} MB)")
    print(f"wrote {OUT_POSTER} and {OUT_SRT}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
