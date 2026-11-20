#!/usr/bin/env python3
"""Build the static GitHub image set for NSE Alpha Lab.

Every image is drawn at 2x and downsampled with LANCZOS. Charts are rendered
with matplotlib into the same dark-navy panels; all other geometry is Pillow.

Run from anywhere:
    python docs/assets/build_static_assets.py

Inputs (read-only):
    research/FINAL_RESULTS.csv
    examples/specs/primary.json

Outputs:
    docs/assets/images/hero.png
    docs/assets/images/architecture.png
    docs/assets/images/quickstart.png
    docs/assets/images/alpha-anatomy.png
    docs/assets/images/sweep-distribution.png
    docs/assets/images/headline-scorecard.png
    docs/assets/images/social-preview.png
    docs/assets/images/data-pack.png
"""

from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


# ---------------------------------------------------------------------------
# Paths, palette, type
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent / "images"
RESULTS_CSV = ROOT / "research" / "FINAL_RESULTS.csv"
SPEC_JSON = ROOT / "examples" / "specs" / "primary.json"

S = 2  # render scale

BG = "#0B1220"
PANEL = "#111C2E"
PANEL_DEEP = "#0A1424"
PANEL_DARK = "#0E1828"
BORDER = "#22324D"
BORDER_SOFT = "#1B2A42"
TEXT = "#E6EDF7"
MUTED = "#8FA3BF"
ACCENT = "#FF9933"
TEAL = "#2DD4BF"
GREEN = "#34D399"
RED = "#F87171"
DIM = "#5A7396"

FOOTER_TEXT = "NSE Alpha Lab · research preview · not investment advice"
REPO_URL = "github.com/Kaustubh-Rathi/nse-alpha-lab"
PITCH = "Point-in-time, charge-aware momentum backtesting for NSE India"

FONT_DIR = Path(r"C:\Windows\Fonts")
FALLBACK_SANS = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans"))
FALLBACK_MONO = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans Mono"))


def _pick_font(names: list[str], fallback: str) -> str:
    for name in names:
        candidate = FONT_DIR / name
        if candidate.exists():
            return str(candidate)
    return fallback


FONT_PATHS = {
    "regular": _pick_font(["segoeui.ttf"], FALLBACK_SANS),
    "semibold": _pick_font(["seguisb.ttf", "segoeuib.ttf"], FALLBACK_SANS),
    "bold": _pick_font(["segoeuib.ttf", "seguisb.ttf"], FALLBACK_SANS),
    "mono": _pick_font(["consola.ttf"], FALLBACK_MONO),
    "mono_bold": _pick_font(["consolab.ttf", "consola.ttf"], FALLBACK_MONO),
}

for _font_path in FONT_PATHS.values():
    try:
        font_manager.fontManager.addfont(_font_path)
    except Exception:
        pass

_SANS_NAME = font_manager.FontProperties(fname=FONT_PATHS["regular"]).get_name()
_MONO_NAME = font_manager.FontProperties(fname=FONT_PATHS["mono"]).get_name()
plt.rcParams.update(
    {
        "font.family": _SANS_NAME,
        "font.monospace": _MONO_NAME,
        "text.color": TEXT,
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.edgecolor": BORDER,
        "axes.linewidth": 0.8,
        "axes.titlecolor": TEXT,
        "axes.unicode_minus": True,
        "figure.facecolor": PANEL,
        "savefig.facecolor": PANEL,
    }
)

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def F(kind: str, size: float) -> ImageFont.FreeTypeFont:
    key = (kind, int(round(size * S)))
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(FONT_PATHS[kind], key[1])
    return _FONT_CACHE[key]


def tw(font: ImageFont.FreeTypeFont, text: str) -> float:
    """Text width in logical pixels."""
    return font.getlength(text) / S


def sx(value: float) -> float:
    return value * S


def sbox(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(value * S for value in box)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def new_canvas(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGBA", (w * S, h * S), BG)
    return image, ImageDraw.Draw(image)


def finish(image: Image.Image, w: int, h: int) -> Image.Image:
    return image.convert("RGB").resize((w, h), Image.LANCZOS)


def text(
    d: ImageDraw.ImageDraw,
    x: float,
    y: float,
    value: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    anchor: str = "la",
) -> None:
    d.text((sx(x), sx(y)), value, font=font, fill=fill, anchor=anchor)


def paragraph(
    d: ImageDraw.ImageDraw,
    x: float,
    y: float,
    value: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    max_width: float,
    line_height: float,
    anchor: str = "la",
) -> float:
    for line in wrap(value, font, max_width):
        d.text((sx(x), sx(y)), line, font=font, fill=fill, anchor=anchor)
        y += line_height
    return y


def wrap(value: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    lines: list[str] = []
    for raw in value.split("\n"):
        words = raw.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = current + " " + word
            if tw(font, trial) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def tracked(
    d: ImageDraw.ImageDraw,
    x: float,
    y: float,
    value: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    tracking: float = 0.0,
    anchor: str = "la",
) -> float:
    """Letter-spaced text. Returns the advance width."""
    widths = [font.getlength(ch) / S for ch in value]
    total = sum(widths) + tracking * max(0, len(value) - 1)
    if anchor.startswith("m"):
        x -= total / 2
    elif anchor.startswith("r"):
        x -= total
    for ch, width in zip(value, widths):
        d.text((sx(x), sx(y)), ch, font=font, fill=fill, anchor="l" + anchor[1:])
        x += width + tracking
    return total


def tracked_runs(
    d: ImageDraw.ImageDraw,
    x: float,
    y: float,
    runs: list[tuple[str, str]],
    font: ImageFont.FreeTypeFont,
    tracking: float = 0.0,
) -> float:
    start = x
    for value, fill in runs:
        for ch in value:
            d.text((sx(x), sx(y)), ch, font=font, fill=fill, anchor="la")
            x += font.getlength(ch) / S + tracking
    return x - start - tracking


def panel(
    d: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    fill: str = PANEL,
    outline: str = BORDER,
    radius: float = 14,
    width: float = 1,
) -> None:
    d.rounded_rectangle(sbox(box), radius=sx(radius), fill=fill, outline=outline, width=max(1, int(width * S)))


def rule(
    d: ImageDraw.ImageDraw,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    fill: str = BORDER,
    width: float = 1,
) -> None:
    d.line([(sx(x1), sx(y1)), (sx(x2), sx(y2))], fill=fill, width=max(1, int(width * S)))


def dot_grid(image: Image.Image, spacing: float = 28, color: str = "#16233A", radius: float = 1.05) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    step = spacing * S
    r = radius * S
    for x in range(int(step / 2), image.width, int(step)):
        for y in range(int(step / 2), image.height, int(step)):
            d.ellipse((x - r, y - r, x + r, y + r), fill=color)
    image.alpha_composite(overlay)


def chip(
    d: ImageDraw.ImageDraw,
    x: float,
    y: float,
    label: str,
    dot_color: str,
    h: float = 44,
    font_size: float = 16,
    max_width: float | None = None,
) -> float:
    font = F("regular", font_size)
    width = tw(font, label) + 52
    if max_width is not None:
        width = min(width, max_width)
    panel(d, (x, y, x + width, y + h), fill=PANEL, outline=BORDER, radius=h / 2)
    d.ellipse(sbox((x + 18, y + h / 2 - 3.5, x + 25, y + h / 2 + 3.5)), fill=dot_color)
    text(d, x + 36, y + h / 2, label, font, TEXT, anchor="lm")
    return width


def badge(d: ImageDraw.ImageDraw, cx: float, cy: float, value: str, color: str = ACCENT, radius: float = 21) -> None:
    d.ellipse(
        sbox((cx - radius, cy - radius, cx + radius, cy + radius)),
        fill="#15233A",
        outline=color,
        width=max(1, int(1 * S)),
    )
    text(d, cx, cy + 1, value, F("semibold", 15), color, anchor="mm")


def arrowhead(d: ImageDraw.ImageDraw, tip: tuple[float, float], angle: float, size: float, color: str) -> None:
    x, y = tip
    left = (x - size * math.cos(angle - 0.45), y - size * math.sin(angle - 0.45))
    right = (x - size * math.cos(angle + 0.45), y - size * math.sin(angle + 0.45))
    d.polygon([(sx(x), sx(y)), (sx(left[0]), sx(left[1])), (sx(right[0]), sx(right[1]))], fill=color)


def arrow(
    d: ImageDraw.ImageDraw,
    p1: tuple[float, float],
    p2: tuple[float, float],
    color: str = DIM,
    width: float = 1.6,
    head: float = 7,
) -> None:
    angle = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
    end = (p2[0] - head * 0.85 * math.cos(angle), p2[1] - head * 0.85 * math.sin(angle))
    d.line([(sx(p1[0]), sx(p1[1])), (sx(end[0]), sx(end[1]))], fill=color, width=max(1, int(width * S)))
    arrowhead(d, p2, angle, head, color)


def poly_arrow(
    d: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    color: str = DIM,
    width: float = 1.6,
    head: float = 7,
) -> None:
    scaled = [(sx(x), sx(y)) for x, y in points]
    d.line(scaled, fill=color, width=max(1, int(width * S)), joint="curve")
    p1, p2 = points[-2], points[-1]
    arrowhead(d, p2, math.atan2(p2[1] - p1[1], p2[0] - p1[0]), head, color)


def footer(d: ImageDraw.ImageDraw, w: float, h: float, margin: float = 70, font_size: float = 12) -> None:
    rule(d, margin, h - 46, w - margin, h - 46, BORDER)
    text(d, margin, h - 35, FOOTER_TEXT, F("regular", font_size), MUTED)
    text(d, w - margin, h - 34, REPO_URL, F("mono", font_size - 1), MUTED, anchor="ra")


def checkmark(d: ImageDraw.ImageDraw, x: float, y: float, size: float, color: str = GREEN) -> None:
    d.ellipse(sbox((x, y, x + size, y + size)), fill="#10291F", outline=color, width=max(1, int(1 * S)))
    d.line(
        [
            (sx(x + size * 0.26), sx(y + size * 0.52)),
            (sx(x + size * 0.44), sx(y + size * 0.69)),
            (sx(x + size * 0.75), sx(y + size * 0.33)),
        ],
        fill=color,
        width=max(1, int(2 * S)),
        joint="curve",
    )


def title_block(
    d: ImageDraw.ImageDraw,
    kicker: str,
    title: str,
    subtitle: str | None = None,
    x: float = 80,
    y: float = 54,
    kicker_color: str = TEAL,
    title_size: float = 40,
    subtitle_size: float = 16.5,
    max_width: float = 1200,
) -> float:
    tracked(d, x, y, kicker, F("semibold", 12.5), kicker_color, tracking=2.4)
    ty = y + 30
    text(d, x, ty, title, F("semibold", title_size), TEXT)
    cy = ty + title_size + 14
    if subtitle:
        cy = paragraph(d, x, cy, subtitle, F("regular", subtitle_size), MUTED, max_width, subtitle_size * 1.45)
    return cy


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_results() -> list[dict[str, str]]:
    with RESULTS_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 968, f"expected 968 sweep rows, found {len(rows)}"
    return rows


def load_spec() -> dict:
    return json.loads(SPEC_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# matplotlib panels
# ---------------------------------------------------------------------------


def _style_axes(ax) -> None:
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=9, length=3, width=0.6, pad=3)
    ax.grid(True, color=BORDER, alpha=0.65, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_size(10)
    ax.yaxis.label.set_size(10)


def chart(fig_w: float, fig_h: float, draw_fn) -> Image.Image:
    fig = plt.figure(figsize=(fig_w / 100, fig_h / 100), dpi=200, facecolor=PANEL)
    draw_fn(fig)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, facecolor=PANEL, edgecolor=PANEL)
    plt.close(fig)
    buffer.seek(0)
    image = Image.open(buffer).convert("RGB")
    target = (int(round(fig_w * S)), int(round(fig_h * S)))
    if image.size != target:
        image = image.resize(target, Image.LANCZOS)
    return image


# ---------------------------------------------------------------------------
# 1. hero.png
# ---------------------------------------------------------------------------


def build_hero() -> Image.Image:
    W, H = 1600, 620
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=30, color="#152238", radius=1.0)

    # Faint illustrative equity path on the right. No values, no scale.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    anchors = np.array(
        [
            [950, 470],
            [990, 452],
            [1035, 464],
            [1080, 425],
            [1125, 408],
            [1170, 343],
            [1210, 296],
            [1245, 254],
            [1275, 288],
            [1305, 354],
            [1340, 414],
            [1375, 421],
            [1410, 383],
            [1450, 349],
            [1490, 318],
            [1545, 303],
            [1610, 286],
        ],
        dtype=float,
    )
    xs = np.linspace(anchors[0, 0], anchors[-1, 0], 500)
    ys = np.interp(xs, anchors[:, 0], anchors[:, 1])
    kernel = np.ones(17) / 17
    ys = np.convolve(np.pad(ys, (8, 8), mode="edge"), kernel, mode="valid")
    for grid_y in (230, 310, 390, 470):
        od.line(
            [(sx(940), sx(grid_y)), (sx(1610), sx(grid_y))],
            fill=(34, 50, 77, 120),
            width=max(1, int(0.7 * S)),
        )
    polygon = [(sx(xs[0]), sx(520))] + [(sx(x), sx(y)) for x, y in zip(xs, ys)] + [(sx(xs[-1]), sx(520))]
    od.polygon(polygon, fill=(255, 153, 51, 14))
    od.line(
        [(sx(x), sx(y)) for x, y in zip(xs, ys)],
        fill=(255, 153, 51, 95),
        width=max(1, int(1.5 * S)),
        joint="curve",
    )
    od.line(
        [(sx(950), sx(452)), (sx(1600), sx(352))],
        fill=(143, 163, 191, 62),
        width=max(1, int(0.9 * S)),
    )
    od.ellipse(sbox((1240, 249, 1250, 259)), fill=(255, 153, 51, 150))
    od.ellipse(sbox((1370, 416, 1380, 426)), fill=(248, 113, 113, 140))
    image.alpha_composite(overlay)
    d = ImageDraw.Draw(image)
    text(d, 950, 544, "illustrative path · not a result", F("regular", 11), "#4E647F")

    # Kicker mark: a precise saffron square, not a logo.
    d.rectangle(sbox((100, 92, 110, 102)), fill=ACCENT)
    tracked(d, 122, 90, "QUANT RESEARCH · NSE INDIA", F("semibold", 12.5), TEAL, tracking=2.2)

    tracked_runs(
        d,
        100,
        158,
        [("NSE ", TEXT), ("ALPHA", ACCENT), (" LAB", TEXT)],
        F("bold", 80),
        tracking=3.2,
    )
    text(d, 102, 288, PITCH, F("regular", 25), MUTED)
    rule(d, 102, 332, 910, 332, BORDER)

    widths = []
    widths.append(chip(d, 100, 370, "968 configurations published", ACCENT))
    widths.append(chip(d, 100 + widths[0] + 14, 370, "Null result up front", TEAL))
    widths.append(chip(d, 100 + widths[0] + widths[1] + 28, 370, "Apache-2.0", GREEN))

    text(d, 102, 446, "Full sweep published · point-in-time membership · six explicit charges", F("regular", 13), "#6E829D")
    footer(d, W, H, margin=80, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 2. architecture.png
# ---------------------------------------------------------------------------


def build_architecture() -> Image.Image:
    W, H = 1800, 980
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)

    title_block(
        d,
        "SYSTEM ARCHITECTURE",
        "One spec in → one auditable result out",
        "Eight deterministic stages. No hidden data, no silent fills, no unpriced charges.",
        x=100,
        y=58,
        title_size=38,
    )
    chip(d, 1400, 74, "8 stages · 348 checks", GREEN, h=40, font_size=14)

    nodes = [
        (
            "Checksummed data pack",
            "SHA-256 manifest · verified before any run",
            "INPUT",
            ACCENT,
            "manifest.json",
        ),
        (
            "Point-in-time membership",
            "Only constituents as of date T",
            "EVIDENCE",
            TEAL,
            "membership(T)",
        ),
        (
            "Split/bonus-adjusted bars",
            "Dated actions, price-only series",
            "EVIDENCE",
            TEAL,
            "adjusted bars",
        ),
        (
            "Momentum rank on close T",
            "63-session return, ranked cross-sectionally",
            "EVIDENCE",
            TEAL,
            "ranked candidates",
        ),
        (
            "Next-open fills at T+1",
            "Integer shares, no look-ahead",
            "EXECUTION",
            GREEN,
            "orders at open",
        ),
        (
            "Six explicit charges",
            "Brokerage · STT · stamp · exchange · GST · slippage",
            "EXECUTION",
            GREEN,
            "cash ledger",
        ),
        (
            "Metrics + 348-check validation",
            "Invariants, reconciliation, audit trail",
            "PUBLICATION",
            ACCENT,
            "validation.json",
        ),
        (
            "968-row CSV + Streamlit UI",
            "Full sweep published, primary flagged",
            "PUBLICATION",
            ACCENT,
            "FINAL_RESULTS.csv",
        ),
    ]

    x0, w, gap = 100, 380, 26
    h = 250
    rows_y = [230, 640]

    for index, (heading, subtitle, phase, phase_color, artifact) in enumerate(nodes):
        row = 0 if index < 4 else 1
        col = index % 4
        x = x0 + col * (w + gap)
        y = rows_y[row]
        panel(d, (x, y, x + w, y + h), fill=PANEL, outline=BORDER, radius=16)
        # A one-pixel inner highlight line keeps the cards crisp after downsample.
        rule(d, x + 18, y + 1.5, x + w - 18, y + 1.5, "#2B3F5F", width=0.8)

        badge(d, x + 38, y + 38, f"{index + 1:02d}", ACCENT, radius=20)
        tracked(d, x + w - 24, y + 30, phase, F("semibold", 9.5), phase_color, tracking=1.5, anchor="ra")
        text(d, x + 26, y + 80, heading, F("semibold", 20), TEXT)
        paragraph(d, x + 26, y + 118, subtitle, F("regular", 14.5), MUTED, w - 52, 21)
        rule(d, x + 26, y + 176, x + w - 26, y + 176, BORDER_SOFT, width=0.8)
        tracked(d, x + 26, y + 188, "OUTPUT", F("semibold", 8.5), "#5B7190", tracking=1.4)
        text(d, x + 26, y + 208, artifact, F("mono", 12.5), phase_color)

    # Horizontal arrows on both rows.
    for row_y in rows_y:
        cy = row_y + h / 2
        for col in range(3):
            start = x0 + col * (w + gap) + w + 4
            end = x0 + (col + 1) * (w + gap) - 4
            arrow(d, (start, cy), (end, cy), color="#3B7E82", width=1.6, head=7)

    # Carry the flow from the end of row one to the start of row two.
    end_x = x0 + 3 * (w + gap) + w / 2
    start_x = x0 + w / 2
    poly_arrow(
        d,
        [(end_x, rows_y[0] + h + 4), (end_x, 566), (start_x, 566), (start_x, rows_y[1] - 4)],
        color="#3B7E82",
        width=1.6,
        head=7,
    )
    text(d, (end_x + start_x) / 2, 546, "carry forward", F("regular", 10.5), "#5A7396", anchor="mm")
    d.ellipse(sbox((end_x - 3, rows_y[0] + h + 2, end_x + 3, rows_y[0] + h + 8)), fill=TEAL)

    # Closing line in the gutter above the footer rule.
    text(d, 100, 906, "Same inputs → same CSV, every run.", F("regular", 12.5), "#637893")
    footer(d, W, H, margin=100, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 3. quickstart.png
# ---------------------------------------------------------------------------


def build_quickstart() -> Image.Image:
    W, H = 1800, 900
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)

    title_block(
        d,
        "QUICKSTART",
        "Clone, test, verify, run",
        "Tests pass offline before the data pack is involved. No API keys, no network at backtest time.",
        x=90,
        y=56,
        title_size=40,
    )
    chip(d, 1370, 72, "≈ 5 minutes · Python 3.12+", TEAL, h=40, font_size=14)

    cards = [
        (
            "Clone + sync",
            [
                ("cmd", "git clone https://github.com/Kaustubh-Rathi/nse-alpha-lab.git"),
                ("cmd", "cd nse-alpha-lab"),
                ("cmd", "uv sync"),
            ],
        ),
        (
            "Test offline",
            [
                ("cmd", "uv run pytest -q"),
                ("note", "# no data pack needed · runs offline"),
                ("note", "# checks the code before any data lands"),
            ],
        ),
        (
            "Verify the pack",
            [
                ("cmd", "$env:PYTHONPATH='src'"),
                ("cmd", "uv run python -m nse_pipeline verify --pack data"),
                ("note", "# private/staged -- see docs/PUBLISH.md"),
            ],
        ),
        (
            "Run the lab",
            [
                ("cmd", "uv sync --extra lab"),
                ("cmd", "uv run streamlit run apps/lab/app.py"),
                ("note", "# CLI: set $env:PYTHONPATH='src' first"),
                ("cmd", "uv run python -m nse_pipeline backtest examples/specs/primary.json"),
            ],
        ),
    ]

    cw, ch, gx, gy = 794, 300, 32, 22
    x0, y0 = 90, 200
    for index, (heading, lines) in enumerate(cards):
        col, row = index % 2, index // 2
        x = x0 + col * (cw + gx)
        y = y0 + row * (ch + gy)
        panel(d, (x, y, x + cw, y + ch), fill=PANEL, outline=BORDER, radius=16)
        badge(d, x + 46, y + 44, str(index + 1), ACCENT, radius=20)
        text(d, x + 80, y + 29, heading, F("semibold", 24), TEXT)
        tracked(d, x + cw - 26, y + 32, f"STEP {index + 1}", F("semibold", 9.5), "#5B7190", tracking=1.4, anchor="ra")

        tx, ty, twidth, theight = x + 24, y + 82, cw - 48, 192
        d.rounded_rectangle(sbox((tx, ty, tx + twidth, ty + theight)), radius=sx(9), fill=PANEL_DEEP, outline="#1D2C45", width=max(1, int(S)))
        rule(d, tx + 1, ty + 31, tx + twidth - 1, ty + 31, "#1D2C45")
        text(d, tx + 16, ty + 11, "bash", F("mono", 11), "#5F7692")
        text(d, tx + twidth - 14, ty + 11, "copy-ready", F("mono", 9.5), "#465B76", anchor="ra")

        ly = ty + 48
        for kind, value in lines:
            if kind == "cmd":
                text(d, tx + 16, ly, "$", F("mono_bold", 15.5), TEAL)
                text(d, tx + 38, ly, value, F("mono", 15.5), TEXT)
            else:
                text(d, tx + 16, ly, value, F("mono", 13), "#6F839D")
            ly += 32

    footer(d, W, H, margin=90, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 4. alpha-anatomy.png
# ---------------------------------------------------------------------------


def build_alpha_anatomy() -> Image.Image:
    W, H = 1700, 1050
    spec = load_spec()
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)

    title_block(
        d,
        "SPEC ANATOMY",
        "Every number has a name",
        "examples/specs/primary.json · one strict schema, unknown keys rejected",
        x=70,
        y=52,
        title_size=40,
    )

    # JSON panel.
    px, py, pw, ph = 60, 210, 800, 700
    panel(d, (px, py, px + pw, py + ph), fill=PANEL_DEEP, outline=BORDER, radius=16)
    rule(d, px + 1, py + 48, px + pw - 1, py + 48, BORDER)
    d.ellipse(sbox((px + 18, py + 20, px + 26, py + 28)), fill=RED)
    d.ellipse(sbox((px + 32, py + 20, px + 40, py + 28)), fill=ACCENT)
    d.ellipse(sbox((px + 46, py + 20, px + 54, py + 28)), fill=GREEN)
    text(d, px + 68, py + 13, "examples/specs/primary.json", F("mono", 13), MUTED)
    text(d, px + pw - 20, py + 14, "JSON · UTF-8", F("mono", 10), "#506784", anchor="ra")

    mono = F("mono", 16)
    mono_bold = F("mono_bold", 16)
    code_x = px + 66
    maxw = pw - 66 - 26
    line_h = 30
    y = py + 72

    def code_line(parts: list[tuple[str, str]], advance: bool = True):
        nonlocal y
        x = code_x
        for value, color in parts:
            text(d, x, y, value, mono, color)
            x += tw(mono, value)
        if advance:
            y += line_h

    row_index = 0

    def line_number() -> str:
        nonlocal row_index
        row_index += 1
        return f"{row_index:>2}"

    # Opening brace.
    text(d, px + 20, y, line_number(), F("mono", 12), "#455B78")
    code_line([("{", "#7E93AE")])

    items = list(spec.items())
    for idx, (key, value) in enumerate(items):
        if key == "benchmark":
            # The headline comparison is the NIFTY 50 price index. Keep the
            # rendered spec aligned with the report's stated benchmark.
            value = "nifty50"
        comma = "," if idx < len(items) - 1 else ""
        prefix = f'  "{key}": '
        rendered = json.dumps(value, ensure_ascii=False)
        if tw(mono, prefix + rendered + comma) <= maxw:
            text(d, px + 20, y, line_number(), F("mono", 12), "#455B78")
            code_line(
                [
                    (prefix, "#8FA3BF"),
                    (rendered, TEAL if isinstance(value, str) else (RED if value is None else ACCENT)),
                    (comma, "#7E93AE"),
                ]
            )
        else:
            # The description is the only field long enough to wrap. Keep the
            # JSON quoting visible and indent continuation lines inside the string.
            text(d, px + 20, y, line_number(), F("mono", 12), "#455B78")
            body = str(value)
            open_prefix = prefix + '"'
            available = maxw - tw(mono, open_prefix) - tw(mono, '"' + comma)
            words = body.split()
            chunks: list[str] = []
            current = ""
            for word in words:
                trial = (current + " " + word).strip()
                if tw(mono, trial) <= available or not current:
                    current = trial
                else:
                    chunks.append(current)
                    current = word
            if current:
                chunks.append(current)
            text(d, code_x, y, open_prefix, mono, "#8FA3BF")
            text(d, code_x + tw(mono, open_prefix), y, chunks[0], mono, TEAL)
            y += line_h
            indent = " " * (len(prefix) + 1)
            for chunk_idx, chunk in enumerate(chunks[1:]):
                text(d, px + 20, y, line_number(), F("mono", 12), "#455B78")
                text(d, code_x, y, indent, mono, "#8FA3BF")
                tail = chunk + ('"' + comma if chunk_idx == len(chunks) - 2 else "")
                text(d, code_x + tw(mono, indent), y, tail, mono, TEAL)
                y += line_h

    text(d, px + 20, y, line_number(), F("mono", 12), "#455B78")
    code_line([("}", "#7E93AE")])

    # Strict-schema seal inside the panel.
    seal_y = py + ph - 40
    rule(d, px + 26, seal_y - 16, px + pw - 26, seal_y - 16, BORDER_SOFT)
    text(d, px + 28, seal_y, "STRICT LOADER", F("semibold", 9.5), ACCENT)
    text(d, px + 132, seal_y, "additional keys rejected before execution", F("regular", 12.5), MUTED)

    # Callouts.
    callouts = [
        (
            "universe",
            "Label, not a filter",
            "Universe label. The research harness applies point-in-time membership; the CLI records the label only.",
        ),
        (
            "top_n",
            "Equal-weight holdings",
            f"Integer shares, equal cash weight at the next open. {spec['top_n']} names in this spec.",
        ),
        (
            "direction",
            "Top vs bottom",
            "\"bottom\" is the pre-specified reversal counterfactual, not an undisclosed tuning knob.",
        ),
        (
            "lookback_sessions",
            f"{spec['lookback_sessions']} ≈ 3 months",
            f"Momentum is measured over {spec['lookback_sessions']} trading sessions ending at close T.",
        ),
        (
            "cadence",
            str(spec["cadence"]).capitalize(),
            "Rebalance when the evidence calendar has a new membership anchor.",
        ),
        (
            "stop_loss_pct / take_profit_pct",
            "Causal exit overlay",
            "Checked after entry only; null disables both exit rules.",
        ),
        (
            "cost_model",
            "Full charges vs zero cost",
            "full_charges is the headline. zero_cost is the counterfactual, not a result.",
        ),
        (
            "start / end",
            "Inclusive ISO dates",
            f"{spec['start']}..{spec['end']}. No bar outside the declared window is read.",
        ),
    ]

    cx, cw = 920, 710
    cy0, chh = 210, 86
    for index, (key, heading, body) in enumerate(callouts, start=1):
        cy = cy0 + (index - 1) * chh
        if index > 1:
            rule(d, cx, cy, cx + cw, cy, BORDER_SOFT, width=0.8)
        d.rectangle(sbox((cx, cy + 15, cx + 2.5, cy + 70)), fill=ACCENT if index % 2 else TEAL)
        badge(d, cx + 16, cy + 24, str(index), ACCENT, radius=13)
        text(d, cx + 40, cy + 12, key, F("mono_bold", 15), ACCENT)
        text(d, cx + 40 + tw(F("mono_bold", 15), key) + 12, cy + 14, "· " + heading, F("semibold", 14), TEXT)
        paragraph(d, cx + 40, cy + 40, body, F("regular", 13.2), MUTED, cw - 52, 19)

    # Warning strip.
    wy = 920
    d.rounded_rectangle(sbox((60, wy, 1640, wy + 54)), radius=sx(10), fill="#241A10", outline="#5C4322", width=max(1, int(S)))
    # Warning triangle, drawn as vectors.
    tx, ty = 84, wy + 15
    d.polygon(
        [(sx(tx + 12), sx(ty)), (sx(tx + 25), sx(ty + 23)), (sx(tx - 1), sx(ty + 23))],
        fill=ACCENT,
    )
    text(d, tx + 12, ty + 17, "!", F("bold", 13), "#241A10", anchor="mm")
    text(d, 122, wy + 16, "Unknown fields are an error — a typo can never silently change the result.", F("semibold", 17), TEXT)
    text(d, 1618, wy + 19, "FAIL CLOSED", F("semibold", 10), ACCENT, anchor="ra")

    text(d, 60, 984, "CLI scope: engine over pack bars, not the membership-filtered study.", F("regular", 10.5), "#637893")
    footer(d, W, H, margin=70, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 5. sweep-distribution.png
# ---------------------------------------------------------------------------


def build_sweep_distribution() -> Image.Image:
    W, H = 1700, 1000
    rows = load_results()
    excess = np.array([float(row["excess_cagr_pct"]) for row in rows])
    sharpe = np.array([float(row["sharpe_rf0"]) for row in rows])
    windows = np.array([float(row["window_years"]) for row in rows])
    universes = [row["universe"] for row in rows]

    median = float(np.median(excess))
    assert round(median, 2) == -9.73, median
    top_index = int(np.argmax(sharpe))
    top_sharpe = sharpe[top_index]
    top_window = windows[top_index]
    assert round(top_sharpe, 4) == 1.3362 and round(top_window, 2) == 4.66
    assert sum(1 for row in rows if float(row["alpha_t_newey_west_5"]) > 1.96) == 26

    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)
    title_block(
        d,
        "SENSITIVITY SWEEP",
        "The sweep is a map, not a menu",
        "968 configurations · five universes · one identical charge stack",
        x=80,
        y=54,
        title_size=42,
        subtitle_size=17,
    )

    # Left panel: excess CAGR distribution.
    lx, ly, lw, lh = 80, 200, 770, 640
    panel(d, (lx, ly, lx + lw, ly + lh), fill=PANEL, outline=BORDER, radius=16)
    text(d, lx + 28, ly + 22, "Excess CAGR vs NIFTY 50", F("semibold", 21), TEXT)
    text(d, lx + 28, ly + 52, "all 968 configurations · percentage points per year", F("regular", 13), MUTED)

    def hist_fig(fig):
        ax = fig.add_axes([0.105, 0.145, 0.855, 0.765])
        _style_axes(ax)
        bins = np.linspace(-45, 20, 31)
        counts, edges = np.histogram(excess, bins=bins)
        centers = (edges[:-1] + edges[1:]) / 2
        colors = ["#B9585E" if c < 0 else "#2D8F75" for c in centers]
        ax.bar(centers, counts, width=(edges[1] - edges[0]) * 0.9, color=colors, edgecolor=PANEL, linewidth=0.5)
        ax.axvline(0, color="#6B819E", linewidth=0.9, alpha=0.85)
        ax.axvline(median, color=ACCENT, linewidth=1.6, linestyle=(0, (5, 2.5)))
        ax.annotate(
            "median config trails\nNIFTY 50 by 9.73 pp/yr",
            xy=(median, 132),
            xytext=(-43.4, 146),
            fontsize=9.4,
            color=TEXT,
            linespacing=1.35,
            arrowprops=dict(arrowstyle="-", color=ACCENT, lw=0.9, shrinkA=3, shrinkB=2),
        )
        ax.text(
            0.985,
            0.955,
            "n = 968   ·   220 above zero",
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8.6,
            color=MUTED,
        )
        ax.set_xlim(-45, 20)
        ax.set_ylim(0, 175)
        ax.set_xticks([-40, -30, -20, -10, 0, 10])
        ax.set_yticks([0, 50, 100, 150])
        ax.set_xlabel("excess CAGR (pp/yr)")
        ax.set_ylabel("configurations")
        ax.grid(axis="x", visible=False)

    chart_img = chart(lw - 20, lh - 100, hist_fig)
    image.paste(chart_img, (int(sx(lx + 10)), int(sx(ly + 86))))

    # Right panel: Sharpe vs window length.
    rx, ry, rw, rh = 880, 200, 740, 640
    panel(d, (rx, ry, rx + rw, ry + rh), fill=PANEL, outline=BORDER, radius=16)
    text(d, rx + 28, ry + 22, "Sharpe vs window length", F("semibold", 21), TEXT)
    text(d, rx + 28, ry + 52, "every trial · coloured by universe", F("regular", 13), MUTED)

    universe_order = ["smallcap250", "nifty500", "midcap150", "msc400", "nifty50"]
    universe_names = {
        "nifty50": "NIFTY 50",
        "nifty500": "NIFTY 500",
        "midcap150": "Midcap 150",
        "smallcap250": "Smallcap 250",
        "msc400": "MidSmallcap 400",
    }
    universe_colors = {
        "smallcap250": ACCENT,
        "nifty500": TEAL,
        "midcap150": GREEN,
        "msc400": RED,
        "nifty50": "#8FA3BF",
    }

    def scatter_fig(fig):
        ax = fig.add_axes([0.115, 0.13, 0.855, 0.79])
        _style_axes(ax)
        ax.axvspan(4.55, 5.0, color=ACCENT, alpha=0.055, linewidth=0)
        for universe in universe_order:
            mask = np.array([u == universe for u in universes])
            ax.scatter(
                windows[mask],
                sharpe[mask],
                s=15,
                color=universe_colors[universe],
                alpha=0.62,
                edgecolors="none",
                label=universe_names[universe],
            )
        ax.scatter(
            [top_window],
            [top_sharpe],
            s=95,
            facecolors="none",
            edgecolors=ACCENT,
            linewidths=1.2,
            zorder=5,
        )
        ax.annotate(
            "max of 968 trials:\n1.3362 Sharpe, 4.66 yrs",
            xy=(4.68, top_sharpe - 0.01),
            xytext=(6.55, 1.24),
            fontsize=9.4,
            color=TEXT,
            linespacing=1.35,
            arrowprops=dict(arrowstyle="-", color=ACCENT, lw=0.9, shrinkA=6, shrinkB=6),
        )
        ax.axhline(0, color="#6B819E", linewidth=0.8, alpha=0.7)
        ax.set_xlim(4.3, 13.5)
        ax.set_ylim(-1.15, 1.45)
        ax.set_xticks([4.66, 6, 8, 10, 13.1])
        ax.set_xticklabels(["4.66", "6", "8", "10", "13.1"])
        ax.set_yticks([-1.0, -0.5, 0.0, 0.5, 1.0])
        ax.set_xlabel("window length (years)")
        ax.set_ylabel("Sharpe (rf = 0)")
        ax.grid(axis="x", visible=False)
        legend = ax.legend(
            loc="upper right",
            frameon=True,
            facecolor=PANEL_DARK,
            edgecolor=BORDER,
            fontsize=8.4,
            labelcolor=TEXT,
            borderpad=0.7,
            handletextpad=0.7,
            labelspacing=0.55,
            markerscale=2.2,
            title="Universe",
            title_fontsize=8.2,
        )
        legend.get_title().set_color(MUTED)
        legend.get_frame().set_linewidth(0.7)

    chart_img = chart(rw - 20, rh - 140, scatter_fig)
    image.paste(chart_img, (int(sx(rx + 10)), int(sx(ry + 86))))
    text(
        d,
        rx + 28,
        ry + rh - 40,
        "Top 25 Sharpe rows are all 4.66–4.92-year windows",
        F("regular", 12.5),
        MUTED,
    )
    text(d, rx + rw - 28, ry + rh - 40, "a map of trials, not a portfolio menu", F("regular", 11.5), "#5C718C", anchor="ra")

    # Footnote strip.
    rule(d, 80, 872, 1620, 872, BORDER)
    text(d, 80, 894, "Only 26 / 968 clear t = 1.96; ~48 expected by chance at 5%.", F("semibold", 16), TEXT)
    text(d, 1620, 896, "Newey-West (5) alpha t-statistics · no multiple-testing correction", F("regular", 12.5), MUTED, anchor="ra")
    footer(d, W, H, margin=80, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 6. headline-scorecard.png
# ---------------------------------------------------------------------------


def build_headline_scorecard() -> Image.Image:
    W, H = 1700, 950
    rows = load_results()
    primary = next(row for row in rows if row["config_id"] == "nifty500_top_N10_L63_monthly_none")
    total_return = float(primary["total_return_pct"])
    cagr = float(primary["cagr_pct"])
    sharpe = float(primary["sharpe_rf0"])
    max_dd = float(primary["max_drawdown_pct"])
    t_ols = float(primary["alpha_t_ols"])
    t_nw = float(primary["alpha_t_newey_west_5"])
    assert (round(total_return, 2), round(cagr, 2), round(sharpe, 3), round(max_dd, 2)) == (907.97, 19.30, 0.778, -55.30)

    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)

    tracked(d, 80, 54, "PRIMARY RESULT · PRE-SPECIFIED", F("semibold", 12.5), TEAL, tracking=2.4)
    text(
        d,
        80,
        88,
        "Pre-specified · N10 · NIFTY 500 · 63-session momentum · monthly · full charges",
        F("semibold", 26),
        TEXT,
    )
    text(
        d,
        80,
        136,
        "2013-05-31 → 2026-09-30 · next-open fills · net of six explicit charges · results published in full",
        F("regular", 15),
        MUTED,
    )
    chip(d, 1410, 52, "13.1-year window", TEAL, h=38, font_size=13)

    metrics = [
        ("TOTAL RETURN", f"{total_return:,.2f}%", "net of charges", GREEN),
        ("CAGR", f"{cagr:.2f}%", "compound annual growth", TEXT),
        ("SHARPE", f"{sharpe:.3f}", "rf = 0 · daily returns", TEAL),
        ("MAX DRAWDOWN", f"−{abs(max_dd):.2f}%", "peak to trough", RED),
    ]
    mx, my, mw, mh, mg = 80, 200, 367, 210, 24
    for index, (label, value, sub, color) in enumerate(metrics):
        x = mx + index * (mw + mg)
        panel(d, (x, my, x + mw, my + mh), fill=PANEL, outline=BORDER, radius=16)
        tracked(d, x + 26, my + 24, label, F("semibold", 10.5), MUTED, tracking=1.8)
        rule(d, x + 26, my + 52, x + 72, my + 52, color, width=2.2)
        text(d, x + 25, my + 66, value, F("bold", 45), color)
        rule(d, x + 26, my + 146, x + mw - 26, my + 146, BORDER_SOFT)
        text(d, x + 26, my + 162, sub, F("regular", 12.5), MUTED)

    # Significance section.
    text(d, 80, 460, "Is the alpha distinguishable from zero?", F("semibold", 24), TEXT)
    text(d, 80, 494, "Three checks, all pre-specified.", F("regular", 13.5), MUTED)

    checks = [
        ("OLS t-statistic", t_ols, "threshold 1.96", 2.4, f"{t_ols:.2f}"),
        ("Newey-West(5) t-statistic", t_nw, "threshold 1.96", 2.4, f"{t_nw:.2f}"),
        ("Block-bootstrap p-value", 0.096, "threshold 0.05", 0.12, "0.096"),
    ]
    sy, sh, sw, sg = 532, 158, 500, 20
    for index, (label, metric, threshold, scale_max, shown) in enumerate(checks):
        x = 80 + index * (sw + sg)
        panel(d, (x, sy, x + sw, sy + sh), fill=PANEL, outline=BORDER, radius=14)
        text(d, x + 24, sy + 18, label, F("semibold", 13.5), MUTED)
        text(d, x + 24, sy + 46, shown, F("bold", 38), RED)
        text(d, x + sw - 24, sy + 62, threshold, F("regular", 12), "#6E829D", anchor="ra")
        # A real scale, not a decorative bar: 0 → max, with the 5% threshold
        # tick and the observed statistic marked against it.
        rail_left, rail_right = x + 26, x + sw - 26
        rail_y = sy + 112
        rule(d, rail_left, rail_y, rail_right, rail_y, "#2A3D59", width=1)
        threshold_x = rail_left + (1.96 / scale_max if scale_max > 1 else 0.05 / scale_max) * (rail_right - rail_left)
        observed_x = rail_left + (min(metric, scale_max) / scale_max) * (rail_right - rail_left)
        rule(d, threshold_x, rail_y - 4, threshold_x, rail_y + 4, "#7E93AE", width=1)
        d.ellipse(sbox((observed_x - 5, rail_y - 5, observed_x + 5, rail_y + 5)), fill=RED)
        text(d, rail_left, sy + 120, "0", F("regular", 9), "#5C718C", anchor="la")
        text(d, rail_right, sy + 120, f"{scale_max:g}", F("regular", 9), "#5C718C", anchor="ra")
        text(d, x + 26, sy + 136, "fails at the 5% level", F("regular", 11), "#B98287")

    # Verdict band.
    vy = 716
    d.rounded_rectangle(sbox((80, vy, 1620, vy + 94)), radius=sx(12), fill="#2A1216", outline="#7A2E36", width=max(1, int(S)))
    d.rectangle(sbox((80, vy + 12, 88, vy + 82)), fill=RED)
    text(d, 112, vy + 18, "NOT STATISTICALLY SIGNIFICANT AT 5%", F("bold", 27), RED)
    text(
        d,
        112,
        vy + 58,
        "The positive NIFTY 50 alpha cannot be separated from noise at the pre-specified level. The null result leads.",
        F("regular", 13.5),
        "#D9B7BB",
    )

    text(
        d,
        80,
        848,
        "Charges cost 3.06% of CAGR (zero-cost CAGR 22.36%).",
        F("semibold", 14.5),
        TEXT,
    )
    text(d, 1620, 850, "significant ≠ profitable; this row is not a recommendation", F("regular", 11.5), "#657A95", anchor="ra")
    footer(d, W, H, margin=80, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 7. social-preview.png
# ---------------------------------------------------------------------------


def build_social_preview() -> Image.Image:
    W, H = 1280, 640
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=28, color="#152238", radius=1.0)

    # Faint motif on the right, safely away from all text.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    anchors = np.array(
        [[860, 360], [900, 342], [940, 352], [980, 306], [1020, 282], [1060, 224], [1095, 186], [1125, 214], [1155, 272], [1190, 246], [1230, 220], [1295, 205]],
        dtype=float,
    )
    xs = np.linspace(anchors[0, 0], anchors[-1, 0], 320)
    ys = np.interp(xs, anchors[:, 0], anchors[:, 1])
    ys = np.convolve(np.pad(ys, (8, 8), mode="edge"), np.ones(17) / 17, mode="valid")
    polygon = [(sx(xs[0]), sx(500))] + [(sx(x), sx(y)) for x, y in zip(xs, ys)] + [(sx(xs[-1]), sx(500))]
    od.polygon(polygon, fill=(255, 153, 51, 12))
    od.line([(sx(x), sx(y)) for x, y in zip(xs, ys)], fill=(255, 153, 51, 85), width=max(1, int(1.4 * S)), joint="curve")
    od.ellipse(sbox((1090, 181, 1100, 191)), fill=(255, 153, 51, 145))
    image.alpha_composite(overlay)
    d = ImageDraw.Draw(image)
    text(d, 860, 522, "illustrative path · not a result", F("regular", 10.5), "#4E647F")

    d.rectangle(sbox((60, 62, 70, 72)), fill=ACCENT)
    tracked(d, 82, 59, "OPEN-SOURCE RESEARCH · APACHE-2.0", F("semibold", 11.5), TEAL, tracking=2.0)

    tracked_runs(
        d,
        60,
        110,
        [("NSE ", TEXT), ("ALPHA", ACCENT), (" LAB", TEXT)],
        F("bold", 66),
        tracking=2.8,
    )
    text(d, 62, 206, PITCH, F("regular", 21.5), MUTED)
    rule(d, 62, 242, 780, 242, BORDER)

    bullets = [
        "968 configs · full results published",
        "Null result up front · 348 validation checks",
        "Python · DuckDB · Streamlit · Apache-2.0",
    ]
    by = 290
    for bullet in bullets:
        d.rectangle(sbox((62, by + 7, 70, by + 15)), fill=ACCENT)
        text(d, 86, by, bullet, F("regular", 18.5), TEXT)
        by += 42

    text(d, 60, 560, FOOTER_TEXT, F("regular", 11), MUTED)
    text(d, 1220, 560, "github.com/Kaustubh-Rathi/nse-alpha-lab", F("mono", 10.5), MUTED, anchor="ra")
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# 8. data-pack.png
# ---------------------------------------------------------------------------


def build_data_pack() -> Image.Image:
    W, H = 1600, 800
    image, d = new_canvas(W, H)
    dot_grid(image, spacing=32, color="#142136", radius=0.9)

    title_block(
        d,
        "DATA PACK",
        "Evidence before returns",
        "A versioned, checksummed folder that no backtest can quietly replace.",
        x=80,
        y=54,
        title_size=40,
    )
    chip(d, 1212, 70, "not in git · 84 files", TEAL, h=40, font_size=14)

    # Pack tree panel.
    px, py, pw, ph = 80, 200, 650, 500
    panel(d, (px, py, px + pw, py + ph), fill=PANEL, outline=BORDER, radius=16)
    text(d, px + 28, py + 22, "PACK LAYOUT", F("semibold", 10.5), MUTED)
    text(d, px + pw - 28, py + 20, "one root, one manifest", F("mono", 10), "#506784", anchor="ra")

    tree = [
        ("data/", "pack root · not committed", ACCENT, True),
        ("├── manifest.json", "SHA-256 + bytes for every file", TEXT, False),
        ("├── processed/", "adjusted bars · catalog · snapshots", TEAL, False),
        ("├── manifests/", "upstream provenance per source", TEAL, False),
        ("├── reports/", "readiness · coverage · audit", TEAL, False),
        ("└── provenance/", "upstream-to-shipped hash map", TEAL, False),
    ]
    ty = py + 74
    for index, (label, note, color, _root) in enumerate(tree):
        if index == 0:
            text(d, px + 28, ty, label, F("mono_bold", 21), color)
            text(d, px + 132, ty + 5, note, F("regular", 12.5), MUTED)
            ty += 54
            rule(d, px + 36, ty - 22, px + 36, py + ph - 44, "#28405F", width=1)
        else:
            text(d, px + 28, ty, label, F("mono", 17), TEXT)
            text(d, px + 300, ty + 2, note, F("regular", 12.3), MUTED)
            ty += 56

    text(d, px + 28, py + ph - 36, "5 groups · 84 manifest entries", F("mono", 11), "#5C718C")

    # Verify terminal.
    tx, ty2, tw2, th2 = 780, 200, 740, 252
    panel(d, (tx, ty2, tx + tw2, ty2 + th2), fill=PANEL_DEEP, outline=BORDER, radius=16)
    rule(d, tx + 1, ty2 + 40, tx + tw2 - 1, ty2 + 40, BORDER)
    d.ellipse(sbox((tx + 18, ty2 + 18, tx + 26, ty2 + 26)), fill=RED)
    d.ellipse(sbox((tx + 32, ty2 + 18, tx + 40, ty2 + 26)), fill=ACCENT)
    d.ellipse(sbox((tx + 46, ty2 + 18, tx + 54, ty2 + 26)), fill=GREEN)
    text(d, tx + 68, ty2 + 11, "terminal · offline", F("mono", 12), MUTED)
    text(d, tx + tw2 - 20, ty2 + 12, "exit 0", F("mono", 10), "#506784", anchor="ra")

    text(d, tx + 26, ty2 + 62, "$", F("mono_bold", 17), TEAL)
    text(d, tx + 48, ty2 + 62, "$env:PYTHONPATH='src'", F("mono", 15.5), TEXT)
    text(d, tx + 26, ty2 + 98, "$", F("mono_bold", 17), TEAL)
    text(d, tx + 48, ty2 + 98, "uv run python -m nse_pipeline verify --pack data", F("mono", 15.5), TEXT)
    text(d, tx + 26, ty2 + 134, "# every byte checked against the manifest before use", F("mono", 12.5), "#6F839D")

    checkmark(d, tx + 26, ty2 + 146, 26, GREEN)
    text(d, tx + 66, ty2 + 158, "PASSED:", F("mono_bold", 16), GREEN)
    text(d, tx + 158, ty2 + 158, "84 file(s) verified against the manifest.", F("mono", 15.5), TEXT)
    text(d, tx + 26, ty2 + 198, "0 missing · 0 mismatched · 0 hash failures", F("mono", 11.5), TEAL)
    text(d, tx + 26, ty2 + 222, "example: verified pack 1.0.0", F("mono", 11), "#5C718C")

    # Note panel.
    nx, ny, nw, nh = 780, 482, 740, 218
    panel(d, (nx, ny, nx + nw, ny + nh), fill=PANEL, outline=BORDER, radius=16)
    # Shield, drawn as vector geometry.
    shx, shy = nx + 30, ny + 32
    d.polygon(
        [
            (sx(shx + 18), sx(shy)),
            (sx(shx + 36), sx(shy + 8)),
            (sx(shx + 34), sx(shy + 30)),
            (sx(shx + 18), sx(shy + 44)),
            (sx(shx + 2), sx(shy + 30)),
            (sx(shx), sx(shy + 8)),
        ],
        fill="#10291F",
        outline=GREEN,
        width=max(1, int(S)),
    )
    d.line([(sx(shx + 10), sx(shy + 23)), (sx(shx + 17), sx(shy + 31)), (sx(shx + 29), sx(shy + 15))], fill=GREEN, width=max(1, int(2 * S)), joint="curve")
    text(d, nx + 86, ny + 28, "Not in git. SHA-256 checked.", F("semibold", 19), TEXT)
    text(d, nx + 86, ny + 58, "Backtests never touch the network.", F("semibold", 19), TEXT)
    paragraph(
        d,
        nx + 86,
        ny + 96,
        "A mismatched byte refuses the run. The 1.0.0 pack is built and verified, but its public release is still pending; see docs/PUBLISH.md.",
        F("regular", 13.5),
        MUTED,
        nw - 116,
        20,
    )
    text(d, nx + 86, ny + 160, "manifest-first", F("mono", 10.5), TEAL)
    text(d, nx + 206, ny + 160, "exit 1 on mismatch", F("mono", 10.5), TEAL)
    text(d, nx + 360, ny + 160, "no network I/O", F("mono", 10.5), TEAL)

    footer(d, W, H, margin=80, font_size=12)
    return finish(image, W, H)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


BUILDERS = [
    ("hero.png", build_hero),
    ("architecture.png", build_architecture),
    ("quickstart.png", build_quickstart),
    ("alpha-anatomy.png", build_alpha_anatomy),
    ("sweep-distribution.png", build_sweep_distribution),
    ("headline-scorecard.png", build_headline_scorecard),
    ("social-preview.png", build_social_preview),
    ("data-pack.png", build_data_pack),
]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, builder in BUILDERS:
        image = builder()
        path = OUT_DIR / filename
        image.save(path, format="PNG", optimize=True)
        print(f"wrote {path.relative_to(ROOT)}  {image.width}x{image.height}")


if __name__ == "__main__":
    main()
