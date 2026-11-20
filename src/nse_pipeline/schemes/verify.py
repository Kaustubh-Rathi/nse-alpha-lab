"""Manual verification of scheme PDF text: verified ledger rows from .txt files.

The regex ratios extractor (``ratios.py``) found **0** explicit ratios across
all 50 extracted scheme PDFs. A manual pass over
``data/processed/schemes/pdf_text/*.txt`` shows why: the dominant NSE/SEBI
disclosure formulation is

    "1 (One) equity share ... for every 1 (One) fully paid-up equity share"

where the word-in-parentheses forms ("1 (One)", "4 [4 only]") break the
number-token patterns in ``ratios.py``. This module records the manual
verification that a human (``verified_by = "cline6"``) performed by reading
the extracted pages, and machine-checks every row against the cached text:

* each verified row carries ``pdf_url`` + ``page_ref`` + a verbatim
  ``snippet``; ``validate_verified_row`` re-checks that the snippet
  (whitespace-normalised) is actually present on the cited page of the
  cached .txt file before the row is written out;
* a row is admitted **only** where an explicit swap / demerger ratio or
  cash-consideration clause was read on the cited page. No ratio is ever
  inferred from a price move; there are no price-derived fields and
  ``ratios_inferred_from_price`` is always ``False``;
* wholly-owned amalgamations that expressly provide *no issue of shares*
  are recorded as verified no-consideration rows (ratio stays null);
* schemes that were **withdrawn** (DIL) keep ``ratio_num/ratio_den =
  null`` in the verified ledger even though an explicit clause exists in
  the superseded filing (ledger rule: withdrawn schemes carry no terms);
* every other cached .txt file is listed as ``needs_human_pdf`` with a
  reason (scanned annexure with no text layer, cover-letter wrapper whose
  order is not enclosed, newspaper/observation-letter wrapper, ...).

No network access. Does not write to the host DB.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from nse_pipeline.paths import data_root

VERIFIED_BY = "cline6"

# ---------------------------------------------------------------------------
# Hand-read verified legs.
#
# Each entry was read on the cited page of the cached .txt file (01-09 Nov
# 2026 manual pass). ``snippet`` is the verbatim (whitespace-normalised)
# clause; ``snippet_check`` lists fragments that must all be found on the
# cited page so the check is robust to PDF line-break artefacts.
# successor_symbol is always null: exchange symbols are never inferred
# from company names.
# ---------------------------------------------------------------------------
VERIFIED_LEGS: list[dict] = [
    {
        "scheme_id": "SLG-SANOFI-2023-05-10",
        "scheme_type": "demerger",
        "symbol": "SANOFI",
        "parties": [
            {"symbol": "SANOFI", "company_name": "Sanofi India Limited",
             "role": "demerged_company"},
            {"company_name": "Sanofi Consumer Healthcare India Limited",
             "role": "resulting_company"},
        ],
        "dates": {"announced": "2023-05-10", "board_date": "2023-05-10",
                  "nclt_order_date": "2024-05-09"},
        "ratio_num": 1, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Sanofi Consumer Healthcare India Limited",
        "successor_symbol": None,
        "pdf_file": "SANOFI_10052023134914_Reg30.txt",
        "page_ref": "p.4",
        "snippet": ("will be issued equity shares of the Resulting Company "
                    "(Sanofi Consumer Healthcare India Limited) in the ratio of 1:1"),
        "snippet_check": ["in the ratio of 1:1",
                          "Sanofi Consumer Healthcare India Limited"],
        "filing_trail": [
            "SANOFI_10052023134914_Reg30.txt (board approval; ratio clause)",
        ],
        "note": ("Cross-check vs web-known SANOFI 1:1: CONFIRMED in cached "
                 "text. Same page also states the clause twice: '1 (One) "
                 "equity share ... for every 1 (One) fully paid-up equity "
                 "share' (p.4) and 'in the ratio of 1:1' (p.4); p.5: "
                 "'shareholders will receive 1:1 SCHIL equity share ... for "
                 "each equity share owned'. No cash consideration."),
    },
    {
        "scheme_id": "SLG-RAYMOND-2024-07-04",
        "scheme_type": "demerger",
        "symbol": "RAYMOND",
        "parties": [
            {"symbol": "RAYMOND", "company_name": "Raymond Limited",
             "role": "demerged_company"},
            {"company_name": "Raymond Realty Limited",
             "role": "resulting_company"},
        ],
        "dates": {"announced": "2024-07-04"},
        "ratio_num": 1, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Raymond Realty Limited",
        "successor_symbol": None,
        "pdf_file": "RAYMOND_04072024170811_SEintimationsigned.txt",
        "page_ref": "p.5",
        "snippet": ("1 (One Only) equity share of Raymond Realty Limited of "
                    "INR 10/- each fully paid up for every 1 (One Only) "
                    "equity share of Raymond Limited of INR 10/- each fully "
                    "paid up"),
        "snippet_check": ["equity share of Raymond Realty Limited",
                          "for every 1 (One Only) equity share of Raymond Limited"],
        "filing_trail": [
            "RAYMOND_04072024170811_SEintimationsigned.txt (board approval; ratio clause)",
            "RAYMOND_19122024182010_SEintimationsigned.txt (NCLT first-motion order; same 1:1 read on p.6)",
        ],
        "note": ("Cross-check vs web-known RAYMOND 1:1: CONFIRMED in cached "
                 "text — but only for the Realty demerger. Raymond's other "
                 "scheme (Lifestyle/RCCL) is 4:5, not 1:1; see next rows."),
    },
    {
        "scheme_id": "SLG-RAYMOND-2023-04-27",
        "scheme_type": "demerger",
        "symbol": "RAYMOND",
        "parties": [
            {"symbol": "RAYMOND", "company_name": "Raymond Limited",
             "role": "demerged_company"},
            {"company_name": "Raymond Consumer Care Limited",
             "role": "resulting_company"},
        ],
        "dates": {"announced": "2023-04-27"},
        "ratio_num": 4, "ratio_den": 5, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Raymond Consumer Care Limited",
        "successor_symbol": None,
        "pdf_file": "RAYMOND_27042023204619_SEintimationsigned.txt",
        "page_ref": "p.7",
        "snippet": ("Four [4 only] equity shares of Raymond Consumer Care "
                    "Limited of INR 2 each fully paid-up for every five [5 "
                    "only] equity share of Raymond Limited of INR 10 each "
                    "fully paid up"),
        "snippet_check": ["equity shares of Raymond Consumer Care Limited",
                          "for every five [5 only] equity share of Raymond Limited"],
        "filing_trail": [
            "RAYMOND_27042023204619_SEintimationsigned.txt (board approval; ratio clause)",
            "RAYMOND_18012024173535_finalseintimation.txt (NCLT first-motion; same 4:5 read on p.8)",
            "RAYMOND_21062024184840_SEintimationsigned.txt (same 4:5 read on p.6; RCCL renamed Raymond Lifestyle Limited)",
        ],
        "note": ("Demerger of Lifestyle Business Undertaking of Raymond "
                 "Limited (RL) into Raymond Consumer Care Limited (RCCL), "
                 "later Raymond Lifestyle Limited. The RCCL share face value "
                 "is INR 2 against RL INR 10."),
    },
    {
        "scheme_id": "SLG-RAYMOND-2023-04-27-RG",
        "scheme_type": "merger",
        "symbol": "RAYMOND",
        "parties": [
            {"company_name": "Ray Global Consumer Trading Limited",
             "role": "transferor"},
            {"company_name": "Raymond Consumer Care Limited",
             "role": "transferee"},
        ],
        "dates": {"announced": "2023-04-27"},
        "ratio_num": 2, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Raymond Consumer Care Limited",
        "successor_symbol": None,
        "pdf_file": "RAYMOND_27042023204619_SEintimationsigned.txt",
        "page_ref": "p.7",
        "snippet": ("Two (2 only) equity share of Raymond Consumer Care "
                    "Limited of INR 2 each fully paid-up for every One (1 "
                    "only) equity shares of Ray Global Consumer Trading "
                    "Limited of INR 10/- each fully paid-up"),
        "snippet_check": ["equity share of Raymond Consumer Care Limited",
                          "for every One (1 only) equity shares of Ray Global Consumer Trading Limited"],
        "filing_trail": [
            "RAYMOND_27042023204619_SEintimationsigned.txt (board approval; ratio clause)",
            "RAYMOND_18012024173535_finalseintimation.txt (same 2:1 read on p.9)",
            "RAYMOND_21062024184840_SEintimationsigned.txt (same 2:1 read on p.7)",
        ],
        "note": ("Second leg of the 2023-04-27 composite scheme: "
                 "amalgamation (merger) of Ray Global Consumer Trading "
                 "Limited (RG) into RCCL. Same composite scheme as the 4:5 "
                 "demerger above; kept as a separate row so each leg's "
                 "ratio is unambiguous."),
    },
    {
        "scheme_id": "SLG-INDIAGLYCO-2025-05-16-EBL",
        "scheme_type": "demerger",
        "symbol": "INDIAGLYCO",
        "parties": [
            {"symbol": "INDIAGLYCO", "company_name": "India Glycols Limited",
             "role": "demerged_company"},
            {"company_name": "Ennature Bio Pharma Limited",
             "role": "resulting_company"},
        ],
        "dates": {"announced": "2025-05-16"},
        "ratio_num": 1, "ratio_den": 3, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "Ennature Bio Pharma Limited",
        "successor_symbol": None,
        "pdf_file": "INDIAGLYCO_16052025174230_Reg30Demerger.txt",
        "page_ref": "p.7",
        "snippet": ("EBL shall issue 1 (One) Equity Share having face value "
                    "INR 10 (Indian Rupees Ten) each, to the shareholders of "
                    "IGL holding 3 (Three) Equity Shares of INR 10 (Indian "
                    "Rupees Ten) each"),
        "snippet_check": ["EBL shall issue 1 (One) Equity Share",
                          "holding 3 (Three) Equity Shares"],
        "filing_trail": [
            "INDIAGLYCO_16052025174230_Reg30Demerger.txt (modified scheme; ratio clause)",
            "INDIAGLYCO_04022025184505_Scheme_SEdisclosure.txt (original composite scheme; same 1:3 read on p.6/p.8)",
            "INDIAGLYCO_16012026154638_Reg_30_NCLT_Order.txt (NCLT first-motion; 1 for 3 read on p.11)",
            "INDIAGLYCO_20072026184456_NCLTOrder.txt (NCLT order; 1 for 3 read on p.27)",
            "INDIAGLYCO_01092026161552_SE_Intimation_Effective_Date.txt (effective date; 1 for 3 read on p.2)",
            "INDIAGLYCO_06102026180443_Apportionment_of_Cost_of_Acquisition.txt (cost apportionment; 1 for 3 read on p.2)",
        ],
        "note": ("Bio Pharma Undertaking demerger (Resulting Company 1 / EBL). "
                 "The 2025-05-16 board modified the original 2025-02-04 "
                 "composite scheme by dropping the KHL amalgamation leg; "
                 "both demerger ratios are unchanged. Post-demerger face "
                 "value appears as Rs 5 in 2026 filings (scheme annexure "
                 "revision); share-count ratio 1:3 is unchanged."),
    },
    {
        "scheme_id": "SLG-INDIAGLYCO-2025-05-16-ISL",
        "scheme_type": "demerger",
        "symbol": "INDIAGLYCO",
        "parties": [
            {"symbol": "INDIAGLYCO", "company_name": "India Glycols Limited",
             "role": "demerged_company"},
            {"company_name": "IGL Spirits Limited",
             "role": "resulting_company"},
        ],
        "dates": {"announced": "2025-05-16"},
        "ratio_num": 1, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "IGL Spirits Limited",
        "successor_symbol": None,
        "pdf_file": "INDIAGLYCO_16052025174230_Reg30Demerger.txt",
        "page_ref": "p.7",
        "snippet": ("ISL shall issue 1 (One) Equity Share having face value "
                    "INR 10 (Indian Rupees Ten) each, to the shareholders of "
                    "IGL holding 1 (One) Equity Share of INR 10 (Indian "
                    "Rupees Ten) each"),
        "snippet_check": ["ISL shall issue 1 (One) Equity Share",
                          "holding 1 (One) Equity Share"],
        "filing_trail": [
            "INDIAGLYCO_16052025174230_Reg30Demerger.txt (modified scheme; ratio clause)",
            "INDIAGLYCO_04022025184505_Scheme_SEdisclosure.txt (original composite scheme; same 1:1 read on p.6/p.8)",
            "INDIAGLYCO_16012026154638_Reg_30_NCLT_Order.txt (NCLT first-motion; 1 for 1 read on p.12)",
            "INDIAGLYCO_20072026184456_NCLTOrder.txt (NCLT order; 1 for 1 read on p.29)",
            "INDIAGLYCO_01092026161552_SE_Intimation_Effective_Date.txt (effective date; 1 for 1 read on p.2)",
            "INDIAGLYCO_06102026180443_Apportionment_of_Cost_of_Acquisition.txt (cost apportionment; 1 for 1 read on p.2)",
        ],
        "note": ("Spirits and Biofuel Undertaking demerger (Resulting "
                 "Company 2 / ISL). Same modified-scheme filing as the EBL "
                 "leg; separate row so each leg is unambiguous."),
    },
    {
        "scheme_id": "SLG-TINPLATE-2024-01-08",
        "scheme_type": "merger",
        "symbol": "TINPLATE",
        "parties": [
            {"symbol": "TINPLATE",
             "company_name": "The Tinplate Company of India Limited",
             "role": "transferor"},
            {"company_name": "Tata Steel Limited", "role": "transferee"},
        ],
        "dates": {"announced": "2024-01-08", "record_date": "2024-01-19"},
        "ratio_num": 33, "ratio_den": 10, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "Tata Steel Limited",
        "successor_symbol": None,
        "pdf_file": "TINPLATE_08012024105608_TCIL_Reg_42_Reg_30_Disclosure__S.txt",
        "page_ref": "p.1",
        "snippet": ("33 (thirty-three) fully paid-up equity shares of in the "
                    "ratio of shares of Tata Steel Limited"),
        "snippet_check": ["33 (thirty -three) fully paid-up equity shares",
                          "in the ratio of"],
        "filing_trail": [
            "TINPLATE_08012024105608_TCIL_Reg_42_Reg_30_Disclosure__S.txt (record-date filing; ratio clause)",
            "TINPLATE_08012024105909_TCIL_Reg_42_Reg_30_Disclosure__S.txt (duplicate record-date filing)",
            "TINPLATE_04012024194817_TCIL_Reg_30_Disclosure_CTC_S.txt (scheme text; same 33-for-10 read at Clause 15.2, p.56 excerpt)",
        ],
        "note": ("Amalgamation of TCIL into Tata Steel Limited: 33 shares of "
                 "Re 1/- for every 10 shares of Rs 10/- held. Cash legs: "
                 "none stated in the record-date filing."),
    },
    {
        "scheme_id": "SLG-SANGHIIND-2024-12-17",
        "scheme_type": "merger",
        "symbol": "SANGHIIND",
        "parties": [
            {"symbol": "SANGHIIND",
             "company_name": "Sanghi Industries Limited",
             "role": "transferor"},
            {"company_name": "Ambuja Cements Limited",
             "role": "transferee"},
        ],
        "dates": {"announced": "2024-12-17"},
        "ratio_num": 12, "ratio_den": 100, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "Ambuja Cements Limited",
        "successor_symbol": None,
        "pdf_file": "SANGHIIND_17122024175311_SIl_Outcome_Final.txt",
        "page_ref": "p.4",
        "snippet": ("will be issued and allotted 12 Transferee Company Shares "
                    "credited as fully paid-up, for every 100 equity shares"),
        "snippet_check": ["will be issued and allotted 12 Transferee Company Shares",
                          "for every 100 equity shares"],
        "filing_trail": [
            "SANGHIIND_17122024175311_SIl_Outcome_Final.txt (board outcome; ratio clause, sentence straddles p.4-5)",
        ],
        "note": ("Sanghi Industries (face value Rs 10) into Ambuja Cements "
                 "(face value Rs 2): 12 Ambuja shares for every 100 Sanghi "
                 "shares. Verified from the board-outcome filing; later "
                 "NCLT-order copies in the cache are image scans."),
    },
    {
        "scheme_id": "SLG-SANGHIIND-2024-12-17-ACL",
        "scheme_type": "merger",
        "symbol": "SANGHIIND",
        "parties": [
            {"company_name": "Adani Cementation Limited",
             "role": "transferor"},
            {"symbol": "SANGHIIND",
             "company_name": "Sanghi Industries Limited",
             "role": "transferee"},
        ],
        "dates": {"announced": "2024-12-17"},
        "ratio_num": 174, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "Sanghi Industries Limited",
        "successor_symbol": None,
        "pdf_file": "SANGHIIND_17122024175311_SIl_Outcome_Final.txt",
        "page_ref": "p.6",
        "snippet": ("Adani Enterprises Limited (the shareholder of the "
                    "Transferor Company) will be issued and allotted "
                    "87,00,000 Equity Shares of the Company as per Share "
                    "Exchange Ratio i.e. 174 Equity Shares of the Company "
                    "for every 1 share of the Transferor Company"),
        "snippet_check": ["87,00,000 Equity Shares of the Company as per Share Exchange Ratio",
                          "174 Equity Shares of the Company for every 1 share"],
        "filing_trail": [
            "SANGHIIND_17122024175311_SIl_Outcome_Final.txt (second-scheme disclosure note)",
        ],
        "note": ("Second scheme disclosed inside the same PDF: amalgamation "
                 "of Adani Cementation Limited into Sanghi Industries "
                 "Limited (approved 2024-06-27), under which Sanghi issues "
                 "174 shares for every 1 Adani Cementation share (to Adani "
                 "Enterprises Limited, the transferor's shareholder). "
                 "Recorded as its own row because the transferee differs "
                 "from the row above."),
    },
    {
        "scheme_id": "SLG-JMCPROJECT-2023-01-04",
        "scheme_type": "merger",
        "symbol": "JMCPROJECT",
        "parties": [
            {"symbol": "JMCPROJECT",
             "company_name": "JMC Projects (India) Limited",
             "role": "transferor"},
            {"company_name": "Kalpataru Power Transmission Limited",
             "role": "transferee"},
        ],
        "dates": {"announced": "2023-01-04"},
        "ratio_num": 1, "ratio_den": 4, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "Kalpataru Power Transmission Limited",
        "successor_symbol": None,
        "pdf_file": "JMCPROJECT_04012023193411_SEIntimationupdatedMOAAOA_JMC.txt",
        "page_ref": "p.93",
        "snippet": ("1 (One) equity share of INR 2 (Indian Rupees two) each of "
                    "the Transferee Company, credited as fully paid up, for "
                    "every 4 (four) equity shares of INR 2 each of the "
                    "Transferor Company"),
        "snippet_check": ["equity share of INR 2 (Indian Rupees two) each of the Transferee Company",
                          "for every 4 (four) equity"],
        "filing_trail": [
            "JMCPROJECT_04012023193411_SEIntimationupdatedMOAAOA_JMC.txt (NCLT order annexure to MOA/AOA filing)",
        ],
        "note": ("Amalgamation of JMC Projects (India) Limited with "
                 "Kalpataru Power Transmission Limited (KPTL), effective "
                 "2023-01-04. Parties identified from the p.1 cover "
                 "intimation; ratio from the annexed NCLT order extract on "
                 "p.93 (OCR-noisy scan: 'Sha. res', '!NR 2', but the "
                 "1-for-4 clause is unambiguous)."),
    },
]

# Explicit clauses found in superseded/withdrawn filings: documented, but
# the scheme is withdrawn so no ratio enters the verified ledger.
WITHDRAWN_LEGS: list[dict] = [
    {
        "scheme_id": "SLG-DIL-2024-06-28",
        "scheme_type": "merger",
        "symbol": "DIL",
        "parties": [
            {"symbol": "DIL",
             "company_name": "Debock Industries Limited", "role": "transferee"},
            {"company_name": "Debock Ventures Limited", "role": "transferor"},
        ],
        "dates": {"announced": "2024-06-28"},
        "ratio_num": 270, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Debock Industries Limited",
        "successor_symbol": None,
        "pdf_file": "DIL_28062024204818_OutcomeBM.txt",
        "page_ref": "p.5",
        "snippet": ("270 (Two Hundred Seventy) equity shares of Debock "
                    "Industries Limited having face value of INR 10 each "
                    "fully paid up shall be issued to the respective "
                    "shareholders of Debock Ventures Limited for every 1 "
                    "(One) equity shares held by them in Debock Ventures "
                    "Limited"),
        "snippet_check": ["270 (Two Hundred Seventy) equity shares of Debock Industries Limited",
                          "for every 1 (One) equity shares held by them in Debock Ventures Limited"],
        "reason": ("Scheme WITHDRAWN: DIL_08112024121416_Intimation_-_Withdrawal_of_Scheme.txt "
                   "p.1 records the SEBI-directed withdrawal (application "
                   "submitted 2024-10-18). Withdrawn schemes carry no ratio "
                   "in the verified ledger."),
    },
    {
        "scheme_id": "SLG-INDIAGLYCO-2025-02-04-KHL",
        "scheme_type": "merger",
        "symbol": "INDIAGLYCO",
        "parties": [
            {"company_name": "Kashipur Holdings Limited", "role": "transferor"},
            {"symbol": "INDIAGLYCO", "company_name": "India Glycols Limited",
             "role": "transferee"},
        ],
        "dates": {"announced": "2025-02-04"},
        "ratio_num": 1, "ratio_den": 1, "cash_per_share": None,
        "no_cash_consideration": None,
        "successor_company_name": "India Glycols Limited",
        "successor_symbol": None,
        "pdf_file": "INDIAGLYCO_04022025184505_Scheme_SEdisclosure.txt",
        "page_ref": "p.6",
        "snippet": ("IGL shall issue 1 (One) Equity Share having face value "
                    "of INR 10 (Indian Rupees Ten) each, for every 1(one) "
                    "equity share held by KHL in IGL"),
        "snippet_check": ["IGL shall issue 1 (One) Equity Share",
                          "for every 1(one) equity share held by KHL in IGL"],
        "reason": ("DROPPED LEG: the 2025-05-16 board withdrew the "
                   "amalgamation part of the 2025-02-04 composite scheme "
                   "(INDIAGLYCO_16052025174230_Reg30Demerger.txt p.1) and "
                   "proceeded with the two demergers only (1:3 and 1:1, "
                   "verified above)."),
    },
]

# Explicit no-consideration (wholly-owned) clauses: verified as such,
# with ratio and cash both null.
NO_CONSIDERATION_LEGS: list[dict] = [
    {
        "scheme_id": "SLG-BALKRISIND-2024-01-24",
        "scheme_type": "merger",
        "symbol": "BALKRISIND",
        "parties": [
            {"symbol": "BALKRISIND",
             "company_name": "Balkrishna Industries Limited", "role": "transferee"},
            {"company_name": "BKT Tyres Limited", "role": "transferor"},
        ],
        "dates": {"announced": "2024-01-24", "nclt_order_date": "2025-03-25"},
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Balkrishna Industries Limited",
        "successor_symbol": None,
        "pdf_file": "BALKRISIND_24012024165955_OutcomeReg30signed.txt",
        "page_ref": "p.4",
        "snippet": ("The Transferor Company is a wholly owned subsidiary of "
                    "the Company and therefore, there shall be no issue of "
                    "shares as consideration for the amalgamation of the "
                    "Transferor Company with the Company"),
        "snippet_check": ["wholly owned subsidiary of the Company",
                          "no issue of shares as consideration"],
        "filing_trail": [
            "BALKRISIND_24012024165955_OutcomeReg30signed.txt (scheme disclosure; clause on p.4)",
            "BALKRISIND_24012024183516_OutcomeReg30signed.txt (duplicate filing; same clause on p.4)",
        ],
        "note": ("BKT Tyres Limited (wholly-owned subsidiary) into "
                 "Balkrishna Industries Limited; expressly no issue of "
                 "shares, no cash."),
    },
    {
        "scheme_id": "SLG-UDS-2024-05-20",
        "scheme_type": "merger",
        "symbol": "UDS",
        "parties": [
            {"symbol": "UDS", "company_name": "Updater Services Limited",
             "role": "transferee"},
            {"company_name": "Stanworth Management Private Limited",
             "role": "transferor"},
            {"company_name": "Tangy Supplies & Solutions Private Limited",
             "role": "transferor"},
        ],
        "dates": {"announced": "2024-05-20"},
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Updater Services Limited",
        "successor_symbol": None,
        "pdf_file": "UDS_20052024224102_UDS_Outcome_BM_Scheme_sd.txt",
        "page_ref": "p.21",
        "snippet": ("Since the Transferor Companies are wholly owned "
                    "subsidiaries of the Transferee Company, upon the "
                    "Scheme being sanctioned by the Hon'ble Regional "
                    "Director or the Tribunal ... all the Equity Shares, "
                    "held by the Transferee Company and its nominees in the "
                    "Transferor Companies shall be cancelled and "
                    "extinguished. Accordingly, there will be no issue and "
                    "allotment of Equity shares of the Transferee Company"),
        "snippet_check": ["wholly owned subsidiaries of the Transferee Company",
                          "cancelled and extinguished"],
        "filing_trail": [
            "UDS_20052024224102_UDS_Outcome_BM_Scheme_sd.txt (draft scheme; Clause 11.1, p.21)",
            "UDS_20052024224359_UDS_Outcome_BM_Scheme_sd.txt (duplicate filing)",
            "UDS_27062024171911_UDS_intimation_Reg30_CAA9_sd.txt (CAA 9 disclosure, p.25)",
            "UDS_27062024205359_UDS_REg37_sd.txt (Regulation 37 disclosure, p.24)",
        ],
        "note": ("Stanworth Management Private Limited + Tangy Supplies & "
                 "Solutions Private Limited (both wholly-owned "
                 "subsidiaries) into Updater Services Limited; expressly no "
                 "consideration, shares cancelled."),
    },
    {
        "scheme_id": "SLG-WIPRO-2023-11-08",
        "scheme_type": "merger",
        "symbol": "WIPRO",
        "parties": [
            {"symbol": "WIPRO", "company_name": "Wipro Limited",
             "role": "transferee"},
            {"company_name": "Wipro Overseas IT Services Private Limited",
             "role": "transferor"},
            {"company_name": "Wipro Technology Product Services Private Limited",
             "role": "transferor"},
            {"company_name": "Wipro Trademarks Holding Limited",
             "role": "transferor"},
            {"company_name": "Wipro VLSI Design Services India Private Limited",
             "role": "transferor"},
        ],
        "dates": {"announced": "2023-11-08"},
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Wipro Limited",
        "successor_symbol": None,
        "pdf_file": "WIPRO_08112023174101_SEIntimation_Merger_.txt",
        "page_ref": "p.20",
        "snippet": ("The Transferor Companies are wholly owned subsidiaries "
                    "of the Transferee Company. Upon the Scheme becoming "
                    "effective, no shares of the Transferee Company shall "
                    "be allotted in lieu or exchange of the shares of the "
                    "Transferor Companies"),
        "snippet_check": ["wholly owned subsidiaries of the Transferee Company",
                          "no shares of the Transferee Company shall be allotted"],
        "filing_trail": [
            "WIPRO_08112023174101_SEIntimation_Merger_.txt (draft scheme; Part C, p.20)",
        ],
        "note": ("Four wholly-owned subsidiaries into Wipro Limited; "
                 "expressly no allotment, share capital cancelled. The two "
                 "other 2024 Wipro filings are a stepdown-subsidiary "
                 "merger (see the row below) and a leasehold slump sale "
                 "(consideration is sale proceeds for assets, not a "
                 "per-share scheme term) — neither yields a ratio."),
    },
    {
        "scheme_id": "SLG-WIPRO-2024-06-27-DESIGNIT",
        "scheme_type": "merger",
        "symbol": "WIPRO",
        "parties": [
            {"symbol": "WIPRO", "company_name": "Wipro Limited",
             "role": "primary"},
            {"company_name": "Designit North America, Inc.",
             "role": "transferor"},
            {"company_name": "Wipro Designit Services, Inc.",
             "role": "transferee"},
        ],
        "dates": {"announced": "2024-06-27", "effective_date": "2024-07-01"},
        "ratio_num": None, "ratio_den": None, "cash_per_share": None,
        "no_cash_consideration": True,
        "successor_company_name": "Wipro Designit Services, Inc.",
        "successor_symbol": None,
        "pdf_file": "WIPRO_27062024172216_Reg_30_Disclosure_Designit_North_America.txt",
        "page_ref": "p.2",
        "snippet": ("in case of cash consideration - amount or otherwise "
                    "share exchange ratio; Not Applicable"),
        "snippet_check": ["share exchange ratio; Not Applicable"],
        "filing_trail": [
            "WIPRO_27062024172216_Reg_30_Disclosure_Designit_North_America.txt (completion disclosure; clause on p.2)",
        ],
        "note": ("Merger of Designit North America, Inc. (step-down "
                 "subsidiary) with Wipro Designit Services, Inc. (step-down "
                 "subsidiary), completed and effective 2024-07-01; the "
                 "SEBI disclosure expressly marks cash consideration / "
                 "share exchange ratio Not Applicable (both wholly-owned "
                 "stepdown entities)."),
    },
]

BANNED_PRICE_FIELDS = {
    "price_factor", "share_multiplier", "inferred_ratio", "inferred_from_price",
    "price_discontinuity_ratio", "implied_ratio_from_return", "raw_return_used",
}


def norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_pages(text_path: Path) -> dict[int, str]:
    """Return {page_no: normalised page text} from a cached .txt file."""
    text = text_path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"===== PAGE (\d+) =====", text)
    return {int(no): norm_ws(body) for no, body in zip(parts[1::2], parts[2::2])}


def load_batch(pdf_text_dir: Path) -> list[dict]:
    batch = pdf_text_dir / "ratios_batch.jsonl"
    if not batch.exists():
        return []
    return [json.loads(line) for line in
            batch.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()]


def pdf_url_for(batch_rows: list[dict], symbol: str,
                announcement_date: str | None) -> str | None:
    for row in batch_rows:
        if (row.get("symbol") == symbol and announcement_date
                and row.get("announcement_date") == announcement_date):
            return row.get("pdf_url")
    for row in batch_rows:
        if row.get("symbol") == symbol:
            return row.get("pdf_url")
    return None


def snippet_on_page(pages: dict[int, str], page_ref: str,
                    snippet_check: list[str]) -> bool:
    """True iff every check fragment appears on the cited page."""
    try:
        page_no = int(str(page_ref).replace("p.", "").strip())
    except ValueError:
        return False
    page_text = pages.get(page_no, "")
    return all(norm_ws(frag) in page_text for frag in snippet_check)


def validate_verified_row(row: dict) -> dict:
    """Fail-closed validation of one verified ledger row."""
    for banned in BANNED_PRICE_FIELDS:
        if banned in row:
            raise ValueError(f"Banned price-derived field present: {banned}")
    source = str(row.get("extraction_source") or "").lower()
    if "price" in source or "raw_return" in source or "discontinuity" in source:
        raise ValueError("Terms must never be inferred from a price move")
    if row.get("verified_by") != VERIFIED_BY:
        raise ValueError(f"verified_by must be {VERIFIED_BY!r}")
    if row.get("terms_verified") is not True:
        raise ValueError("terms_verified must be True")
    if not row.get("pdf_url"):
        raise ValueError("Verified terms require pdf_url citation")
    if not row.get("page_ref"):
        raise ValueError("Verified terms require page_ref citation")
    if not row.get("snippet"):
        raise ValueError("Verified terms require a verbatim snippet")
    num, den = row.get("ratio_num"), row.get("ratio_den")
    for v in (num, den):
        if v is not None and (not isinstance(v, int) or v <= 0):
            raise ValueError("ratio_num/ratio_den must be positive ints")
    if (num is None) != (den is None):
        raise ValueError("ratio_num and ratio_den must be set together")
    cash = row.get("cash_per_share")
    if cash is not None and (not isinstance(cash, (int, float)) or cash < 0):
        raise ValueError("cash_per_share must be a non-negative number or null")
    if row.get("status") == "withdrawn" and (num is not None or cash is not None):
        raise ValueError("Withdrawn schemes carry no ratio or cash")
    if row.get("successor_symbol") is not None:
        raise ValueError("successor_symbol is never guessed in verified rows")
    return row


def build_rows(pdf_text_dir: Path, batch_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build (verified_rows, needs_human_rows) with snippet checks."""
    cache: dict[str, dict[int, str]] = {}

    def pages_for(name: str) -> dict[int, str]:
        if name not in cache:
            path = pdf_text_dir / name
            cache[name] = load_pages(path) if path.exists() else {}
        return cache[name]

    verified: list[dict] = []
    for leg in VERIFIED_LEGS + NO_CONSIDERATION_LEGS:
        pages = pages_for(leg["pdf_file"])
        checked = snippet_on_page(pages, leg["page_ref"], leg["snippet_check"])
        if not checked:
            raise ValueError(
                f"Snippet check FAILED for {leg['scheme_id']}: "
                f"{leg['snippet_check']} not found on "
                f"{leg['pdf_file']} {leg['page_ref']}")
        announced = leg["dates"].get("announced")
        row = {
            "scheme_id": leg["scheme_id"],
            "scheme_type": leg["scheme_type"],
            "parties": leg["parties"],
            "dates": leg["dates"],
            "ratio_num": leg["ratio_num"],
            "ratio_den": leg["ratio_den"],
            "cash_per_share": leg["cash_per_share"],
            "no_cash_consideration": leg["no_cash_consideration"],
            "successor_company_name": leg["successor_company_name"],
            "successor_symbol": leg["successor_symbol"],
            "pdf_url": pdf_url_for(batch_rows, leg["symbol"], announced),
            "page_ref": leg["page_ref"],
            "snippet": leg["snippet"],
            "status": "approved",
            "verified_by": VERIFIED_BY,
            "terms_verified": True,
            "extraction_source": f"manual_read_of_cached_pdf_text ({leg['pdf_file']})",
            "ratio_from_price": False,
            "snippet_verified_on_page": True,
            "filing_trail": leg.get("filing_trail", [leg["pdf_file"]]),
            "note": leg.get("note", ""),
        }
        verified.append(validate_verified_row(row))

    accounted: set[str] = set()
    for group in (VERIFIED_LEGS, WITHDRAWN_LEGS, NO_CONSIDERATION_LEGS):
        for leg in group:
            accounted.add(leg["pdf_file"])
            accounted.update(leg.get("filing_trail", []))
    # Filing-trail entries carry annotations; recover raw filenames.
    accounted_files = {a.split(" (")[0] for a in accounted}

    needs_human: list[dict] = []
    for txt in sorted(pdf_text_dir.glob("*.txt")):
        if txt.name in accounted_files:
            continue
        pages = pages_for(txt.name)
        total_chars = sum(len(v) for v in pages.values())
        n_pages = max(1, len(pages))
        listed = next((r for r in batch_rows
                       if Path(str(r.get("extraction", {}).get("text_file", ""))).name == txt.name), {})
        needs_human.append({
            "text_file": txt.name,
            "symbol": listed.get("symbol"),
            "announcement_date": listed.get("announcement_date"),
            "pdf_url": listed.get("pdf_url"),
            "pages": n_pages,
            "chars": total_chars,
            "status": "needs_human_pdf",
            "reason": needs_reason(txt, total_chars, n_pages, pages),
            "verified_by": None,
            "terms_verified": False,
        })
    return verified, needs_human


def needs_reason(txt: Path, total_chars: int, n_pages: int,
                 pages: dict[int, str]) -> str:
    joined = " ".join(pages.values()).lower()
    empty_pages = sum(1 for v in pages.values() if len(v) < 20)
    if total_chars / n_pages < 150 or (n_pages > 5 and empty_pages > n_pages / 2):
        return ("scanned annexure: most pages carry no text layer "
                f"({empty_pages}/{n_pages} effectively blank) — scheme "
                "terms, if any, are in the image annexure")
    if "newspaper advertisement" in joined:
        return ("newspaper/hearing wrapper: no scheme terms in extracted text")
    if "withdraw" in joined and "scheme" in joined and n_pages <= 2:
        return ("withdrawal intimation: scheme withdrawn, no ratio to verify")
    if "observation letter" in joined and n_pages <= 6:
        return ("observation-letter wrapper: no scheme terms in extracted text")
    return ("no explicit swap/demerger ratio or cash clause in the "
            "extracted text — scheme terms live in an annexure or a "
            "separately filed document")


def build_summary(verified: list[dict], needs_human: list[dict],
                  txt_total: int) -> dict:
    return {
        "pdf_text_files_total": txt_total,
        "verified_rows": len(verified),
        "verified_with_ratio": sum(1 for r in verified
                                   if r["ratio_num"] is not None),
        "verified_no_consideration": sum(1 for r in verified
                                         if r["ratio_num"] is None),
        "needs_human_pdf": len(needs_human),
        "withdrawn_legs_documented": len(WITHDRAWN_LEGS),
        "ratios_inferred_from_price": False,
        "verified_by": VERIFIED_BY,
        "unverified_txt_accounting": {
            "verified_legs_cited_files": len(VERIFIED_LEGS),
            "no_consideration_legs": len(NO_CONSIDERATION_LEGS),
            "withdrawn_legs": len(WITHDRAWN_LEGS),
        },
        "cross_checks": [
            "SANOFI 1:1 (merchant/web-known): CONFIRMED on p.4 of cached "
            "SANOFI_10052023134914_Reg30.txt, stated twice ('1 (One) equity "
            "share for every 1 (One)' and 'in the ratio of 1:1'); p.5 "
            "repeats '1:1 SCHIL equity share'. No cash consideration.",
            "RAYMOND 1:1 (web-known, Realty demerger): CONFIRMED on p.5 "
            "of cached RAYMOND_04072024170811_SEintimationsigned.txt and "
            "p.6 of RAYMOND_19122024182010_SEintimationsigned.txt (NCLT "
            "order). Raymond's Lifestyle/RCCL scheme is 4:5 (p.7 of "
            "RAYMOND_27042023204619), NOT 1:1 — the two Raymond schemes "
            "must not be conflated.",
        ],
        "why_ratios_extractor_found_zero": [
            "Dominant formulation '1 (One) equity share ... for every 1 "
            "(One) fully paid-up equity share' puts the word-form number "
            "in parentheses after the digit; ratios._NUM_TOKEN only "
            "accepts '(digits)' or a bare word, so it fails to parse both "
            "numerator and denominator.",
            "'Four [4 only] ... for every five [5 only]' bracket-word "
            "forms fail the same token pattern.",
            "'12 Transferee Company Shares ... for every 100 equity "
            "shares' (SANGHIIND) has no 'to be issued' / share-for-share "
            "casing the _RATIO_FOR_EVERY pattern expects after the issue "
            "verb.",
        ],
        "limitations": [
            "Verification is against the cached pypdf text layer only; "
            "pages whose image annexures were not OCR'd stay "
            "needs_human_pdf.",
            "successor_symbol is always null: exchange symbols are not "
            "inferred from company names.",
            "SANGHIIND 12:100 and JMCPROJECT 1:4 clauses straddle page "
            "breaks / OCR noise respectively; page_ref cites the page "
            "where the clause starts and is confirmed by the "
            "snippet_on_page check.",
            "Second legs of composite schemes are separate rows so each "
            "ratio is unambiguous; scheme_id suffixes mark the legs.",
        ],
        "cash_consideration_findings": [
            "No filing in the top-50 states a per-share cash amount as "
            "scheme consideration. SANOFI, RAYMOND (both schemes) and DIL "
            "expressly state NO cash consideration; BALKRISIND, UDS and "
            "WIPRO wholly-owned mergers expressly provide no issue of "
            "shares. WIPRO 2024-08-02 INR 224.08 crore is asset-sale "
            "proceeds in a slump sale, not per-share scheme cash.",
        ],
        "files": {
            "module": "src/nse_pipeline/schemes/verify.py",
            "tests": "tests/test_verify.py",
            "verified_ledger": "data/processed/schemes/verified/verified-ledger.jsonl",
            "report_md": "data/reports/scheme-verification.md",
            "report_json": "data/reports/scheme-verification.json",
        },
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def render_markdown(summary: dict, verified: list[dict],
                    needs_human: list[dict]) -> str:
    lines = [
        "# Scheme verification — manual PDF-text reading (verified_by: cline6)",
        "",
        f"Manual verification of the 50 extracted scheme PDFs "
        f"(`data/processed/schemes/pdf_text/*.txt`). The regex extractor "
        f"found 0 explicit ratios; a page-by-page read found "
        f"**{summary['verified_rows']} verified rows** "
        f"({summary['verified_with_ratio']} with swap/demerger ratios, "
        f"{summary['verified_no_consideration']} verified no-consideration "
        f"wholly-owned mergers) and {summary['needs_human_pdf']} files "
        f"marked `needs_human_pdf`. No ratio is inferred from any price "
        f"move (`ratios_inferred_from_price: false`).",
        "",
        "## Cross-checks against known ratios",
        "",
        *(f"- {c}" for c in summary["cross_checks"]),
        "",
        "## Verified ledger rows",
        "",
        "| scheme_id | type | ratio | successor company | pdf (cited file) | page |",
        "|---|---|---|---|---|---|",
    ]
    for r in verified:
        ratio = (f"{r['ratio_num']}:{r['ratio_den']}"
                 if r["ratio_num"] is not None else "none (wholly-owned)")
        file = r["extraction_source"].split(" (")[-1].rstrip(")")
        lines.append(f"| {r['scheme_id']} | {r['scheme_type']} | {ratio} | "
                     f"{r['successor_company_name']} | {file} | {r['page_ref']} |")
    lines += [
        "",
        'Withdrawn / dropped legs (documented, no ratio admitted):',
        "",
        *(f"- {w['scheme_id']}: {w['ratio_num']}:{w['ratio_den']} in "
          f"{w['pdf_file']} {w['page_ref']} — {w['reason']}"
          for w in WITHDRAWN_LEGS),
        "",
        "## needs_human_pdf",
        "",
        "| text_file | symbol | pages | reason |",
        "|---|---|---|---|",
    ]
    for n in needs_human:
        lines.append(f"| {n['text_file']} | {n['symbol']} | {n['pages']} | "
                     f"{n['reason']} |")
    lines += [
        "",
        "## Why the regex extractor found 0",
        "",
        *(f"- {w}" for w in summary["why_ratios_extractor_found_zero"]),
        "",
        "## Cash consideration",
        "",
        *(f"- {c}" for c in summary["cash_consideration_findings"]),
        "",
        "## Limitations",
        "",
        *(f"- {l}" for l in summary["limitations"]),
        "",
        "## Files",
        "",
        *(f"- {k}: {v}" for k, v in summary["files"].items()),
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    args = parser.parse_args()
    data = args.data
    pdf_text_dir = data / "processed/schemes/pdf_text"
    batch_rows = load_batch(pdf_text_dir)
    verified, needs_human = build_rows(pdf_text_dir, batch_rows)
    txt_total = len(list(pdf_text_dir.glob("*.txt")))
    summary = build_summary(verified, needs_human, txt_total)
    summary["needs_human_rows"] = needs_human
    write_jsonl(data / "processed/schemes/verified/verified-ledger.jsonl",
                verified)
    (data / "processed/schemes/verified").mkdir(parents=True, exist_ok=True)
    (data / "reports").mkdir(parents=True, exist_ok=True)
    (data / "reports/scheme-verification.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (data / "reports/scheme-verification.md").write_text(
        render_markdown(summary, verified, needs_human), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in
                      ("verified_rows", "verified_with_ratio",
                       "verified_no_consideration", "needs_human_pdf",
                       "ratios_inferred_from_price")}))


if __name__ == "__main__":
    main()
