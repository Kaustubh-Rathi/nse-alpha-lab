# Disclaimer

**Not investment advice.** Read this before relying on anything in this repository.

---

## 1. No investment advice

NSE Alpha Lab is **research and educational software**. Nothing in this repository —
including the README, the reports, the CSVs, the Streamlit UI, the code comments, or any
output produced by running the code — constitutes:

- investment advice,
- a recommendation, solicitation, or offer to buy or sell any security,
- a personal recommendation tailored to any person's circumstances,
- a research report prepared by a registered research analyst,
- or advice on the suitability, tax treatment or legal consequences of any investment.

The project is **not** a registered investment adviser, research analyst, portfolio
manager, broker or intermediary in any jurisdiction. No advisory, fiduciary or
suitability relationship of any kind is created by reading, downloading, running or
reusing this work.

## 2. Past performance does not predict future returns

Every number in this repository is a **backtest**. A backtest is a simulation of what a
strategy would have done under a stated, simplified set of assumptions about prices,
charges, fills and corporate actions. It is not a record of what anyone achieved.

Past hypothetical performance is **not** a reliable indicator of future results. Past real
performance does not guarantee future results either. Specifically:

- The simulated results do not account for **market impact, liquidity limits, circuit
  limits, trading suspensions, capital-gains tax or depository charges**.
- The charge model is a 2026-style constant applied across 2013–2026, not the actual
  historical fee schedule.
- Returns are **price-vs-price**: dividends are excluded on both sides, so every return is
  understated relative to a total-return basis, by an amount that has not been established.
- Delisted holdings are assumed to exit at their last observed close, which is optimistic.
- The strategy is concentrated and volatile. The primary configuration has a **maximum
  drawdown of −55.30%** and was underwater for **952 calendar days** between 2018 and 2021.
  A real investor would have had to hold through that to realise any simulated return.
- The headline alpha is **not statistically significant** (Newey-West t = 1.62,
  bootstrap p = 0.096). The data cannot distinguish it from noise.
- **968 configurations were tested** and the results are ranked by Sharpe with no
  multiple-testing correction. The best row is a maximum over 968 trials.

See the [Results section of the README](README.md#results) for the full disclosure and
[STATUS.md](STATUS.md) for the complete list of unvalidated assumptions.

## 3. Market-risk warning

Investing in equity markets carries a real risk of total loss of capital. Indian equities,
and particularly mid- and small-cap equities of the kind this research concentrates in, are
volatile, can be illiquid, and can gap through stop-losses. Derivative instruments can lose
more than the capital committed.

If you are considering any real investment decision, consult a qualified, registered
financial adviser who can take your circumstances into account. Do not make a decision on
the basis of this software.

## 4. Data and model risk

- Market data contains errors in the original publications — symbol changes, bad prints,
  missing sessions, restatements. This repository does not claim to have corrected all of
  them. Known gaps are disclosed in [STATUS.md](STATUS.md).
- Backtests are sensitive to their assumptions. Reversing one assumption (the fill price,
  the charge schedule, the delisting treatment, the rebalance date) can change a result
  materially.
- **Overfitting is the default failure mode of this kind of research.** 968 configurations
  ranked by Sharpe without a correction will produce a flattering maximum whether or not
  any edge exists. That is why this repository publishes the null result rather than the
  best row.

## 5. No warranty, no liability

The software and data are provided **"as is"**, without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability, fitness for a
particular purpose and non-infringement.

To the maximum extent permitted by applicable law, the author shall not be liable for any
claim, damages or other liability — whether in contract, tort or otherwise — arising from,
out of, or in connection with the software, the data, or the use or other dealings in the
software and data. **You use this at your own risk.**

This is the same warranty disclaimer and limitation of liability as the
[Apache License 2.0](LICENSE), Section 7 and Section 8, under which the code is licensed.

## 6. Jurisdiction

This software is published from India. Market research and investment advice are regulated
activities in India by SEBI and in other jurisdictions by their own regulators. Nothing here
purports to comply with the regulatory requirements of any jurisdiction for the provision of
investment advice, and it must not be used to provide such advice. If you are subject to
regulation, determine your own compliance position before using or redistributing this work.

## 7. Trademarks

NIFTY 50, NIFTY 500, NIFTY MidSmallcap 400, NIFTY Midcap 150, NIFTY Smallcap 250 and the
National Stock Exchange of India names and marks are the property of their respective
owners. They are used in this repository **only** to identify the indices and market being
studied. Their use does not imply any affiliation with, endorsement by, or sponsorship from
NSE, NSE Indices Limited, or Yahoo Finance. See [DATA_LICENSES.md](DATA_LICENSES.md) for
data-specific notices.

## 8. Summary

This is a research lab. It publishes a strategy result that is **not statistically
significant** and a full table of 968 configurations with **no multiple-testing
correction**. It is for reading, reproducing and critiquing research. It is **not** a
product, **not** a signal service, and **not** investment advice.

Read [STATUS.md](STATUS.md), [DATA_LICENSES.md](DATA_LICENSES.md) and
[NOTICE](NOTICE) too.
