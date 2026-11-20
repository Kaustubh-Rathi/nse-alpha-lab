# Data provenance and licensing

This file documents where the NSE Alpha Lab data pack comes from, what each source is, and
the licensing position the redistributor is relying on. It covers **data only**. The code
and documentation are licensed separately under [Apache License 2.0](LICENSE).

**Data pack:** NSE Alpha Lab Data Pack, version 1.0
**Distributed by:** Kaustubh Rathi (the "Distributor")
**Contact for data questions and takedown requests:** the issue tracker at
<https://github.com/Kaustubh-Rathi/nse-alpha-lab/issues>. This is the monitored public
channel; substitute a dedicated address here if private correspondence is preferred.

---

## 1. Provenance

Every series in the pack is derived from a public source. The table records what each
series is, where it came from, and the basis on which it is redistributed.

| # | Series | What it contains | Source | Character of source |
|---|---|---|---|---|
| 1 | **Daily OHLCV bars** | Per-symbol open, high, low, close, volume and delivery data for NSE-listed equities | NSE daily bhavcopy files (archive publications) | Publicly published by the exchange as market data |
| 2 | **Split / bonus adjustment factors** | Adjustment ratios and ex-dates for splits and bonus issues | NSE corporate-action announcements and circulars; company filings | Public announcements of record |
| 3 | **Corporate-action events** | Dividends, rights, demergers, specials and their handling status | NSE corporate-action announcements; issuer circulars | Public announcements of record |
| 4 | **NIFTY 500 membership snapshots** | 108 monthly constituent snapshots, 2013-04-18 → 2022-03-31 | NSE index announcement publications | Publicly published index constituents |
| 5 | **NIFTY 500 membership anchors** | 65 official event anchors, 2022-03-31 → 2026-09-30 | NSE index press releases and announcements | Publicly published index constituents |
| 6 | **NIFTY MidSmallcap 400 / Midcap 150 / Smallcap 250 membership** | 57 monthly constituent sets each, 2017-07 → 2022-03 | NSE index announcement publications | Publicly published index constituents |
| 7 | **NIFTY 50 membership** | 13 annual constituent snapshots, 2013-10 → 2025-10 | NSE index announcement publications | Publicly published index constituents |
| 8 | **NIFTY 50 benchmark series (primary)** | Daily closing index levels for `^NSEI` | Yahoo Finance (third party) | Third-party republication of index levels |
| 9 | **NIFTY 50 and NIFTY 500 benchmark series (cross-check)** | Daily closing index levels, documented index-rename splice | NSE archive publications (`S&P CNX Nifty` → `CNX Nifty` → `Nifty 50`; `S&P CNX 500` → `CNX 500` → `Nifty 500`) | Publicly published index levels |
| 10 | **Nifty 10-year G-Sec benchmark** | Daily closing levels of the Nifty 10 Yr Benchmark G-Sec index | NSE archive publications | Publicly published index levels |
| 11 | **Trading calendars** | Session dates and derived holiday structure | NSE published trading holidays; observed bar dates | Publicly published |
| 12 | **Derived research artefacts** | Equity curves, trade lists, metrics, `FINAL_RESULTS_public.csv` and the other CSVs in `research/` | Computed by this repository's code from series 1–11 | Original work of the Distributor, licensed Apache-2.0 |

The pack contains no personal data, no client records, no order-book data, no
tick data, and no material obtained under a paid vendor subscription or a
non-disclosure agreement.

---

## 2. Attestation

The Distributor attests, as the party publishing the data pack, that:

1. **The underlying material is public.** Each series above originates from a publication
   that the exchange or the index provider makes available to the public without a paid
   subscription, a licence key, or an executed data agreement at the point of acquisition.
2. **Redistribution is permitted on the basis stated here.** The Distributor has reviewed
   the acquisition path for each series and relies on the public-published status of the
   underlying material as the basis for redistribution.
3. **Derivative works are permitted on the same basis.** Remixes, re-computations,
   reformattings, subsets, aggregations and research built on the pack may be distributed
   on the same terms. Derivative works must carry this attestation forward (see §4).
4. **Attribution is given.** Every series is attributed to its source in this file, and
   the index names remain the trademarks of their owners (see §5).
5. **No rights are claimed over the underlying facts.** The Distributor claims copyright
   only in the selection, arrangement, cleaning, normalisation and the original computed
   columns added by this repository — not in the underlying market facts themselves.
6. **The pack is provided as-is, without warranty.** See §6.

This attestation is a statement of the Distributor's position and of the basis on which
the pack is published. It is not a legal opinion, and it does not transfer any obligation
from the Distributor to you. If you redistribute, the terms you accept are the ones in §3
and §4.

---

## 3. Terms of redistribution

To redistribute the pack, or a derivative of it, in whole or in part:

- **Keep this file.** Ship `DATA_LICENSES.md` (or a link to it) with every redistribution,
  including in an aggregated or modified form.
- **Keep the attribution.** Name the source for each series you redistribute, as listed in
  §1. A reasonable form is:
  *"Contains NSE market data and NIFTY index constituent history, source NSE (see
  https://github.com/&lt;owner&gt;/nse-alpha-lab/DATA_LICENSES.md)."*
- **Mark changes.** If you modify the data, state clearly what you changed. This matters
  more than usual for this data: a reformatted price series is still a price series, but a
  re-adjusted one is a different claim.
- **Do not present the data as official.** Do not describe the pack, or anything derived
  from it, as an official NSE or NSE Indices product or as endorsed by them.
- **Do not relicense the data as exclusively your own.** You may licence your own additions
  however you like; the underlying data keeps the provenance in this file.

The **code** in this repository is Apache-2.0 and has no such condition — only the data
carries the provenance requirement.

---

## 4. Third-party notices

| Item | Holder | Notes |
|---|---|---|
| NIFTY 50, NIFTY 500, NIFTY MidSmallcap 400, NIFTY Midcap 150, NIFTY Smallcap 250 | NSE Indices Limited | Index names and constituent histories. Index names are trademarks of the index provider. Used here for identification and research only. |
| NSE daily bhavcopy, corporate actions and announcements | National Stock Exchange of India Limited | Public exchange publications. |
| Yahoo Finance `^NSEI` closing levels | Yahoo Finance / its upstream providers | Third-party republication. Used as the primary NIFTY 50 benchmark because it is the only series covering the full 2013–2026 window; cross-checked against the NSE-spliced series. |

No vendor-restricted or subscription-restricted feed is redistributed.

---

## 5. Takedown and corrections

If you own rights in material contained in this pack and believe it should not be
distributed, or if you have found an error in the provenance above, contact:

> **<https://github.com/Kaustubh-Rathi/nse-alpha-lab/issues>**

Please include the file name (or its SHA-256 digest from the pack's `manifest.json`), the
series number from §1, and what you would like done. The Distributor will remove or correct
the affected files and republish the pack with a new version number. The checksums in the
pack manifest make the exact affected release identifiable.

---

## 6. No warranty

The data pack is provided **as is**, without warranty of any kind, express or implied,
including any warranty of accuracy, completeness, timeliness or fitness for a particular
purpose. Market data contains errors in the original publications — symbol changes, bad
prints, missing sessions and corporate-action restatements — and this repository does not
claim to have corrected all of them. Known gaps are recorded in [STATUS.md](STATUS.md).

Nothing in this pack is investment advice. See [DISCLAIMER.md](DISCLAIMER.md).
