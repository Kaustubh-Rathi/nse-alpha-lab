#!/usr/bin/env python
"""Capture real Streamlit screenshots for README / demo use.

This script drives the running NSE Alpha Lab Streamlit app
(https://github.com/Kaustubh-Rathi/nse-alpha-lab) with Playwright's Python API,
using the locally installed Microsoft Edge (no browser download), and writes
optimised 1440px-wide PNGs into ``docs/assets/ui/``.

Prerequisites
-------------
1. A Python environment with ``streamlit`` and ``playwright`` installed.
2. The app running, e.g. from ``public/nse-alpha-lab``::

       streamlit run apps/lab/app.py --server.headless true --server.port 8501

3. Microsoft Edge installed (Playwright launches it with ``channel="msedge"``).

Usage
-----
::

    python docs/assets/capture_ui.py
    python docs/assets/capture_ui.py --base-url http://localhost:8501

The script never invents UI content: every screenshot is the rendered app at
viewport 1440x900 with ``device_scale_factor=2``, downsampled to 1440px wide.
Streamlit chrome (Deploy button, main menu, status widget, sidebar collapse
control) is hidden for a clean crop; the application itself is untouched.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "ui"
VIEWPORT = {"width": 1440, "height": 900}
DEVICE_SCALE_FACTOR = 2

# Hide only Streamlit's own chrome; no application content is touched.
CHROME_CSS = """
[data-testid="stAppDeployButton"],
[data-testid="stMainMenu"],
[data-testid="stStatusWidget"],
[data-testid="stDecoration"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stHeader"] {
    display: none !important;
}
"""

#: page label, output file, scroll offset in the app's main scroll container.
#: Offsets were chosen so each screenshot shows the page's key content:
#: Home keeps the hero + warning, Create keeps the form + start of the spec
#: preview, Compare keeps the multiple-testing warning + full table frame,
#: Docs keeps the limitations + disclaimer.
SHOTS = [
    {"label": "Home", "file": "home.png", "scroll": 0, "title": "NSE Alpha Lab"},
    {"label": "Data", "file": "data.png", "scroll": 0, "title": "Data"},
    {"label": "Create", "file": "create.png", "scroll": 88, "title": "Create a strategy spec"},
    {"label": "Compare", "file": "compare.png", "scroll": 88,
     "title": "Compare against the research grid"},
    {"label": "Docs", "file": "docs.png", "scroll": 88,
     "title": "Docs: limitations, disclaimer, licensing"},
]


def log(message: str) -> None:
    print(message, flush=True)


def click_page(page: Page, label: str) -> None:
    """Click a sidebar radio item by its visible label."""
    sidebar = page.locator('[data-testid="stSidebar"]')
    item = sidebar.locator("label").filter(has_text=label).first
    item.click()
    page.wait_for_timeout(800)


def wait_for_page(page: Page, title: str, timeout_ms: int = 60_000) -> None:
    """Wait for the page title and for Streamlit's rerun status to settle."""
    page.wait_for_function(
        """(expected) => {
            const headings = [...document.querySelectorAll('[data-testid="stMain"] h1')];
            return headings.some(h => h.innerText.trim() === expected);
        }""",
        arg=title,
        timeout=timeout_ms,
    )
    # Streamlit is websocket-driven; networkidle is best-effort, so never let it
    # abort a capture. The app's own content is the real readiness signal.
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:  # noqa: BLE001 - long-lived websocket connections are normal
        pass
    page.wait_for_timeout(2_000)


def main_scroll(page: Page, offset: int) -> None:
    page.evaluate(
        """(offset) => {
            const main = document.querySelector('[data-testid="stMain"]');
            if (main) main.scrollTop = offset;
        }""",
        offset,
    )
    page.wait_for_timeout(800)


def read_main_text(page: Page) -> str:
    try:
        return page.locator('[data-testid="stMain"]').inner_text()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8501")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    notes: list[str] = []
    captured: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="nse-ui-raw-") as raw_dir:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(
                viewport=VIEWPORT,
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text)
                if msg.type == "error"
                else None,
            )
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            log(f"Opening {args.base_url}")
            page.goto(args.base_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector('[data-testid="stSidebar"]', timeout=60_000)
            page.wait_for_timeout(3_000)
            page.add_style_tag(content=CHROME_CSS)

            for shot in SHOTS:
                label = shot["label"]
                log(f"[{label}] navigating")
                try:
                    click_page(page, label)
                    wait_for_page(page, shot["title"])
                    page.add_style_tag(content=CHROME_CSS)

                    if label == "Create":
                        # Defaults are already the realistic primary spec, but
                        # set them explicitly if this changes in the app.
                        top_n = page.get_by_role("spinbutton", name="Hold top N")
                        lookback = page.get_by_role("spinbutton", name="Lookback (sessions)")
                        top_value = top_n.input_value()
                        look_value = lookback.input_value()
                        if top_value != "10":
                            top_n.fill("10")
                            top_n.press("Enter")
                            page.wait_for_timeout(2_000)
                        if look_value != "63":
                            lookback.fill("63")
                            lookback.press("Enter")
                            page.wait_for_timeout(2_000)
                        log(f"[Create] top_n={top_n.input_value()} lookback={lookback.input_value()}")
                        text = read_main_text(page)
                        for expected in ('"top_n"', '"lookback_sessions"'):
                            if expected not in text:
                                notes.append(
                                    f"Create: spec preview does not contain {expected!r}"
                                )
                        page.add_style_tag(content=CHROME_CSS)
                        wait_for_page(page, shot["title"])
                    elif label == "Compare":
                        page.wait_for_selector('[data-testid="stDataFrame"]', timeout=60_000)
                        text = read_main_text(page)
                        if "968 rows x 124 columns" not in text:
                            notes.append(
                                "Compare: caption did not read '968 rows x 124 columns'; "
                                + next(
                                    (line for line in text.splitlines()
                                     if "rows x" in line),
                                    "no row caption found",
                                )
                            )
                        page.wait_for_timeout(3_000)
                        page.add_style_tag(content=CHROME_CSS)
                    elif label == "Data":
                        text = read_main_text(page)
                        if "No data pack installed" not in text:
                            notes.append("Data: expected the no-data-pack message, not found.")
                    elif label == "Docs":
                        text = read_main_text(page)
                        for expected in ("Limitations", "Disclaimer"):
                            if expected not in text:
                                notes.append(f"Docs: {expected!r} section not found.")

                    main_scroll(page, shot["scroll"])
                    # Park the pointer away from headings so link-hover glyphs
                    # do not appear in the capture. x=310 is the blank left
                    # margin of the main column, outside the content block.
                    page.mouse.move(310, 20)
                    page.wait_for_timeout(500)

                    error_box = page.locator('[data-testid="stException"]')
                    if error_box.count():
                        failures.append(
                            f"{label}: Streamlit exception rendered: "
                            + error_box.first.inner_text()[:500]
                        )

                    raw_path = os.path.join(raw_dir, shot["file"])
                    page.screenshot(path=raw_path, full_page=False)
                    captured.append({"label": label, "raw": raw_path, **shot})
                    log(f"[{label}] captured")

                except Exception as exc:  # noqa: BLE001 - record, keep going
                    failures.append(f"{label}: {type(exc).__name__}: {exc}")
                    log(f"[{label}] FAILED: {type(exc).__name__}: {exc}")
                    try:
                        raw_path = os.path.join(raw_dir, shot["file"])
                        page.screenshot(path=raw_path, full_page=False)
                        captured.append({"label": label, "raw": raw_path, **shot})
                    except Exception:  # noqa: BLE001
                        pass

            browser.close()

        log("Optimising screenshots")
        for item in captured:
            raw = Path(item["raw"])
            if not raw.is_file():
                continue
            with Image.open(raw) as image:
                image = image.convert("RGB")
                target = (VIEWPORT["width"],
                          round(image.height * VIEWPORT["width"] / image.width))
                resized = image.resize(target, Image.Resampling.LANCZOS)
                final = output_dir / item["file"]
                resized.save(final, format="PNG", optimize=True)
                size_kb = final.stat().st_size / 1024
                item["final"] = final
                item["dimensions"] = resized.size
                item["size_kb"] = size_kb
                log(f"  {final.name}: {resized.width}x{resized.height}, {size_kb:,.1f} KB")

    if page_errors:
        failures.extend(f"pageerror: {err}" for err in page_errors)
    # Console errors are often benign (favicon, dev websocket); record only
    # those that are not already explained by a page failure.
    notable_console = [
        err for err in console_errors
        if "favicon" not in err.lower() and "websocket" not in err.lower()
    ]
    if notable_console:
        notes.append("Console errors: " + " | ".join(notable_console[:5]))

    if failures or notes:
        lines = ["# UI capture notes", ""]
        lines.append(f"Run: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Base URL: {args.base_url}")
        lines.append("")
        if failures:
            lines.append("## Failures")
            lines.extend(f"- {failure}" for failure in failures)
            lines.append("")
        if notes:
            lines.append("## Notes")
            lines.extend(f"- {note}" for note in notes)
            lines.append("")
        lines.append("## Captured pages")
        for item in captured:
            if "final" in item:
                lines.append(
                    f"- {item['label']}: `{Path(item['final']).name}` "
                    f"({item['dimensions'][0]}x{item['dimensions'][1]}, "
                    f"{item['size_kb']:,.1f} KB)"
                )
        (output_dir / "NOTES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        log(f"Wrote {output_dir / 'NOTES.md'}")

    if failures:
        log("FAILURES:")
        for failure in failures:
            log(f"  - {failure}")
        return 1

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
