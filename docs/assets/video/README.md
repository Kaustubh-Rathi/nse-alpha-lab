# Demo video build

The published walkthrough is checked in at:

- `docs/assets/demo.mp4` — 1080p H.264/AAC, about 3 min 54 s
- `docs/assets/demo.gif` — 12-second README preview
- `docs/assets/demo-poster.png` — poster frame
- `docs/assets/demo.srt` — captions

The narration is a deterministic script in `narration.json`. The video is a
motion-graphics explainer built from diagrams, real Streamlit screenshots and
measured SAPI speech durations. It does not claim live trading or invent a
result.

## Rebuild

The audio build uses Windows SAPI, so this step is Windows-only:

```powershell
powershell -ExecutionPolicy Bypass -File docs/assets/video/build_audio.ps1
```

That writes `audio/<scene>.wav` and `durations.json`. The WAVs are build
intermediates and are not committed.

Then render with Pillow, qrcode and ffmpeg on `PATH`:

```powershell
C:\path\to\media-venv\Scripts\python.exe docs/assets/video/render_video.py
```

`render_video.py` reads `narration.json`, `durations.json`, `audio/`, the static
diagrams in `docs/assets/images/` and the real UI captures in
`docs/assets/ui/`, then writes the four published files above.

The renderer does not download anything and does not call a cloud service.
