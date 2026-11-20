# Publishing the NSE Alpha Lab data pack

**Status: built and verified locally — NOT uploaded.** This build environment has
no Kaggle credentials (`~/.kaggle/kaggle.json` absent, `KAGGLE_USERNAME` /
`KAGGLE_KEY` unset), no Hugging Face token (`HF_TOKEN` unset) and no Zenodo
token. The release owner must run the commands below after supplying
credentials.

## 1. The artifact

| Fact | Value |
|---|---|
| Pack version | `1.0.0` (`nse-alpha-data-pack-1.0.0`) |
| Pack tree | `dist/nse-alpha-data-pack-1.0.0/` |
| Pack tree size | 161,261,235 bytes (161.3 MB / 153.8 MiB) |
| Files in tree | 85 |
| Manifest entries | 84 (`manifest.json` is not self-listed) |
| Archive | `dist/nse-alpha-data-pack-1.0.0.tar.gz` |
| Archive size | 148,619,160 bytes (148.6 MB / 141.7 MiB) |
| **Archive SHA-256** | `bfc2140113b59480153841f641a37901120affb89374355da161f03eafe60708` |
| Bars file | `processed/adjusted/adjusted_bars.parquet` — 143,298,565 bytes, SHA-256 `247dfd5f8ac0b5b736054caa0224ae3c21bd196a2b619c700cc1dd70bb360509` |
| Bars coverage | 8,870,589 rows, 6,677 sessions, 5,810 stable keys, 2000-01-03 → 2026-11-06 |
| Manifest generated_at | `2026-11-12T09:14:14+00:00` (deterministic: max upstream mtime) |
| Build summary | `dist/nse-alpha-data-pack-1.0.0.build.json` |

Deterministic rebuild: the build is offline and normalises archive mtimes,
ordering and the gzip header, so two independent full builds in this
environment produced the **same archive SHA-256**
`bfc21401…eafe60708`. To reproduce:

```powershell
python scripts/build_data_pack.py --source D:\path\to\data
```

The builder is `scripts/build_data_pack.py`. It is stdlib + pandas/pyarrow/duckdb,
never touches the network and reads the upstream data root read-only.

### Pack layout (what is included and why)

| Group | Files | Why it is in the pack |
|---|---|---|
| `processed/adjusted/adjusted_bars.parquet` | 1 | The only file the shipped `backtest` command reads. **Derived**: the upstream canonical file has no `key` column, so the builder applies the repository's own `load_adjusted_bars` filter and `resolve_identity` rules (self-checked equal to per-row resolution on 321,750 real rows). |
| `processed/adjusted/action_audit.parquet`, `audit_manifest.json` | 2 | Every corporate action considered, applied or excluded — the audit trail behind the adjusted prices. |
| `processed/identity/identity_edges.jsonl` | 1 | Dated symbol-rename edges; required to rebuild/audit the `key` column. |
| `processed/universe/universe_observations.parquet` | 1 | Point-in-time research-universe observations (the universe is **not** official Nifty membership). |
| `processed/index_membership/` | 53 | Dated official NIFTY 50/500 constituent snapshots — the documented anchors the research scripts read. |
| `manifests/` | 5 | Upstream release/download/build provenance (`release-*.json`, `downloads.json`, `prices-build.json`, `actions-build.json`). |
| `reports/` | 17 | The public coverage, validation, readiness and audit reports named in README "Useful audit outputs". |
| `README.md`, `DATA_LICENSES.md`, `DISCLAIMER.md`, `provenance/build-provenance.json`, `manifest.json` | 5 | Usage/verification instructions, redistribution terms, upstream→shipped hash map and the CLI manifest. |

**Excluded and why:** `raw/` (2.83 GB acquisition archives — reproducible from the
URLs+SHA-256 in `manifests/`), `market.duckdb` (small, but its views name raw and
processed files that a minimal pack does not ship, so it would be broken),
`processed/schemes/`, `processed/nifty_doc/`, `processed/prices/`,
`processed/backtest/` and the other PDF/announcement/run-output working trees
(internal research artefacts), any `*.internal.csv` (none were present under
`data/`), and the private `verification/` evidence payload (deliberately not
exported; see blockers).

Three public reports contained absolute paths from the private build directory
(`D:\fix_Investment_backtesting\…`): `reports/full-history-readiness.json`,
`reports/verified-actions.json`, `reports/official-gap-backfill.json`. The
builder strips those prefixes deterministically before hashing (recorded in
`provenance/build-provenance.json::sanitised_files`); every other shipped text
file is scanned and the build fails if a private absolute path remains.

## 2. Kaggle (chosen host)

Upload folder: `dist/kaggle-upload/` — contains the archive, the manifest, the
pack README, `DATA_LICENSES.md` and `dataset-metadata.json`. The Kaggle CLI
skips `dataset-metadata.json` itself when uploading.

### `dist/kaggle-upload/dataset-metadata.json` (generated, verbatim)

```json
{
  "title": "NSE Alpha Lab Data Pack",
  "id": "Kaustubh-Rathi/nse-alpha-lab-data-pack",
  "subtitle": "NSE price and corporate-action data for reproducible research",
  "licenses": [{"name": "CC-BY-4.0"}],
  "description": "… (pack version, archive SHA-256, unpack/verify/backtest instructions, provenance and licence note) …",
  "keywords": ["finance", "stock market", "india", "nse", "quantitative finance"],
  "expectedUpdateFrequency": "quarterly"
}
```

Constraints checked against the Kaggle CLI source and
[dataset metadata docs](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md):
title 6–50 chars (23), subtitle 20–80 chars (60), dataset slug 3–50 chars (23),
exactly **one** licence. `CC-BY-4.0` is the closest standard Kaggle licence to
the pack's actual terms (attribution required, changes marked; authoritative
per-source terms are `DATA_LICENSES.md`, which ships inside the archive and
beside it).

### Commands (owner-supplied credentials)

```powershell
# one-time: Kaggle -> Settings -> Create New Token -> save as %USERPROFILE%\.kaggle\kaggle.json
# (or set $env:KAGGLE_USERNAME / $env:KAGGLE_KEY)
cd D:\fix_Investment_backtesting\public\nse-alpha-lab

kaggle datasets create -p dist\kaggle-upload -u

# check the create job, then the URL is:
kaggle datasets status kaustubh-rathi/nse-alpha-lab-data-pack
```

* `-u` / `--public` makes the dataset public (the CLI default is private).
* No sub-directories exist in the upload folder, so `--dir-mode` is irrelevant
  here. (`--dir-mode zip` would wrap each top-level *directory* into its own
  zip, which is why the pack's nested tree cannot be uploaded as loose files
  through this CLI; the versioned archive is the payload instead.)
* Resulting URL pattern:
  `https://www.kaggle.com/datasets/<owner-slug>/<dataset-slug>` →
  **`https://www.kaggle.com/datasets/kaustubh-rathi/nse-alpha-lab-data-pack`**
  (Kaggle username casing may be lower-cased; `id` in the metadata must be the
  account's exact slug or the CLI refuses the upload).
* The downstream CLI identifier is `kaustubh-rathi/nse-alpha-lab-data-pack`.

### Consumer install from Kaggle

```powershell
kaggle datasets download -d kaustubh-rathi/nse-alpha-lab-data-pack -p dist\kaggle-fetch --unzip
cd dist\kaggle-fetch
# verify the archive itself, then extract and verify the pack
(Get-FileHash nse-alpha-data-pack-1.0.0.tar.gz -Algorithm SHA256).Hash.ToLower()
tar -xzf nse-alpha-data-pack-1.0.0.tar.gz
python -m nse_pipeline verify --pack nse-alpha-data-pack-1.0.0
```

Or with the shipped fetcher (it downloads/unzips the dataset; the archive still
needs one extraction because the dataset is archive-hosted):

```powershell
python -m nse_pipeline fetch --source kaggle --from kaustubh-rathi/nse-alpha-lab-data-pack --dest dist\kaggle-fetch
tar -xzf dist\kaggle-fetch\nse-alpha-data-pack-1.0.0.tar.gz -C dist\kaggle-fetch
python -m nse_pipeline verify --pack dist\kaggle-fetch\nse-alpha-data-pack-1.0.0
```

### Size vs Kaggle limits

Kaggle's technical specification has long been quoted as **20 GB per dataset**
(the current [Kaggle Datasets docs](https://www.kaggle.com/docs/datasets) page is
JavaScript-rendered; the 20 GB figure is stated in
[Kaggle's own discussion](https://www.kaggle.com/discussions/general/155688), and
newer community answers quote 100–300 GB). At **148.6 MB archive / 161.3 MB
unpacked** the pack is about **0.7 % of the most conservative quoted limit**, and
its largest single file (`adjusted_bars.parquet`, 143.3 MB) is far below any
per-file limit. No Kaggle-specific repacking is required.

## 3. Hugging Face (alternative host; turnkey for the shipped fetcher)

HF preserves directory structure in a dataset repo, so `nse-alpha fetch --source hf`
needs no manual extraction step.

```powershell
# one-time: token from https://huggingface.co/settings/tokens  (write scope)
hf auth login
hf repos create Kaustubh-Rathi/nse-alpha-lab-data-pack --repo-type dataset --public

# upload the unpacked data-root tree (not the archive)
hf upload Kaustubh-Rathi/nse-alpha-lab-data-pack dist\nse-alpha-data-pack-1.0.0 . `
  --repo-type dataset --commit-message "NSE Alpha Lab data pack 1.0.0 (sha256 bfc2140113b59480153841f641a37901120affb89374355da161f03eafe60708)"
```

Consumer (the CLI calls `snapshot_download(..., repo_type="dataset")`):

```powershell
$env:NSE_HF_REPO = "Kaustubh-Rathi/nse-alpha-lab-data-pack"
python -m nse_pipeline fetch --source hf --from $env:NSE_HF_REPO --dest data
python -m nse_pipeline verify --pack data
```

Optionally also upload `dist\nse-alpha-data-pack-1.0.0.tar.gz` and its `.sha256`
for users who want the versioned archive; keep the unpacked tree for the
fetcher.

## 4. Zenodo (DOI / citable record)

Zenodo needs an access token with `deposit:write` and `deposit:actions`
(https://zenodo.org/account/settings/applications/tokens/new/). Upload via the
files bucket API (50 GB per record/file limit per the
[Zenodo REST API docs](https://developers.zenodo.org/)):

```bash
TOKEN=<zenodo-token>
# 1. create a draft with metadata (license id: cc-by-4.0)
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST https://zenodo.org/api/deposit/depositions \
  -d '{"metadata":{"upload_type":"dataset","title":"NSE Alpha Lab Data Pack 1.0.0",
       "description":"Checksummed data pack for the nse-alpha-lab research repository; see DATA_LICENSES.md.",
       "creators":[{"name":"Rathi, Kaustubh"}],"license":"cc-by-4.0",
       "version":"1.0.0","keywords":["NSE","equity","momentum","backtesting"]}}'
# 2. upload the archive to the returned links.bucket
curl -sS --upload-file dist/nse-alpha-data-pack-1.0.0.tar.gz \
  -H "Authorization: Bearer $TOKEN" "$BUCKET/nse-alpha-data-pack-1.0.0.tar.gz"
# 3. publish (irreversible; re-check metadata first)
curl -sS -H "Authorization: Bearer $TOKEN" \
  -X POST "https://zenodo.org/api/deposit/depositions/$DEPOSIT_ID/actions/publish"
```

Resulting DOI URL pattern: `https://doi.org/10.5281/zenodo.<record-id>`. Use the
sandbox (`https://sandbox.zenodo.org`) first — it issues `10.5072` test DOIs.

## 5. Publishing a new version

```powershell
python scripts/build_data_pack.py --version 1.1.0
kaggle datasets version -p dist\kaggle-upload -m "Data pack 1.1.0 (sha256 <new>)" -u
hf upload Kaustubh-Rathi/nse-alpha-lab-data-pack dist\nse-alpha-data-pack-1.1.0 . --repo-type dataset
# Zenodo: create a new version of the published record instead of a new deposition
```

Keep `DATA_LICENSES.md`'s version string in step with `--version`.

## 6. Verification evidence (local, this build)

Logs are under `dist/_verify/` (git-ignored):

| Step | Result |
|---|---|
| `verify --pack dist\nse-alpha-data-pack-1.0.0` | **PASSED: 84 file(s) verified** (84 `OK`, exit 0) |
| corrupt one byte of `reports/validation.json`, re-verify | **FAILED: 0 missing, 1 mismatched, 83 ok**, exit 1, SHA-256 mismatch printed |
| restore byte, re-verify | **PASSED: 84 file(s) verified**, exit 0 |
| archive SHA-256 vs `.sha256` file | match, `bfc21401…eafe60708` |
| extract archive → `verify` extracted tree | **PASSED: 84 file(s) verified**, exit 0 |
| `fetch --source local --from <pack> --dest <tmp> --manifest <pack>/manifest.json` | **PASSED: 84 file(s) verified after download**, exit 0 |
| `NSE_DATA_DIR=<pack> backtest <spec> --out …` (2022-01-01→2024-06-30) | exit 0: 617 sessions, 30 rebalances, **268 trades**, final equity 9,430,292.16, total return −5.6971 % |

## 7. Blockers before first public release (owner action)

1. **Credentials.** No Kaggle/HF/Zenodo credentials exist in this environment;
   nothing was uploaded. Commands above are ready.
2. ~~Contact email placeholder~~ — **fixed.** `DATA_LICENSES.md` §header and §5 now name the
   repository issue tracker as the monitored public contact channel. Substitute a dedicated
   address in both places if private correspondence is preferred.
3. ~~README command mismatch (docs vs code)~~ — **fixed.** README "Getting data" now
   documents `python -m nse_pipeline verify --pack data`, the pack-root `manifest.json`,
   the `provenance/` directory, and `nse-alpha fetch` for both local and hosted sources.
4. **Kaggle metadata owner slug.** `id` is set to `Kaustubh-Rathi/…` as
   requested. If the Kaggle account slug differs (case or hyphenation), change
   `id` in `dist/kaggle-upload/dataset-metadata.json` and rebuild; the URL
   follows `<owner-slug>/nse-alpha-lab-data-pack`.
5. **Sweep reproduction is a separate, pre-existing repo gap.** The pack runs the
   CLI `backtest` end-to-end, but the published 968-row sweep still is not
   reproducible from the public repo: `research/run_sweep.py` /
   `momentum_grid.DEFAULT_SNAPSHOTS` read the deliberately unexported
   `verification/` payload, and `research/benchmark-series.json` plus
   `research/FINAL_RESULTS_public.csv` are absent from the exported tree. Those
   files are outside the data pack (and outside this task's write scope); fix
   them in the repo export before publishing a "reproducible sweep" claim.
6. **`.gitignore`.** Current `dist/` rule (line 54) already ignores the pack
   tree, archive, Kaggle staging and evidence logs — verified with
   `git check-ignore`. `*.tar.gz` is **not** globally ignored, so if the archive
   is ever written outside `dist/`, add `*.tar.gz` (or `*.tar`). No change was
   needed for this build and `.gitignore` was not edited.
