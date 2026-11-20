# Demo video build

The published walkthrough is checked in at:

- `docs/assets/demo.mp4` — 1080p H.264/AAC, full length
- `docs/assets/demo.gif` — 12-second README preview
- `docs/assets/demo-poster.png` — poster frame
- `docs/assets/demo.srt` — captions

The narration is a deterministic script in `narration.json`. The video is a
motion-graphics explainer built from diagrams, real Streamlit screenshots and
measured edge-tts speech durations. It does not claim live trading or invent a
result.

The README player is a compressed web encode uploaded as a GitHub user
attachment. It is hosted on the pinned "Walkthrough video (media host)" issue;
do not delete that issue. The web encode is an upload artifact and is not
checked in. The download link in the README points at the committed
`docs/assets/demo.mp4` master.

This walkthrough, like the rest of the documentation, was built with multiple AI
agents working under human review. The narration is a deterministic script; the
visuals are rendered from the repository's own diagrams and UI captures. The
agents did not produce or validate the research result.

## Rebuild

Install the media dependencies (a virtual environment is recommended):

```powershell
python -m pip install -r docs/assets/video/requirements.txt
```

Synthesise narration and caption cues. This step needs network access to the
Microsoft edge-tts service and `ffmpeg` on `PATH`:

```powershell
python docs/assets/video/tts.py
```

That writes `audio/<scene>.wav`, `durations.json`, `captions.json` and per-scene
SRTs under `subs/`. The WAVs and SRTs are build intermediates and are not
committed. To refresh one scene:

```powershell
python docs/assets/video/tts.py --only 07_results
```

Then render with Pillow, qrcode and ffmpeg on `PATH`:

```powershell
python docs/assets/video/render_video.py
```

`render_video.py` reads `narration.json`, `durations.json`, `captions.json`,
`audio/`, the static diagrams in `docs/assets/images/` and the real UI captures
in `docs/assets/ui/`, then writes the four published files above.

The renderer itself does not download anything and does not call a cloud
service; only the `tts.py` step calls edge-tts.
