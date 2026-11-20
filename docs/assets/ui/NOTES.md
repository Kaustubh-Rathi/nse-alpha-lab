# UI capture notes

Project: [nse-alpha-lab](https://github.com/Kaustubh-Rathi/nse-alpha-lab)

Captured from the real Streamlit app with Playwright and the installed Microsoft
Edge (`channel="msedge"`, no browser download), viewport **1440x900** at
`device_scale_factor=2`, then downsampled to 1440x900 PNG. Streamlit's own
chrome (Deploy button, main menu, status widget, header overlay, sidebar
collapse control) was hidden for the crop; no application content was altered
or mocked.

## Result

| Page | File | Status |
| --- | --- | --- |
| Home | `home.png` | Captured; hero, research-preview warning, environment table |
| Data | `data.png` | Captured; "No data pack installed" (no pack under `data/`) |
| Create | `create.png` | Captured; form and JSON spec preview |
| Compare | `compare.png` | Captured; multiple-testing warning and 968-row table frame |
| Docs | `docs.png` | Captured; limitations and disclaimer |

Backtest and Results were not captured because there is no data pack and no run
in this checkout.

## Bug found and fixed during capture

The first pass found a real crash on the Create page:

```text
KeyError: 'stop_loss_pct'
apps/lab/app.py, page_create
```

`build_spec()` nests exits under `spec["exit_policy"]`, but `page_create()` read
top-level `spec["stop_loss_pct"]` and `spec["take_profit_pct"]`. The form and
preview rendered, then the exception appeared below them. The reads now use
`spec["exit_policy"]`, and the capture pass completes without a page error.

## Reproduce

```powershell
# from the repository root, with Streamlit installed
$env:PYTHONPATH = "src"
uv run streamlit run apps/lab/app.py --server.headless true --server.port 8501

# in another shell, with Playwright and Pillow available
python docs/assets/capture_ui.py --base-url http://localhost:8501
```
