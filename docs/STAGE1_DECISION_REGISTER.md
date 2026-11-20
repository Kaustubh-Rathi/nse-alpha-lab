# Stage-1 Decision Register

**Status:** planning record plus owner decisions. `D-01`–`D-32` are architectural decisions; owner approval is recorded in the `U-xx` table and in **Owner decisions recorded 2026-11-14** below, which governs where it is more specific. Batch 5 implemented `D-04`/`D-05` for the gross-only path and `D-27`–`D-31` as synthetic-fixture domain contracts with fail-closed behavior; production evidence for those rows remains outstanding. Implementation still requires the associated batch's tests and revision-bound evidence.
**Gate:** the overall Stage-1 mandatory P0 gate remains **CLOSED**. No contract is passed by this register; no legacy runner migration, historical strategy execution or performance publication is authorized.
**Baseline:** `f5e943000764fade242b0482d67db2808991c076`
**Governing rule:** when the repository does not specify an economic rule, this register records a `SPEC GAP`; it does not invent one.
**Owner:** the research owner is the final approver for calendar/membership evidence and for every decision below.

| ID | Topic | Recommendation | Blocks |
|---|---|---|---|
| D-01 | Hard notional rounding | Floor the declared cap to paise at use; never round a hard limit upward | F1, Batch 1 |
| D-02 | Cap scope | Principal notional only; total payable is a separate constraint | F1, Batch 1 |
| D-03 | NAV precision | Exact internal `nav`; reporting-only `nav_reported`; no per-event rounding | F4, Batch 1 |
| D-04 | Dividend receivable lifecycle | Causal `confirm_dividend_payment` / `void_dividend` lifecycle; no permanent dead-end | F5, Batch 1 |
| D-05 | Unscheduled receivable in NAV | Include at known face value; disclose via `unresolved_settlement_claims` | F5, Batch 1 |
| D-06 | Momentum observations | Window-scoped `min_observations`; `None` means full window; history count diagnostic only | F6, Batch 2 |
| D-07 | Momentum rules | Wire the three single-value fields as explicit dispatch/asserts; second value is a schema change | F7, Batch 2 |
| D-08 | Basis provenance | Structured `BasisProvenance`; mandatory `basis_id` for closures/lambdas/stateful callables | F2, Batch 2 |
| D-09 | ADV missingness | Unknown by default; zero only with verified listing/no-trade evidence | F9, Batch 2 |
| D-10 | Universe membership | Eligibility screen + dated membership port; never current constituents | F8, Batch 5 |
| D-11 | Calendar authority | Only a published exchange artifact may fire a month-end; inferred/caller-asserted calendars are diagnostic | F11, Batch 2/5 |
| D-12 | Missing months | Record omission diagnostics; never synthesize a signal | F10, Batch 2 |
| D-13 | PortfolioSpec mapping | Long names canonical; short `P1/P2/P3` aliases at the sizing boundary; one resolver | F12, Batch 3 |
| D-14 | P1/P2/P3 meaning | Freeze definitions; add discriminating goldens | F12, F21, Batch 3 |
| D-15 | Policy validation | Enforce at the integration entry point, not only in a helper | F13, Batch 3 |
| D-16 | Dead configuration | Four-state disposition registry: 10 implemented, 12 reserved, 0 obsolete, 5 disconnected | F14, Batch 3 |
| D-17 | Verification status model | Separate component and contract status; only CONTRACT_PASS authorizes | F17, F18, Batch 4 |
| D-18 | Inventory integrity | State machine; clean inventory passes; unauthorized mismatch fails | F18, F22, Batch 4 |
| D-19 | Revision binding | Bind evidence to commit + dirty fingerprint + source/test/golden hashes | F16, F18, Batch 4 |
| D-20 | Skip policy | Mandatory skip blocks; optional dependency skip is declared, not hidden | F19, Batch 4 |
| D-21 | Evidence retention | Append-only, content-addressed artifacts; never overwrite | F16, Batch 4 |
| D-22 | Dataset prerequisite | Track `data/README.md` or declare it in a prerequisite manifest | F22, Batch 4 |
| D-23 | API compatibility | Additive change by default; removal requires a deprecation cycle | All |
| D-24 | Legacy sequencing | Legacy P0-1..P0-5 remain downstream and out of Stage-1 batches | Scope |
| D-25 | Coverage reason taxonomy | Total, explicit state function, not a fall-through chain | F3, Batch 1 |
| D-26 | Bar coherence | Validate OHLC coherence or explicitly represent degraded bars | F15, Batch 2 |
| D-27 | Continuity authority | Dated NSE/company documentary evidence; no similarity inference | P0-CONTINUITY, Batch 5/8 |
| D-28 | Scoped adjustment | Immutable raw prices; versioned security-scoped factors; point-in-time availability | P0-SCOPED-ADJ, Batch 5 |
| D-29 | Lifecycle evidence | Dated authoritative suspension/delisting/non-trading evidence; unknown stays unknown | P0-LIFECYCLE, Batch 5 |
| D-30 | Scenario isolation | Absolute separation from authoritative accounting and statistics | P0-SCENARIO, Batch 5 |
| D-31 | Friction-aware liquidation | Dated sell-side costs, adverse execution, verified capacity; otherwise unknown | P0-FULL-LIQUIDATION, Batch 5 |
| D-32 | Prefix invariance scope | Freeze past economic and decision artifacts; revised history is a new run | P0-PREFIX, Batch 6 |

---

## D-01 — Hard maximum-order-notional enforcement

**Context.** `fees.py:65,85` quantize `max_order_notional` with `money()` (`ROUND_HALF_UP`) before the integer search. A cap of `99.996` becomes `100.00`; a `100.00` notional is then admitted. The audit reproduced this for both buys and sells.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Floor the declared cap to paise at use, then compare the paise-quantized principal | Conservative; cap can never expand; same units as the booked principal; replayable | A sub-paise declared cap is tightened to the next lower paise |
| B | Compare the unquantized `Decimal(str(cap))` directly against the paise-quantized notional | Mathematically equivalent for a paise-quantized principal; no silent tightening | The applied cap is less explicit in the event record |
| C | Reject sub-paise caps in `ExecutionSpec.__post_init__` | Forces a clean contract | Breaking change for existing float specs; unnecessary |
| D | Keep half-up and document the cap as "rounded to paise" | No code change | Violates the plain meaning of a hard maximum; audit defect stands |

**Recommendation: A.** Introduce two small helpers with one semantic source: `principal_cap(execution) -> Decimal | None` returns `Decimal(str(value)).quantize(CENT, ROUND_FLOOR)` and `within_notional_cap(principal, cap) -> bool` compares the paise-quantized principal to that floored cap. Use the helpers in `affordable_buy`, `bounded_sell` and the independent replay. Do **not** change the global `money()` policy, which remains `ROUND_HALF_UP` for fees and recorded cash. Because booked principal is itself paise-quantized, A and B accept exactly the same set of fills; A is preferred because the applied cap is a booked amount that can be recorded and replayed without a second notion of raw notional. Expose the applied cap in `FillQuote` as `applied_principal_cap` for audit.

**Invariant.** `executed_notional <= declared_max_order_notional`, where `executed_notional` is the paise-quantized principal (fill price × integer quantity) and the declared cap is never rounded upward. Sell symmetry uses the same helper and the same principal definition.

**Consequences.**
- `99.996` permits at most `99.99` (not `100.00`).
- `99.994` and `99.99` permit at most `99.99`.
- `100.00` permits `100.00` (inclusive).
- `0` permits no trade; `None` is unbounded.
- Buys and sells are symmetric; fees never expand the cap.
- `invariants.py` must implement the same contract independently, and a mutation test must prove the old half-up path is detected.

**Open sub-decision (D-02):** whether the cap covers principal only or principal + fees.

---

## D-02 — Scope of `max_order_notional`

**Context.** The current code compares `quoted.notional` to the cap, while affordability separately compares `notional + fees` to `cash_budget`. The specification does not state whether "maximum order notional" includes fees.

**Options.**

| Option | Meaning | Advantages | Disadvantages |
|---|---|---|---|
| A | Principal notional only | Matches the field name and current code; fees are a separate budget constraint | A total cash outflow can exceed the cap by fees |
| B | Principal + fees | Stricter cash control | Contradicts the ordinary meaning of "notional"; would make the cap sensitive to tax rates |
| C | Two explicit fields: `max_principal_notional` and `max_total_payable` | Unambiguous; supports both risk limits | Larger interface change; requires migration |

**Recommendation: A**, with an explicit documentation sentence: "`max_order_notional` bounds principal only; total payable is bounded separately by settled cash and fee-inclusive affordability." If a total-payable risk limit is required, add it later as a distinct field under D-23 compatibility rules.

**Consequences.** Tests must assert both constraints independently: a cap-binding case and a cash/fee-binding case. The audit's F1 fix must not accidentally convert the cap into a total-payable cap.

---

## D-03 — Monetary precision and NAV quantization

**Context.** `docs/ARCHITECTURE.md:151-153` says principal, fees and cash are Decimal rounded once per order/component to paise, and "valuations need not round each position before aggregation." The audit found `value()` returns `nav=300.015` for a sub-paise mark. No explicit policy states whether the reported NAV is quantized.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Quantize NAV to paise at every event | Uniform display | Artificial per-event drift; loses information; repeated aggregation can accumulate error |
| B | Keep internal NAV exact; quantize only in a reporting/formatter layer | No drift; preserves economic truth; separates concerns | Callers must know which field is authoritative |
| C | Keep internal NAV exact and expose both `nav` and `nav_reported` | Explicit; backward compatible | Slightly larger public record |
| D | Never quantize anywhere | Maximum precision | Reports can show unrealistic precision; inconsistent with cash paise |

**Recommendation: C.** `Valuation.nav` remains the exact Decimal sum (`cash + receivables + marked holdings − liabilities`) and is authoritative for economics. Add `Valuation.nav_reported`, computed once as `money(nav)` (paise, `ROUND_HALF_UP`) at the presentation boundary only; it is never an input to a later event. Strategy return calculations use the exact `nav` series and link ratios; final published statistics use the declared reporting quantum. This deliberately rejects the alternative of quantizing the aggregate NAV at every valuation boundary: that approach is defensible for reporting but makes strategy returns depend on a presentation rule and contradicts the brief's requirement not to impose paise rounding on internal NAV merely because fees use it.

**Consequences.**
- No per-event rounding; no cumulative drift. `nav` is recomputed from exact state at each valuation, never accumulated from rounded snapshots.
- Tests: exact `8200.500` internal; `8200.50` reported; aggregation of two sub-paise positions exact; a long event sequence has no drift.
- Doc update: make the ARCHITECTURE sentence explicit about NAV, not just positions.
- This resolves F4 as a **clarification**, not a fee-style change.
- `nav_reported` is additive; existing callers of `nav` keep exact behaviour.

---

## D-04 — Dividend receivable lifecycle

**Context.** `payment_schedule()` returns `(None, "modeled", ...)` for `hold_receivable`; `core.settle()` rejects any claim with `due=None`. No operation can later attach verified payment evidence. The receivable is permanent and unreconcilable.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Declare `hold_receivable` permanently unrealizable and exclude it from NAV | Honest worst case | Writes off a valid economic asset; changes NAV semantics |
| B | Add `confirm_dividend_payment(dividend_id, payment, at)` plus `void_dividend(...)`; keep the receivable in NAV until settled | Causal; verified evidence with amount/date checks; no retroactive cash; explicit terminal states | New API and two event kinds; needs duplicate/conflict rules |
| C | Allow `core.settle` to accept `due=None` and settle on demand | Minimal change | Abandons due-date causality; late evidence could settle retroactively |
| D | Re-accrue a new claim when evidence arrives and leave the old one | Avoids mutation | Double-counts the receivable unless the old claim is explicitly superseded |

**Recommendation: B.** Model the lifecycle explicitly:

1. `accrue_dividend` creates a claim with `amount` known, `due=None`, `state="accrued_held"`, `basis="modeled"`, `unresolved_reason="hold_receivable"`. `Claim(amount known, due=None, reason empty)` auto-labels `no_settlement_schedule`, `state="unbound"`, and is non-confirmable; this preserves opening-balance claims without granting them a lifecycle.
2. `confirm_dividend_payment` is admissible only at `PRE_OPEN_EVIDENCE`, `PRE_OPEN_ACTIONS` or `CLOSE_VALUATION`, with `payment.available_at <= at`, `payment.on >= ex_date`, and `payment.amount is None or money(payment.amount) == claim.amount`. It records `DIVIDEND_PAYMENT_CONFIRMED`, sets `state="confirmed"`, `basis="verified"` and `unresolved_reason=""`, and does **not** move cash or change NAV.
3. Due determination is causal: a payment date later than `at.session` or still ahead in the current session produces `Due(POST_OPEN_SETTLEMENT, on=payment.on)`; a payment date whose settlement phase has already passed produces `Due(POST_OPEN_SETTLEMENT, session_index + 1)`. The historical payment date is retained for provenance.
4. `void_dividend` requires verified applicable evidence and a nonempty reason, records `DIVIDEND_VOIDED` with `state="voided"` and a receivable/write-off posting, and is terminal. It never creates cash.
5. `settle` continues to require a reached `due` in a settlement phase; `due=None` remains unsettleable.
6. Duplicate confirmation, conflicting amount, pre-ex-date payment, confirmation after void/settlement, and void after settlement all raise atomically with no state change.
7. At simulation termination, unresolved claims remain on the books and are disclosed via `unresolved_settlement_claims`; they are never silently converted to cash, zero or a fabricated payment date.

**Consequences.**
- `Claim.due=None` becomes a legal *pending* state, not a dead end.
- Replay must recognize `DIVIDEND_PAYMENT_CONFIRMED` and `DIVIDEND_VOIDED` and verify that the due date was not known earlier and that the amount never changed.
- Tests: causal confirmation, amount conflict, duplicate, void, settlement, termination, NAV.
- Backward compatibility: existing `hold_receivable` fixtures continue to produce a claim; only the new operations are additive.
- Partial payments, withholding and gross-vs-net amounts are `DECISION REQUIRED`; Stage 1 rejects a confirmed amount that differs from the accrued gross amount rather than restating the receivable.

---

## D-05 — Treatment of unscheduled receivables in NAV

**Context.** If a dividend amount is known but the payment date is unknown, is the receivable an asset?

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Include at face value in NAV | Economically correct if collection is probable | Overstates certainty |
| B | Exclude from NAV | Conservative | Understates wealth; contradicts receivable accounting |
| C | Include at face value and disclose via `unresolved_settlement_claims` (sorted claim IDs with `due is None` and a known amount); block authoritative performance statistics at termination while any remain | Transparent; preserves economics; distinguishes "known but unscheduled" from unknown claims | More record fields and terminal policy checks |

**Recommendation: C.** NAV includes the known amount (it is a valid claim) in `dividend_receivable`, never in `cash` or `cash_available`. `Valuation` exposes `unresolved_settlement_claims: tuple[str, ...]`; a terminal report with any unresolved schedule is labeled `incomplete_payment_evidence` and cannot produce an authoritative final return. No write-off is implied, and `nav_reason` does not gain a held state because the amount is known.

**Consequences.** F5 is fixed economically and transparently. The legacy performance gate remains closed.

---

## D-06 — Momentum observation semantics

**Context.** `signals.py:66` compares `ledger.valid_count(key, as_of)` to `min_observations`; `valid_count` counts the whole history. The docs disclose this. A symbol with 15 lifetime observations and 3 in the lookback window is ranked.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Keep whole-history semantics and document it as `min_history_observations` | No behavior change; statistically meaningful sample sufficiency | Does not ensure recent data quality |
| B | Count only observations in `anchor..as_of` | Ensures window completeness | Changes rankings; may exclude valid seasoned names after gaps |
| C | Two fields: `min_history_observations` (sample sufficiency) and `min_window_observations` (window completeness) | Explicit; supports both research intents | Larger spec surface; default choices matter |
| D | Keep one field but add an explicit `observation_window` enum | One field, explicit semantics | Migration and validation complexity |

**Recommendation: B.** Make `min_observations` **window-scoped** over the closed window `[anchor, as_of]` and default it to `None`, where `None` means every session in `window` (`lookback_sessions + 1`) must be a valid observation. Keep the exact observed-session anchor; there is no nearest-session or earliest-available substitute. Do not add a second momentum history gate: use `UniverseSpec.min_listed_sessions` for listing age. Keep `RankedKey.valid_observations` as the window count and add `history_valid_observations` for diagnostics only; the history count must never admit a key whose window count is short. Validate `1 <= min_observations <= lookback_sessions + 1`. Exact Fraction momentum, descending rank, key-ascending ties. Bump `MomentumSpec` to schema 2; reject v1 payloads with an explicit migration error rather than silently reinterpreting them. Set the default `price_basis` to `"close"`; `"adj_close"` requires a caller-supplied basis.

**Consequences.** The audit's F6 becomes a frozen, testable contract rather than an undocumented quirk. Existing tests that pin whole-history counts must be rewritten as window tests, with the history count asserted separately. `observations_lower_bound` remains a censoring diagnostic and is never an eligibility input. See the exact MOM-1..MOM-13 wording and acceptance cases in the research memo; they are the implementation contract.

---

## D-07 — Momentum anchor, observation and tie-break rules

**Context.** `anchor_rule`, `observation_rule` and `tie_break` are validated but never read; each admits one legal value. Mutating them changes nothing.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Implement all three as runtime switches | Future flexibility | No second value exists; speculative framework |
| B | Remove the fields | Honest surface | Breaks JSON specs that contain them |
| C | Retain them as single-value fields but wire each as an explicit dispatch/assert, so the value is actually consulted | Backward compatible; a second value cannot be silently inert | Slight surface noise; requires touching `rank_momentum` |

**Recommendation: C.** `anchor_rule` selects the exact-session-offset implementation, `observation_rule` selects the window-count implementation, and `tie_break` names the key-ascending comparator. Each remains a closed single-value field; adding a second value is a schema change that must come with an implementation. This is the `Implemented (implicit)` disposition in D-16, not a reservation. The execution fields that genuinely lack a consumer remain `RESERVED` under D-16.

**Consequences.** F7 is classified as "implemented as a single-value pin", not "dead". The configuration registry and mutation tests prove that changing any accepted value changes either behaviour or a schema error.

---

## D-08 — Custom price-basis provenance

**Context.** `MomentumRanks.price_basis` is always `spec.price_basis` (`"close"` by default), even when a custom `basis_price` callable computed a different series. `__name__` alone is insufficient: two different closures can share a name, and the same callable can behave differently with different parameters.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Record `__qualname__` | Cheap | Not unique; closures and lambdas collide |
| B | Record module + qualname | Better | Still not behavior-unique; no parameters |
| C | Require a caller-supplied `basis_id` | Unambiguous by contract | Callers can lie or reuse an ID |
| D | Hash normalized source via `inspect.getsource` | Behavior-sensitive for source-defined callables | Unavailable for C extensions/builtins; formatting-sensitive |
| E | Hybrid: `kind`, `name`, optional `version`, `source_digest`, `parameters` | Best available evidence; explicit unknowns | Larger record; digest may be unavailable |

**Recommendation: E, with a stronger identity rule.** Add a frozen `BasisProvenance` record:

```text
kind: "builtin" | "callable" | "composite"
basis_id: stable caller-supplied identifier; mandatory for closures, lambdas, partials and stateful callables
name: built-in basis name or the callable's module + qualname
version: caller-supplied optional version
source_digest: sha256 of normalized source or None; content provenance, not identity
parameters: immutable mapping of transformation parameters
```

`MomentumRanks` emits `basis_provenance` and a backward-compatible `price_basis` string. For built-ins, `price_basis = spec.price_basis` and `kind="builtin"`. For callables, `price_basis = "custom:<basis_id>"` and the provenance carries the identity; **never** the built-in spec string. A callable without a stable identity (`__name__`/`__qualname__` is `<lambda>`, or the callable is a closure/partial/stateful object) MUST supply `basis_id` or raise. `source_digest` may be `None` when source is unavailable; that is recorded as an explicit unknown and does not claim reproducibility. Two behaviorally different callables must never share an identical provenance record.

**Consequences.** F2 is fixed. Existing default tests still see `"close"`. New tests cover two different callables, a closure without `basis_id` (raises), an explicit ID, a parameterized transform, digest stability where available, and process reproducibility. `price_basis` remains a compatibility alias; new consumers read `basis_provenance`.

---

## D-09 — ADV missingness and minimum coverage

**Context.** `trailing_mean` skips missing bars, so a missing bar and a zero-volume bar both contribute nothing. A never-seen key returns `total=0, value=0.00`, contradicting `adv.py:1` ("unknown evidence yields None"). `capacity_notional` ignores `traded_sessions`.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Missing bar = zero volume (current) | Simple; matches "did not trade" for a listed security | Conflates data gaps with no-trade days; never-seen key looks like verified zero |
| B | Missing bar = unknown; any gap makes the mean unknown | Fail-closed | One data gap blocks all liquidity for the window; may be too strict |
| C | Explicit absence policy + minimum coverage: zero only when the key is known listed and not suspended; otherwise unknown; require `min_observed_sessions` | Honest; configurable; separates data gaps from no-trade | Requires lifecycle/listing evidence; more state |
| D | Return both `value` and `coverage` and let callers decide | Transparent | Shifts policy to every caller; easy to misuse |

**Recommendation: C**, with a result record exposing `observed_sessions`, `missing_sessions`, `zero_volume_sessions`, `invalid_sessions`, `coverage`, `traded_sessions`, `value: Decimal | None`, `reason`. The **default is strict**: a missing bar, an unknown security status, an invalid bar or an unknown field makes the mean `None`; zero is used only when the full window is observed with explicit zero trading, or when the caller explicitly opts into `allow_partial_adv=True` with a documented `verified_listed_no_trade` assumption. `capacity_notional` returns `None` unless `value is not None`, coverage meets the declared threshold, and `traded_sessions` meets its gate. Never return `0.00` for a never-seen key.

**Truth table.**

| Bar state | Evidence | Contribution | Result |
|---|---|---|---|
| Present, valid, value > 0 | Observed | Value | Included |
| Present, valid, value = 0 | Observed zero | 0 | Included |
| Present, invalid | Invalid | None | Mean unknown |
| Present, field `None` | Unknown | None | Mean unknown |
| Present, security status unknown | Unknown | None | Mean unknown |
| Absent, key never seen | Unknown | None | Mean unknown |
| Absent, key previously seen, suspension/listing unknown | Unknown | None under default | Coverage reported; below threshold → unknown |
| Absent, verified listed and not suspended | No trade | 0 only under explicit policy | Included as zero |
| Absent, verified suspended/delisted | Not tradable | None | Mean unknown |

**Consequences.** Universe default excludes unknown liquidity (fail closed). Tests cover the full table, the strict default, the explicit opt-in, and the `traded_sessions` gate. Appending later sessions never changes an earlier result.

---

## D-10 — Universe: eligibility screen versus point-in-time membership

**Context.** `build_universe` screens on as-of bars, series, ISIN policy, price, volume, ADV, average trades and listed age. It has no constituent dates, suspension state or termination state. Suspended and delisted keys are indistinguishable (`no_bar_on_session`).

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Keep the eligibility screen and rename it accordingly | Honest; no new data dependency | Not a historical index universe |
| B | Add a dated membership evidence port (constituents, effective intervals, availability) and intersect with eligibility | Correct point-in-time universe | Requires authoritative membership data (currently incomplete) |
| C | Use today's Nifty 500 constituents for all history | Simple | Look-ahead/survivorship bias; forbidden |
| D | Infer membership from liquidity | No external data | Not index membership; false authority |

**Recommendation: B**, with A as the explicit fallback label. The screen returns `Membership(kind="research_eligibility", ...)` when no membership evidence is supplied. A `MembershipEvidence` port supplies dated intervals with `effective_from`, `effective_to`, `available_at`, `citation`, and identity keys. `build_membership` intersects the screen with membership as-of the decision. Current constituents are never substituted. If membership evidence is absent, the result is `RESEARCH_ELIGIBILITY_ONLY` and the P0-UNIVERSE contract stays `CONTRACT_BLOCKED`.

**Consequences.** F8 is not "fixed" by code alone; it becomes a properly gated contract. Existing tests remain valid for the screen.

---

## D-11 — Calendar authority and month-end scheduling

**Context.** `KnownCalendar` accepts any nonempty unique dates, including Saturdays, and `RebalanceSchedule` trusts it. Without a calendar, month-end falls back to the first observed session of the next month. Uncovered months silently use the fallback.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Keep caller-supplied raw calendars and let them fire | No new data | False authority; weekends accepted; a guess becomes an official month-end |
| B | Only a published exchange artifact may fire; caller-asserted/inferred calendars are diagnostic and cannot be registered | Correct authority; explicit fallback | Requires authoritative calendar data |
| C | Infer calendars from observed sessions | No external data | Cannot distinguish a holiday from missing data; look-ahead risk if future sessions used |
| D | Reject any calendar that disagrees with weekdays | Catches some errors | Still cannot know special sessions/holidays |

**Recommendation: B, with D as a validation layer.** `KnownCalendar` gains `authority="published_exchange"`, `complete_months`, `special_sessions`, `source_sha256` and `coverage_end`. Only `published_exchange` is admissible to `RebalanceSchedule`. A `CALLER_ASSERTED` or `inferred_weekday` calendar may be constructed for diagnostics but MUST NOT fire a signal; it cannot be registered. A month fires only when it is covered, complete, and the observed session is its last published session. Uncovered/incomplete months use the observed-month fallback with `late=True` and a diagnostic. A weekend/holiday session is admissible only when explicitly listed in `special_sessions`. `finalize()` reports end-of-history gaps and never emits a signal. Late calendar evidence never rewrites a prior decision.

**Consequences.** F11 and part of F10 are addressed. The P0-BOUNDARY contract requires a dated, cited, digest-bound published calendar; without it, the contract is `CONTRACT_BLOCKED` and no caller assertion may be promoted to `KNOWN_CALENDAR`.

---

## D-12 — Months with zero observed sessions

**Context.** The schedule only compares the immediately previous observed month. If March and April have no observations, no decision, counter or `_signaled` entry records the omission.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Keep silent omission | No behavior change | Downstream cannot distinguish "no rebalance" from "no data" |
| B | Emit an explicit `MonthOmission` record for every skipped month, with reason `no_observed_sessions` | Auditable; causal | New record type and tests |
| C | Synthesize a month-end signal at the next observed session | No silent gap | Fabricates a decision date that never existed; potential look-ahead |
| D | Raise an error on any gap | Forces data completeness | Too strict for historical research |

**Recommendation: B.** `RebalanceSchedule` exposes `omissions: tuple[MonthOmission, ...]`; each carries `month`, `reason`, `first_observed_after`, `calendar_coverage`. No signal is synthesized unless a verified calendar proves a session existed and the strategy explicitly allows late catch-up (already `late=True`). At end-of-history, the final unobserved months are reported as `history_ended`.

**Consequences.** F10 becomes a diagnostic contract. Tests assert both the signal list and the omission list.

---

## D-13 — `PortfolioSpec` to `sizing` mapping

**Context.** `PortfolioSpec.sizing_policy` defaults to `P2_size_excluding_unresolved`; `size_orders` accepts only `"P1"|"P2"|"P3"` and rejects the default. No production code consumes `PortfolioSpec`. The docs say callers must map the names, but no mapper exists.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Make long names canonical and accept short aliases only at the `size_orders` boundary | Self-documenting serialized specs; keeps existing call sites; one vocabulary in results | `sizing.POLICIES` changes and external importers must adapt |
| B | Make short codes canonical and map long names in `specs` | Matches current `sizing`; concise | Keeps the long default unusable; loses self-documentation |
| C | Accept both and store whichever was passed | Backward compatible | Two vocabularies persist in results and fixtures |
| D | Delete `PortfolioSpec` | Removes dead surface | Loses intended portfolio contract |

**Recommendation: A.** Add a single `resolve_sizing_policy(portfolio, valuation)` in `specs` with:
`P1 = "P1_freeze_on_unresolved"`, `P2 = "P2_size_excluding_unresolved"`, `P3 = "P3_size_including_unresolved_at_screen"`, and `ALIASES = {"P1": P1, "P2": P2, "P3": P3}`. Long names are canonical in `PortfolioSpec`, golden fixtures and `SizingResult.policy`; short codes are accepted only at the `size_orders` boundary and normalized once. `PortfolioSpec.sizing_policy` keeps its long default and becomes consumable. `size_portfolio_orders(portfolio, valuation, ...)` is the only portfolio entry point; `size_orders` does not accept a `PortfolioSpec` directly. `resolve_sizing_policy` calls `validate_policy_combination`.

**Consequences.** F12 is fixed as an interface contract, not a silent coercion. Tests cover every long/short pair, the long default, unknown values, and the production wrapper. No public serialized name becomes a short code.

---

## D-14 — P1/P2/P3 meaning and discriminating goldens

**Context.** P1 freezes the whole round when any rank is unresolved; P2 excludes unresolved ranks; P3 includes unresolved ranks at screen value. The golden cases contain only P2/P3 and no unresolved ranks, so the policies are indistinguishable. P1 has no golden.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Freeze current semantics and add goldens | No code change; test-driven clarity | Current P3 "screen value" is not actually modeled in `size_orders` |
| B | Redefine P3 to include a screen-value input | Makes P3 meaningful | API change; needs `ValuationPolicy.sizing_uses_screen_value` integration |
| C | Remove P1/P3 until specified | Honest | Reduces scope; breaks docs |

**Recommendation: A + B.** Freeze and wire:
- **P1 (whole-round freeze):** if any listed rank is unresolved, every rank receives zero with `frozen_unresolved`; no cash, slot or fee is consumed.
- **P2 (exclude unresolved):** each unresolved rank receives zero with `excluded_unresolved`, consumes no cash/slot, and its desired cash is available to later ranks.
- **P3 (include unresolved at screen value):** each unresolved rank is sized from a caller-supplied screen-valued desired notional and screen price; it is capped by remaining settled cash, consumes a slot when filled, and uses `filled_screen_value`. Missing screen evidence yields `screen_value_unavailable` and consumes nothing.

An "unresolved rank" is exactly a zero-based position in `unresolved_ranks`; it is never inferred from a missing price. Add the seven hand-derived goldens from the research memo (`sizing_p1_freeze`, `sizing_p2_exclude`, `sizing_p3_screen_clip`, `sizing_p3_slot`, `sizing_p1_not_first`, `sizing_p3_unavailable`, `sizing_p2_fees`) and mutation tests that delete or swap each branch.

**Consequences.** The docstring claim becomes true; each policy changes at least one expected output. `sizing_uses_screen_value` is wired through `resolve_sizing_policy` (D-15). P3's screen price/value must originate in the rebalance/target layer and must never be inferred from a stale mark.

---

## D-15 — Where cross-policy validation runs

**Context.** `validate_policy_combination` is imported only by tests. A caller can run P3 without `sizing_uses_screen_value=True`.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Validate in each spec constructor | Early | Cannot see the cross-spec combination |
| B | Validate at the integration entry point (portfolio construction / sizing orchestration) | Sees both specs; mandatory in production | Must be called by every path |
| C | Both | Defense in depth | Duplication risk |
| D | Leave to callers | No change | Contract unenforced |

**Recommendation: B + C (helper in constructors, mandatory call at the integration boundary).** Add a single `validate_run_specs(portfolio, valuation, execution, universe, momentum)` entry point that calls `validate_policy_combination` and other cross-spec rules. The integration path must call it before sizing. Tests assert that P3 without the screen flag fails before any order is produced.

**Consequences.** F13 is fixed. The validator becomes production code, not test-only.

---

## D-16 — Disposition of validated but unread specification fields

**Context.** F14 lists fields that are validated but not read (or read only in validation). Keeping them creates a false impression of implemented behavior.

**Alternatives considered.** A: wire every field now — invents economic behaviour and expands scope. B: classify every field in a four-state registry, allow `DISCONNECTED` only under a bounded allowlist, and require zero disconnected before the configuration row passes (adopted). C: delete every unread field — breaks serialized specs and baseline fixtures.

**Policy.** Every field must be in exactly one state:

- **IMPLEMENTED** — a production reader consumes it and a behaviour test proves the effect. A single-value field whose value is hardcoded must be wired as an explicit dispatch/assert before its contract passes; `IMPLEMENTED (implicit)` in the baseline table means a production branch already enforces the sole legal value, but the batch must still add the explicit dispatch/assert and effect test. "Implicit" is a baseline wiring note, not a fifth disposition.
- **RESERVED** — retained for a named future contract, with a single legal value, a registry entry and a test that it has no runtime effect until implemented.
- **OBSOLETE** — removed or rejected with a schema-version bump.
- **DISCONNECTED** — an implemented counterpart exists but the field is not wired. This is a failing state; it may exist only in a bounded allowlist with owner, issue and expiry, and the count must reach zero before the configuration contract can pass.

**Per-field disposition (27 fields: 10 implemented, 12 reserved, 0 obsolete, 5 disconnected).**

| Spec | Field | Disposition | Named gate / required action |
|---|---|---|---|
| `MomentumSpec` | `anchor_rule` | IMPLEMENTED (implicit) | `MOM-ANCHOR`: dispatch exact session offset; second value rejected |
| `MomentumSpec` | `observation_rule` | IMPLEMENTED (implicit) | `MOM-OBS`: dispatch window count; second value rejected |
| `MomentumSpec` | `tie_break` | IMPLEMENTED (implicit) | `MOM-TIE`: comparator named by the field; only `key_asc` |
| `ExecutionSpec` | `fill_timing` | RESERVED | `EXEC-FILL`: phase-40 next-open execution |
| `ExecutionSpec` | `max_participation_rate` | RESERVED | `EXEC-PARTICIPATION`: runtime reporting/participation cap |
| `ExecutionSpec` | `settlement_funding` | IMPLEMENTED (implicit) | `EXEC-FUNDING`: assert `settled_cash_only`; expected proceeds cannot fund a buy |
| `ExecutionSpec` | `unfilled_remainder` | RESERVED | `EXEC-LIFECYCLE`: remainder expiry/reissue |
| `ExecutionSpec` | `circuit_limit_pct` | RESERVED | `EXEC-CIRCUIT`: circuit-band fill behaviour |
| `ExecutionSpec` | `hold_suspended_positions` | RESERVED | `LIFE-SUSPENSION`: hold-but-do-not-buy |
| `ValuationPolicy` | `dividend_cash_phase` | IMPLEMENTED (implicit) | `DIV-PHASE`: assert sole value; cash after opening fills |
| `ValuationPolicy` | `series_precedence` | RESERVED | `VAL-SERIES`: valuation/mark precedence |
| `ValuationPolicy` | `carried_position_is` | RESERVED | `LIFE-ABSENCE`: carried position state |
| `ValuationPolicy` | `absence_review_sessions` | RESERVED | `LIFE-ABSENCE`: review window |
| `ValuationPolicy` | `sizing_uses_screen_value` | **DISCONNECTED** | `SIZING-P3`: wire through `resolve_sizing_policy`; P3 iff true |
| `ValuationPolicy` | `allow_scenario_statistics` | IMPLEMENTED (guard) | `VAL-SCENARIO`: prohibition stays; true raises |
| `CostSpec` | `schedule_basis` | IMPLEMENTED (implicit) | `COST-SCHEDULE`: assert/emit in fee provenance |
| `CostSpec` | `monetary_rounding` | IMPLEMENTED (implicit) | `COST-ROUNDING`: assert paise half-up; do not silently change |
| `PortfolioSpec` | `max_positions` | **DISCONNECTED** | `PORT-SLOTS`: map to slots in `size_portfolio_orders` |
| `PortfolioSpec` | `initial_cash` | RESERVED | `PORT-INIT`: portfolio initialization |
| `PortfolioSpec` | `weighting` | RESERVED | `PORT-WEIGHT`: equal-weight target construction |
| `PortfolioSpec` | `sizing_policy` | **DISCONNECTED** | `SIZING-POLICY`: `resolve_sizing_policy`; long default must work |
| `PortfolioSpec` | `allocation_order` | IMPLEMENTED (implicit) | `PORT-ORDER`: assert `rank_then_key` |
| `PortfolioSpec` | `disposal_policy` | RESERVED | `LIFE-DISPOSAL`: hold/retire semantics |
| `PortfolioSpec` | `scenario_only` | IMPLEMENTED (guard) | `PORT-SCENARIO`: retire-zero guard tested |
| `PortfolioSpec` | `rebalance_frequency` | **DISCONNECTED** | `PORT-REBALANCE`: spec-to-schedule adapter |
| `PortfolioSpec` | `rebalance_anchor` | **DISCONNECTED** | `PORT-REBALANCE`: adapter; only known-calendar-else-observed fallback |
| `PortfolioSpec` | `trim_overweight_targets` | RESERVED | `PORT-TRIM`: trim-overweight target behaviour |

**Consequences.** Add a machine-checkable `FieldDisposition` registry (`specs/dispositions.py` or test-only) with completeness, reader, behaviour-test, reserved-anchor, obsolete-rejection and disconnected-allowlist checks. `scripts/report_stage1_accounting.py` reports the four counts and fails the configuration row while `disconnected > 0`. F14 is closed by honest classification and by driving the five disconnected fields to zero, not by pretending everything is wired.

**Current registry note (2026-11-15).** `CURRENT_DISPOSITIONS` is 16 implemented / 11 reserved / 0 obsolete / 0 disconnected. `PortfolioSpec.trim_overweight_targets` is now implemented through the owner-approved explicit-target planner. The table above is the retained D-16 baseline snapshot; it is not rewritten.

---

## D-17 — Verification status model and authorization

**Context.** The doc and machine report disagree; component tests are implicitly treated as contract evidence.

**Alternatives considered.** A: keep treating component tests as contract evidence — current unsafe behaviour. B: separate `COMPONENT_*` from `CONTRACT_*`, bind evidence to the tested commit, and authorize only on `CONTRACT_PASS` (adopted). C: block every row until the full contract suite exists — no actionable intermediate state.

**Recommendation.** Adopt the seven-status model from the master plan (`COMPONENT_IMPLEMENTED`, `COMPONENT_VERIFIED`, `CONTRACT_PARTIAL`, `CONTRACT_PASS`, `CONTRACT_NOT_IMPLEMENTED`, `CONTRACT_FAIL`, `CONTRACT_BLOCKED`). Each mandatory row has:

```text
contract_id
contract_status
component_status
evidence: [artifact paths + sha256]
tested_commit
blocking_reason
required_proof
```

Authorization requires every mandatory row `CONTRACT_PASS`, evidence current at HEAD, no mandatory skips, and inventory integrity pass. `COMPONENT_VERIFIED` never authorizes.

**Backward compatibility.** Emit legacy `status` during a transition and document the mapping; remove only after consumers migrate.

**Consequences.** Report, matrix and authorization use one vocabulary; a component test can no longer promote a contract row, and a stale artifact cannot authorize a newer commit.

---

## D-18 — Inventory integrity state machine

**Context.** `only_accepted_exception` means "exactly one acknowledged change", so a perfectly clean inventory fails `integrity_ok`. The CLI exits 0 on a clean tree even when `only_accepted_exception=false`.

**Alternatives considered.** A: keep the boolean — a clean clone still reports a false failure. B: a seven-state machine with one shared report/CLI predicate (adopted). C: ignore the clean-tree false negative — leaves a known false red in the authorization path.

**States.**

| State | Condition | Report `integrity_ok` | CLI exit |
|---|---|---|---|
| `CLEAN` | All hashes match; zero acknowledged exceptions | true | 0 |
| `AUTHORIZED_EXCEPTION` | All non-exception hashes match; exactly the authorized exception list matches | true | 0 |
| `AUTHORIZED_EXCEPTION_ABSENT` | All hashes match; an authorized exception is no longer present | true (record `exception_absent`) | 0 |
| `UNEXPECTED_CHANGE` | Any non-exception hash differs | false | 1 |
| `MISSING_FILE` | Any inventoried path cannot be read | false | 1 |
| `READ_ERROR` | Any non-missing OSError | false | 1 |
| `EXCEPTION_LIST_CHANGED` | A new/changed exception not in the authorized list | false | 1 |
| `AUTHORIZED_EXCEPTION_MISMATCH` | The exception path exists but its hash is neither original nor accepted | false | 1 |

`integrity_ok = state in {CLEAN, AUTHORIZED_EXCEPTION, AUTHORIZED_EXCEPTION_ABSENT}`. The report and CLI must use the same function. The authorized exception list is configuration, not a count.

**Recommendation: B** — replace the `only_accepted_exception` boolean with this state machine and one shared report/CLI predicate.

**Consequences.** F18 and F22 are closed by a single shared implementation.

---

## D-19 — Revision-bound evidence

**Context.** Artifacts record `foundation_commit` and `head_at_verification` but never compare them; a report can be generated at any HEAD and still look authoritative.

**Alternatives considered.** A: keep the advisory fields — an artifact can authorize a commit it never tested. B: require `tested_commit == HEAD` plus source, golden and inventory fingerprints (adopted). C: trust file timestamps — not reproducible and not content-bound.

**Recommendation: B.** Bind every authorization artifact to the tested commit, the worktree state and content fingerprints; the rules below make a stale or mismatched artifact non-authorizing.

**Required schema (minimum).**

```json
{
  "schema_version": 2,
  "generated_at": "...",
  "tested_commit": "<sha>",
  "worktree_state": "clean|dirty",
  "dirty_fingerprint": "<sha256 or null>",
  "source_fingerprint": "<sha256 of tracked source at tested tree>",
  "test_selection": {"paths": [...], "hash": "<sha256>"},
  "test_results": {"tests": 0, "pass": 0, "fail": 0, "skip": 0, "xml_sha256": "<sha256>"},
  "mandatory_families": {"family": "CONTRACT_PASS|..."},
  "golden_sha256": "...",
  "inventory_sha256": "...",
  "environment": {"python": "...", "pytest": "...", "lock_sha256": "..."},
  "verification_scope": "...",
  "authorization": {"status": "CLOSED", "reasons": [...]}
}
```

**Rules.**
- Authorization requires `tested_commit == git rev-parse HEAD`.
- A dirty tree is allowed only if `dirty_fingerprint` is recorded and the source fingerprint of the tested tree is reproducible; the report must say `DIRTY`.
- Missing mandatory family → `CONTRACT_BLOCKED`.
- Any mandatory skip → not authorized.
- Evidence older than the tested commit or with a mismatched source/golden hash → stale → not authorized.

**Consequences.** F16/F18 are closed. The report cannot authorize code it did not test.

---

## D-20 — Skip policy

**Context.** Baseline artifacts have 0 skips; HEAD has 5 optional PDF skips. The doc conflates the two.

**Alternatives considered.** A: report one undifferentiated skip count — mandatory gaps hide among optional ones. B: classify every test as `MANDATORY`, `OPTIONAL_DEPENDENCY` or `DIAGNOSTIC` and fail authorization on any mandatory skip (adopted). C: deselect optional tests — hides the dependency instead of disclosing it.

**Recommendation.**
- Classify each test as `MANDATORY`, `OPTIONAL_DEPENDENCY`, or `DIAGNOSTIC`.
- A skip in a mandatory family is a failure for authorization.
- An optional-dependency skip is reported explicitly with the missing dependency and does not silently reduce the mandatory count.
- The machine report records `mandatory_skips` and `optional_skips` separately.
- Human docs quote the commit and environment for every count.

**Consequences.** F19 is fixed; future baseline/HEAD comparisons cannot hide skips.

---

## D-21 — Evidence retention and regeneration

**Alternatives considered.** A: overwrite artifacts on regeneration — destroys the audit trail. B: append-only, content-addressed artifacts with a `SUPERSEDED` index (adopted). C: delete stale artifacts — also destroys the trail and invites gate shopping.

**Recommendation.** Verification artifacts are append-only and content-addressed. Regeneration writes a new file (timestamp/commit suffix) and updates an index; it never overwrites a prior artifact. The machine report references evidence by path + SHA256. Existing artifacts remain untouched and are marked `SUPERSEDED` in the index, not deleted.

**Consequences.** F16 is auditable over time. `git` history is not rewritten.

---

## D-22 — Dataset prerequisite tracking

**Context.** `data/README.md` is inventoried (sha `230470d3...`) but untracked. A clean clone fails inventory with an `OSError`.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Track `data/README.md` in git | Clean clone verifies | Adds a data-tree file to version control |
| B | Declare it in a prerequisite manifest checked before inventory | Keeps data untracked | Extra mechanism |
| C | Remove it from the inventory | Simplest | Changes the baseline inventory (forbidden here) |

**Recommendation: A**, with the owner's approval. The file is documentation, 2.6 KB, and already part of the inventory hash. Do not change the inventory itself.

**Consequences.** F22 is closed without a dataset-content change.

---

## D-23 — Public API and specification compatibility

**Alternatives considered.** A: hard-rename and remove ambiguous fields immediately — breaks callers and silently reinterprets serialized specs. B: additive change by default with an alias window and an explicit schema-2 rejection for the window-scoped momentum payload (adopted). C: accept both old and new momentum semantics under one schema — ambiguous and unreplayable.

**Recommendation.**
- Additive fields and operations are preferred.
- Renames use aliases for at least one release: long/short sizing names and `price_basis` → `basis_provenance`.
- `min_observations` keeps its name and becomes window-scoped under D-06; there is no `min_history_observations` alias. `MomentumSpec` schema 2 rejects v1 payloads with an explicit migration error rather than silently reinterpreting them.
- Removals require a documented deprecation and a major-version decision.
- JSON round-trips must accept old and new names; serialization uses the new canonical name.
- Golden fixtures remain valid; new fixtures are additive.

**Consequences.** Existing callers and tests do not break abruptly. A migration table is part of Batch 3.

---

## D-24 — Legacy P0 sequencing

**Context.** `docs/QUANT_REPOSITORY_AUDIT.md` defines five legacy P0s (stale marks, no strict return, incompatible engines, identity-matched actions, no exit values). They are real but outside the Stage-1 accounting slice.

**Alternatives considered.** A: fold legacy P0s into Stage-1 batches — mixes correctness with strategy/performance work and dilutes the gate. B: keep `LEG-P0-1..5` downstream, list both gates, and forbid legacy edits in Stage-1 (adopted). C: ignore legacy P0s entirely — would let a Stage-1 PASS imply performance authorization it does not grant.

**Recommendation.** Keep `LEG-P0-1..5` in the contract matrix as `DOWNSTREAM / NOT IN STAGE-1 BATCHES`. Stage-1 may define ports or evidence interfaces that the legacy remediation will consume, but must not modify `engine.py`, `history.py`, `sensitivity.py`, `total_return.py` or run strategies. Legacy P0s remain a precondition for any performance publication, not for Stage-1 accounting PASS.

**Consequences.** Scope is protected. The final gate report must list both Stage-1 and legacy gates so no reader confuses them.

---

## D-25 — Reference-coverage reason taxonomy

**Context.** `valuation.py:99` uses a fall-through expression: `missing_reference` if gross is None, else `zero_reference_notional` if gross == 0, else `mixed_date_reference_only`. A fully priced book has `gross = marked > 0`, so it is labeled mixed-date.

**Alternatives considered.** A: keep one mixed fall-through reason — the current defect. B: six closed reference states plus a separate four-value `nav_reason` (adopted). C: add a `held_receivable` NAV state — rejected because a known-amount claim does not make NAV unknown and is disclosed separately.

**Adopted states.** `reference_coverage_reason` describes *reference coverage only*. Whether NAV is available is a separate field, `nav_reason`; conflating them would repeat the original design error.

| `reference_coverage_reason` | Condition | Meaning |
|---|---|---|
| `no_positions` | `holdings == ()` | Cash-only book; coverage is undefined, not zero-priced |
| `missing_reference` | at least one unresolved holding has no admissible reference | Aggregate reference and gross are `None`; `known_reference_subtotal` preserves the partial sum |
| `zero_gross_notional` | holdings exist and `gross == 0` | Defensive state; no priced or referenced notional |
| `complete_current_date` | `reference == 0` and `gross > 0` | Every holding is priced at the current session and phase |
| `dated_reference_only` | `marked == 0` and `reference > 0` | Nothing is priced; all value is a dated reference |
| `mixed_current_and_dated_reference` | `marked > 0` and `reference > 0` | Some holdings are priced; the rest use dated references |

**Precedence.** `no_positions` > `missing_reference` > `zero_gross_notional` > `complete_current_date` > `dated_reference_only` > `mixed_current_and_dated_reference`.
A reference never makes a holding priced; a blocked position is never priced by a current mark.

**Separate `nav_reason` axis.** `nav_available`, `unpriced_positions`, `unknown_assets`, `unpriced_positions_and_unknown_assets`. Precedence: `nav_available` > the two combined states > the single-cause states. `unresolved_settlement_claims` is a separate disclosure for known-amount claims with no due date and does not make NAV unknown.

**Recommendation: B** — adopt the six-state `reference_coverage_reason` taxonomy with the precedence above and the separate `nav_reason` axis.

**Consequences.** A fully priced book returns `complete_current_date`; a cash-only book returns `no_positions`; an unknown claim does not change the reference reason. Add the eight-book reference/NAV table (B1–B8) from the accounting memo and the exact-string closed-vocabulary test. This is a diagnostic-only fix; NAV arithmetic is unchanged.

---

## D-26 — `Bar` OHLC coherence

**Context.** `Bar` accepts `valid_ohlcv=True` with all OHLC `None`. Consumers fail closed, but the record is internally inconsistent.

**Options.**

| Option | Description | Advantages | Disadvantages |
|---|---|---|---|
| A | Reject `valid_ohlcv=True` unless `close` is present and OHLC ordering is coherent | Strong record integrity | May reject legitimate partial bars |
| B | Add `bar_quality` enum (`complete`, `close_only`, `degraded`) and require `valid_ohlcv` to agree | Explicit; flexible | New field and migration |
| C | Leave as is; consumers fail closed | No change | Inconsistent records persist |

**Recommendation: B**, with `valid_ohlcv=True` requiring at least `close`; `close_only` allowed but flagged. If OHLC are all present, enforce `low <= min(open, close) <= max(open, close) <= high` and nonnegative values. Add tests for every negative case.

**Consequences.** F15 is closed without silently changing valid historical bars. Consumers keep failing closed.

---

## D-27 — Documentary continuity authority

**Owner decision (2026-11-14).** Require explicit dated NSE/company documentary evidence for every symbol/ISIN change, merger, demerger and successor relationship. Preserve separate security identities unless a verified mapping establishes both continuity and the nature of the corporate event. Similarity of names, ISINs or prices is never evidence. An unresolved boundary is reported, not guessed.

**Contract effect.** `P0-CONTINUITY` may consume a dated evidence port and a contract-level identity fixture. Without authoritative documents, the affected step is `CONTRACT_BLOCKED` and uses only clearly labelled synthetic fixtures for interface and fail-closed tests.

**Implementation (Batch 5/8/10).** `src/nse_pipeline/backtest/continuity.py` implements `DocumentaryEvidence`, `ContinuityBoundary`, `admit_boundaries` and `ContinuityLedger`; `src/nse_pipeline/backtest/continuity_evidence.py` verifies stored evidence lanes before constructing those objects. A document is admissible only when its publisher, document ID, citation, content digest, availability time and effective interval are explicit; future documents are wholly invisible; conflicting successors, duplicate edges and cycles are reported and excluded. Evidence: `tests/test_accounting_continuity.py`, `tests/test_accounting_continuity_evidence.py`, `tests/test_accounting_evidence_contract.py`, `tests/test_accounting_batch5_integration.py`, `tests/golden/continuity_cases.json`, `verification/stage1/batch8/continuity/` and `verification/stage1/batch9/continuity-audit/`. The current Batch 10 revision stores 1,113 NSE Indices Limited press releases plus 5 reference documents, proves 59 two-sided index-change boundaries (effective 2018-02-05..2026-09-07) and refuses 50 candidates; it also retains the 14-edge company-filing history lane (2018-03-31..2022-05-03). The combined loader binds 73 boundary objects, but the typed ledger admits 63 records, excludes 10 and reports 5 conflicts where the parent extension collides with history-lane edges on the same source key. The parent gaps 2018-01-01..2018-02-04 and 2026-09-08..2026-11-16, the history gaps 2018-01-01..2018-03-30 and 2022-05-04..2022-05-30, the combined-lane conflicts, and the absence of `symbol_change`/`isin_change` edges remain explicit. The independent Batch 9 audit rehashed 1,113/1,113 PDFs and 5/5 references with zero mismatches and confirmed `coverage.complete=false`; its excerpt-scope, refusal and screening findings remain owner-review items, including one unrefused Jubilant Life Sciences scheme candidate. Gap closure or an explicit owner exclusion, conflict reconciliation and owner acceptance of the current revision remain outstanding, so `P0-CONTINUITY` stays `CONTRACT_BLOCKED` under OD-B9-2.

## D-28 — Scoped historical adjustment

**Owner decision (2026-11-14).** Preserve immutable raw prices. Apply versioned adjustment factors only to the affected security's eligible historical window, based on verified corporate-action terms and effective dates. Enforce point-in-time availability for trading decisions. Distinguish an ex-post research series from a point-in-time-available series: a future split may restate past prices for ex-post analysis, but it must not leak into an earlier simulated decision.

**Contract effect.** `P0-SCOPED-ADJ` requires identity-proven scope, horizon admissibility, raw-price immutability and prefix invariance. A factor whose target or effective date is ambiguous is refused, not applied approximately.

**Implementation (Batch 5).** `src/nse_pipeline/backtest/scoped_adjustment.py` keeps `RawPriceSeries` immutable, applies exact rational `AdjustmentFactor` values only inside the declared security key and date scope, rejects wrong-security or wrong-digest factors, and separates `point_in_time` from `ex_post` views. Evidence: `tests/test_accounting_scoped_adjustment.py`, `tests/test_accounting_evidence_contract.py`, `tests/test_accounting_batch5_integration.py` and `tests/golden/scoped_adjustment_cases.json`. Authoritative action terms are absent, so the row is `CONTRACT_PARTIAL`.

## D-29 — Lifecycle states from dated evidence

**Owner decision (2026-11-14).** Distinguish suspension, delisting, temporary non-trading and missing data only through dated authoritative evidence. Missing OHLCV alone never establishes suspension or delisting. Unknown status remains unknown. Termination requires verified terminal evidence; terminal proceeds remain unknown rather than last close.

**Contract effect.** `P0-LIFECYCLE` may model explicit states and blockers. Absence of a bar is an observation gap, not a lifecycle event.

**Implementation (Batch 5).** `src/nse_pipeline/backtest/lifecycle.py` provides `LifecycleState`, `LifecycleRecord`, `resolve_lifecycle`, `blocking_reason`, `require_negotiable` and `require_terminal_proceeds`. States come only from dated available records; missing bars never create a state; delisted terminal proceeds stay null unless explicitly verified and never fall back to the last close. Evidence: `tests/test_accounting_lifecycle.py`, `tests/test_accounting_evidence_contract.py`, `tests/test_accounting_batch5_integration.py` and `tests/golden/lifecycle_cases.json`. Authoritative exchange records are absent, so the row is `CONTRACT_PARTIAL`.

## D-30 — Scenario isolation

**Owner decision (2026-11-14).** Scenario holdings, costs, cash, NAV, liquidation values and statistics must be absolutely separate from the authoritative portfolio and performance record. Scenario operations may not mutate the reference portfolio, and scenario values may not produce authoritative statistics.

**Contract effect.** `P0-SCENARIO` requires a separate namespace/portfolio and an isolation test at the object and ledger level. Inline hypothetical `ScenarioValue` fields remain insufficient.

**Implementation (Batch 5).** `src/nse_pipeline/backtest/scenarios.py` provides `ReferenceSnapshot`, `ScenarioPortfolio`, `ScenarioMark`, `ScenarioNav` and `ScenarioStatistics`. Scenarios derive separate cash and holdings from an immutable reference, label NAV hypothetical, expose `authoritative_nav=None` and reject authoritative statistics. Evidence: `tests/test_accounting_scenarios.py`, `tests/test_accounting_batch5_integration.py` and `tests/golden/scenario_cases.json`. A production reference book and stress run are not certified, so the row is `CONTRACT_PARTIAL`.

## D-31 — Friction-aware liquidation

**Owner decision (2026-11-14).** Use the applicable dated sell-side cost schedule, explicit adverse slippage and evidence-backed liquidity/capacity limits. If these cannot be determined, report liquidation proceeds as unknown or as an explicitly hypothetical scenario. Never assume a guaranteed fill, zero-cost default or fabricated proceeds. Keep authoritative NAV separate from estimated liquidation value.

**Contract effect.** `P0-FULL-LIQUIDATION` remains distinct from the zero-friction `P0-LIQUIDATION` PASS. Orders are integer, capacity-bounded and fee-inclusive; unpriced assets have no liquidation value.

**Implementation (Batch 5).** `src/nse_pipeline/backtest/liquidation.py` provides `LiquidationCostSchedule`, `SaleOrder`, `LiquidationPrice`, `LiquidationCapacity` and `LiquidationEstimate`. Dated sell-side costs are charged once per order, slippage is explicitly adverse, capacity limits the fill, and unknown price/capacity yields `None` rather than zero. Evidence: `tests/test_accounting_liquidation.py`, `tests/test_accounting_batch5_integration.py` and `tests/golden/liquidation_cases.json`. Dated real schedules and verified capacity are absent, so the row is `CONTRACT_PARTIAL`.

## D-32 — Prefix invariance scope

**Owner decision (2026-11-14).** Freeze every past economic and decision artifact as it was known at that historical information horizon: cash, receivables, holdings, marks, NAV validity, universe membership, signals, ranks, orders, fills and provenance. Later data or late evidence must not change those artifacts in the same frozen run. Any revised history is a separately versioned run.

**Contract effect.** `P0-PREFIX` compares the economic/provenance projection of a prefix run with the same prefix of a longer run. Run metadata may differ; economic state and provenance may not. Batch 5 proves local future-evidence invisibility in continuity, scoped adjustment and lifecycle; Batch 6 provides only test-only prefix-invariance regression coverage. `P0-PREFIX` remains `CONTRACT_NOT_IMPLEMENTED`, and Batch 6 does not change its status.

---

## Unresolved decisions requiring owner input

The table records the owner disposition. An `APPROVED` row is binding; implementation steps that depend on it are no longer `DECISION REQUIRED`. `U-04` and `U-05` remain `BLOCKED` until the required external evidence is collected, hashed, reconciled and approved.

| ID | Question | Recommendation | Why it blocks | Status |
|---|---|---|---|---|
| U-01 | Does `max_order_notional` bound principal or total payable? | Principal only (D-02); a total-payable limit would be a new field | F1 acceptance tests | **APPROVED 2026-11-14** |
| U-02 | Is `hold_receivable` a permanent modeling assumption or a pending state? | Pending, with evidenced settlement/cancellation through immutable events; never silently erase (D-04) | F5 lifecycle and NAV | **APPROVED 2026-11-14** |
| U-03 | Is momentum sample sufficiency whole-history or window-based? | Window-scoped, `None` = full window, history count diagnostic (D-06) | F6 ranking contract and schema 2 | **APPROVED 2026-11-14** |
| U-04 | What is the authoritative source and cadence for historical exchange calendars? | Official NSE Capital Market calendars and dated NSE circulars; digest-bound, reconciled; no code-embedded holiday table (D-11) | P0-BOUNDARY | **BLOCKED — source approved; evidence outstanding** (Batch 7: 2022–2025 CMTR circulars + Muhurat notices collected/hashed/transcribed under `verification/stage1/batch7/calendar-reconciliation.json`) |
| U-05 | What is the authoritative source for historical index membership and suspension/delisting? Is PIT membership Stage-1 or Stage-2? | Official NSE Indices snapshots/announcements, effective dates and NSE status records; separate `NSE_LIQUID_UNIVERSE` and `NIFTY500_PIT`; never current constituents (D-10) | P0-UNIVERSE / P0-LIFECYCLE | **BLOCKED — `NIFTY500_PIT` evidence outstanding; liquidity universe may proceed** (Batch 7: a third-party reconstruction baseline + 5 dated NSE Indices press releases collected/hashed/summarized under `verification/stage1/batch7/membership-reconciliation.json`; per-row reconciliation + owner approval still required) |
| U-06 | Are long `PortfolioSpec` names public API or internal research vocabulary? | Long names canonical; short aliases only at the sizing boundary (D-13) | F12 compatibility | **APPROVED 2026-11-14** |
| U-07 | Should P3 use an explicit caller-supplied screen value and screen price? | Yes; both mandatory for a filled P3 rank; never inferred from a stale mark (D-14) | P1/P2/P3 goldens | **APPROVED 2026-11-14** |
| U-08 | Should internal NAV be exposed unquantized, or only `nav_reported`? | Both: exact `nav` plus presentation-only `nav_reported` (D-03) | F4 API | **RESOLVED by D-03** |
| U-09 | Is an adjusted-price basis in scope for Stage-1 momentum, and who supplies its identity? | Default `close`; `adj_close` requires an explicit caller basis and `BasisProvenance` (D-08) | F2 / P0-SCOPED-ADJ | **APPROVED 2026-11-14** |
| U-10 | How are partial dividend payments, withholding tax and gross-vs-net amounts handled? | **Modified:** partial payments and withholding modeling stay out of Stage 1. Full gross payment is permitted only when labelled `gross_dividend_no_withholding_model`; unsupported partial or net-payment evidence is quarantined, never forced into settled cash | F5 | **APPROVED AS MODIFIED 2026-11-14** |
| U-11 | May `void_dividend` extinguish an opening-balance dividend claim with no schedule? | **Modified:** no arbitrary cancellation. Permit explicitly evidenced correction, settlement or write-off through new immutable ledger events; preserve the original event; never retroactively rewrite prior balances | F5 | **APPROVED AS MODIFIED 2026-11-14** |
| U-12 | Does a confirmed-but-unsettled claim remain in `unresolved_settlement_claims`? | No; it has a due date and is a scheduled receivable, not an unresolved timing claim | F5 disclosure | **APPROVED 2026-11-14** |
| U-13 | Is strict ADV missingness the default, with zero-fill only via an explicit opt-in? | Yes; `allow_partial_adv=True` with a documented listed/no-trade assumption | F9 / P0-ADV | **APPROVED 2026-11-14** |

`U-08` is resolved by D-03 and is retained only to preserve identifier traceability.

---

## Owner decisions recorded 2026-11-14

**Approver:** research owner, final approval authority. These decisions supersede the corresponding planning-final recommendations where they are more specific. They do not promote any contract row; Batch 4–6 still require tests-first implementation and revision-bound evidence.

### Approved design defaults (1–7 and 10)

| Design question | Decision |
|---|---|
| 1. Order cap excludes fees | Approved. `max_order_notional` bounds principal only; total cash affordability is a separate constraint and both must hold. |
| 2. Dividend hold is temporary | Approved. A held receivable may later be settled with verified evidence or explicitly corrected/cancelled; it is never silently erased. |
| 3. Momentum requires recent observations | Approved. Window-scoped sufficiency; lifetime history does not establish recent reliability. |
| 4. Long sizing-policy names canonical | Approved. `P1`/`P2`/`P3` are interface aliases resolved through one authoritative mapping. |
| 5. P3 explicit screen value/price | Approved. Both are mandatory, dated and provenance-carrying; no stale-mark fallback. |
| 6. Missing volume means unknown | Approved. Zero trading volume and missing data are different observations. |
| 7. Confirmed-but-unpaid dividend is scheduled | Approved. Once a verified payment date is available at the information horizon, it is a scheduled receivable. |
| 10. Custom adjusted-price momentum requires explicit basis | Approved. Default remains raw close; adjusted bases require stable identity, version and causal provenance. |

### Modified design questions 8 and 9

**8. Dividend withholding and partial payments.** Partial payments and withholding-tax modeling remain out of Stage 1. A full-gross payment assumption is permitted only when explicitly labelled `gross_dividend_no_withholding_model`; it is not represented as verified net cash for every investor. Unsupported partial-payment or net-payment evidence must be rejected or quarantined, never silently treated as a full payment. A later model may split a claim or add a withholding claim; that is a separate schema change.

**9. Opening-balance dividends.** Arbitrary cancellation is not allowed. An opening dividend receivable may be corrected, settled or written off only with independent documentary evidence, through new immutable ledger events. The original ledger event is preserved; prior portfolio states and historical balances are never retroactively rewritten.

### U-04 — Exchange calendar authority

* **Authoritative publisher:** National Stock Exchange of India.
* **Segment:** NSE Capital Market, not F&O or clearing holidays.
* **Primary source:** `https://www.nseindia.com/resources/exchange-communication-holidays`.
* **Historical circular example:** `https://nsearchives.nseindia.com/content/circulars/CMTR65587.pdf` (NSE/CMTR/65587, dated 2024-12-13, including the special Muhurat session).
* **Evidence per source:** publisher, publication date, URL, original file, SHA-256, applicable trading dates, source version.
* **Coverage:** every year in the chosen backtest period, including holiday amendments, special sessions and Muhurat trading.
* **Reconciliation:** reconcile against official exchange session/report evidence. Never infer a session from a weekday or from the mere absence of a price/bhavcopy. An exceptional Saturday/Sunday session must be included; a missing bhavcopy is not automatically a holiday.
* **Gate:** `U-04`/`P0-BOUNDARY` remains `CONTRACT_BLOCKED` until the agent validates coverage and hashes and the owner approves them.

### U-05 — Historical index membership authority

* **Sources:** NSE Indices Limited official historical constituent snapshots, inclusion/exclusion announcements and effective dates; NSE security-status and delisting records.
* **Reference URLs:** `https://www.niftyindices.com/press-release`; `https://www.niftyindices.com/indices/equity/broad-based-indices/NIFTY-500`; `https://www.nseindia.com/static/list/list-of-companies-proposed-to-be-delisted`.
* **`NSE_LIQUID_UNIVERSE`:** historically observable NSE equities selected by causal eligibility and liquidity filters; independently verifiable and not dependent on index membership.
* **`NIFTY500_PIT`:** historically verified Nifty 500 membership as of each decision date.
* **Prohibition:** do not reconstruct official index membership from today's constituents or a synthetic top-500 ranking.
* **Gate:** `NIFTY500_PIT`/`P0-UNIVERSE` remains `CONTRACT_BLOCKED` until a verified starting snapshot, all inclusions/exclusions, effective dates, contemporaneous announcement dates and ad-hoc changes are collected and approved. This does not block the separately verified `NSE_LIQUID_UNIVERSE`.

### Batch 5/6 domain decisions

| ID | Topic | Owner decision |
|---|---|---|
| D-27 | Continuity | Require explicit dated NSE/company documentary evidence for symbol/ISIN changes, mergers, demergers and successor relationships. Preserve separate identities unless a verified mapping establishes continuity and the nature of the event. No similarity-based guessing or undocumented merging. |
| D-28 | Adjustments | Preserve immutable raw prices. Apply adjustment factors only to the affected security's eligible historical window, using verified corporate-action terms and effective dates. Maintain versioned adjusted series. Distinguish an ex-post research series from a point-in-time-available series; a future split may restate past prices for analysis but its adjustment must not leak into an earlier simulated decision. |
| D-29 | Lifecycle | Distinguish suspension, delisting, temporary non-trading and missing data only through dated authoritative evidence. Missing OHLCV alone never establishes suspension or delisting. Unknown status remains unknown. |
| D-30 | Scenarios | Scenario holdings, costs, cash, NAV, liquidation values and statistics are absolutely separate from authoritative accounting and performance; they never mutate the reference portfolio. |
| D-31 | Liquidation | Apply the applicable dated sell-side cost schedule, explicit adverse slippage and evidence-backed liquidity/capacity limits. If these cannot be determined, report proceeds as unknown or as an explicitly hypothetical scenario. Never assume a guaranteed fill, zero-cost default or fabricated proceeds. Maintain authoritative NAV separately from estimated liquidation value. |
| D-32 | Prefix invariance | Freeze every past economic and decision artifact as known at that historical information horizon: cash, receivables, holdings, marks, NAV validity, universe membership, signals, ranks, orders, fills and provenance. Revised history produces a separately versioned run; it never silently rewrites the original. |

### Cross-cutting implementation rules reaffirmed

1. Never manufacture missing market information. Unknown prices, volume, corporate actions and security status remain explicitly unknown.
2. Never allow future information to change historical decisions, including retrospective corporate-action adjustments and later-published index membership.
3. Keep actual portfolio accounting separate from research assumptions, hypothetical scenarios and liquidation estimates.
4. Do not declare a blocked contract complete without its required evidence. Do not weaken tests to force PASS. Do not change immutable raw datasets, baseline inventory hashes or existing goldens without explicit independent justification and approval.
5. For externally blocked contracts, implement and test input validation, interfaces and fail-closed behavior using clearly labelled synthetic fixtures, and report the production evidence requirement as outstanding.
6. The overall P0 gate remains `CLOSED` until independently verified.

---

## Owner decisions recorded 2026-11-15 (readiness-reopen lane R5)

**Approver:** research owner, final approval authority. These entries adjudicate the two `NEEDS_OWNER_DECISION` clause items recorded in `verification/stage1/readiness-reopen/clause-reconciliation.json` (`OD-R5-1`, `OD-R5-2`). They do not promote any contract row and do not open any gate.

### OD-R5-1 — AT-COV-07 `zero_gross_notional` reachability

* **Date:** 2026-11-15
* **Question:** Is a legitimate zero-valued holding/reference case intended by the domain, given that `zero_gross_notional` is in the closed vocabulary and valuation branch but unreachable through the public input model (positive shares, strictly positive accepted marks, missing marks yield `None`, an empty book classifies as `no_positions` first)?
* **Owner-approved resolution:** Keep the strictly-positive input model. The clause is adjudicated on vocabulary closure only; no zero-valued case is added and no input-model change is approved.
* **Consequence:** AT-COV-07 closes on vocabulary-closure evidence only (the closure test already passes). No new goldens, no validation relaxation. No zero-price mark may ever be manufactured to reach the defensive branch.

### OD-R5-2 — AT-ADV-07 `traded_sessions` gate wording

* **Date:** 2026-11-15
* **Question:** Under current ADV semantics a value-bearing estimate's `traded_sessions` equals its known observed-value count (explicit valid zero-volume bars count as known observations), so coverage is the only sufficiency gate. Does the owner want a separate positive-activity threshold for capacity?
* **Owner-approved resolution:** Amend the clause wording to coverage/known-observation sufficiency. The separate positive-volume `traded_sessions` minimum is redundant under current semantics and is removed from the acceptance wording. No new economic rule is introduced.
* **Consequence:** The `AT-ADV-07` row in `docs/STAGE1_P0_CONTRACT_MATRIX.md` is amended to coverage-sufficiency wording; the clause is provable by the existing passing coverage tests and the redundant-gate basis is recorded stale.

---

## Owner decisions recorded 2026-11-15 (completion follow-up)

**Approver:** research owner, through the interactive decision questions in the
completion session. These decisions do not themselves promote contracts or open
the gate. Implementation and revision-bound proof remain required.

### OD-COMP-1 — Explicit external capital flows

* **Owner answer:** “Approve rule (Recommended)”.
* **Approved rule:** Explicit deposits and withdrawals occur at phase 10 and
  represent outside capital, not investment profit. Withdrawals require known,
  settled, unreserved cash after protecting payable funding; they cannot spend
  unsettled receipts, borrow, or overdraw. Every flow requires provenance and
  independent replay.
* **Consequence:** The previously implemented rule now has owner approval.
  Reverify AT-RECON-04 and the other conservation clauses before changing
  `P0-CONSERVATION` status. Documentary authentication is not implied.

### OD-COMP-2 — Explicit-target trimming direction

* **Owner answer:** “Explicit-target trims (Recommended)”.
* **Approved direction:** The caller supplies target share quantities. Any
  proposed reduction is limited to excess owned shares and delivered shares,
  frozen at decision close, and executed next opening subject to costs and
  capacity. Do not infer weights, dispose of unresolved holdings automatically,
  or spend sale proceeds at the same opening.
* **Still to confirm:** Exact activation/configuration, partial-fill behavior,
  conflicts with other explicit orders, and treatment of unavailable close or
  capacity evidence. The question explicitly promised confirmation of remaining
  execution details; this direction is not approval of unspecified defaults.
* **Consequence:** Design and implement only after those details are settled.
  `trim_overweight_targets` remains RESERVED until wired and verified;
  AT-POL-05 and `P0-POLICIES` remain qualified in the meantime.

### OD-COMP-3 — Stricter signal formula system, not an attestation waiver

* **Owner answer after the plain-language explanation:** “Build stricter formula system”.
* **Approved direction:** Build a restricted, verifiable formula representation
  instead of treating arbitrary Python callable identity/settings as sufficient
  evidence of behavior.
* **Not approved:** Do not narrow AT-SIG-05 to attestation-only guarantees, promote
  existing opaque callables, or remove the signal-provenance blocker by wording
  alone.
* **Further owner answer:** “Include action-adjusted prices”. The first restricted
  version therefore includes closing-price arithmetic and an evidence-aware path
  through the existing point-in-time corporate-action adjustment machinery.
  Missing authoritative action documents remain external blockers; no factors or
  continuity may be inferred.
* **Still to confirm/verify:** The final production admission interface for legacy
  arbitrary callables, and proof that the restricted representation and its
  evidence bindings cannot be overridden by caller identity labels.
* **Consequence:** `P0-SIGNALS` remains PARTIAL while the stricter path is designed,
  implemented and tested. Data/evidence provenance is distinct from formula
  identity; a formula fingerprint does not authenticate its market inputs.

**Trim follow-up:** The owner answered “Change trim details” to the proposed
conservative execution bundle. That bundle is **not approved as a whole**.
OD-COMP-2's explicit-target direction stands, but no unspecified replacement
execution rule is approved; implementation must await the requested changes.

### OD-COMP-4 — Strategy-neutral rupee instructions

* **Owner answer:** “Yes, strategy supplies amounts”.
* **Approved refinement of OD-COMP-2:** Accept distinct explicit instructions for
  an incremental buy budget (“spend up to ₹X”) and a target holding value (“hold
  approximately ₹Y”, potentially requiring a reduction). The strategy—not the
  accounting module—chooses allocation percentages and declares whether their
  denominator is available cash, portfolio value, or another approved input.
* **Consequence:** Do not hard-code 5%, equal weighting, or a cash/NAV denominator.
  The earlier explicit-share-target direction is refined to explicit rupee
  targets. Conversion to integer raw shares requires an explicit rounding rule;
  final trim execution details remain pending confirmation.

### OD-COMP-5 — Predeclared opening-price conditions

* **Owner answer:** “Predeclared opening conditions”.
* **Approved mode:** Freeze the condition before the next opening. Evaluate it
  using the separately observed opening print only, never that session's close,
  high, low, completed volume or turnover. Any modeled opening fill retains
  explicit execution assumptions.
* **Not approved:** Do not observe the opening, make an unrestricted new strategy
  decision, and silently claim execution at that same official opening price.
  A general after-opening decision mode requires later executable observations.
* **Consequence:** Implement typed immutable opening conditions and independently
  replay their outcomes. Preserve the original unconditional next-open mode.

### OD-COMP-6 — Integer targets and strict trim admission

* **Owner rounding answer:** “33 shares (Recommended)” for a ₹1,000 target at a
  ₹30 decision price. The target share count is therefore
  `floor(target_holding_value / decision_close)`; any proposed reduction is
  limited by owned and delivered shares. Opening gaps and partial fills can
  prevent the achieved holding from matching the decision-time target.
* **Owner missing-data correction:** “why to skip .. this situation should naver
  came right ? throw the error in this situation”.
* **Approved error behavior:** An explicitly requested trim/trade lacking its
  required current price/capacity evidence, or targeting an unresolved holding,
  must fail visibly and atomically. Do not silently omit that instruction or
  remove the holding from the book. The earlier proposed skip-with-reason
  behavior is superseded for explicit trim admission.
* **Distinction:** Eligibility screening can still exclude new candidates before
  the strategy issues orders. A known false opening condition, an already
  satisfied target, and a documented execution limit are intentional no-trade
  outcomes, not fabricated missing-data success.
* **Preserved constraints:** Selling one security and buying a different one is
  not an instruction conflict. Preserve settled-cash-only funding, supplied
  partial-fill/expiry assumptions, and existing rejection of share-unit changes
  that invalidate frozen sell quantities. Do not introduce automatic target
  weights or same-open financing.

### Post-decision implementation note (host, 2026-11-15)

This note records implementation state; it does not rewrite the historical
decision text above or by itself promote a contract.

* **OD-COMP-1:** the external-flow economic rule is approved and implemented.
  `P0-CONSERVATION` remains qualified because a deposit while a payable amount
  is unknown is still rejected by the conservative `reconcile` `funded_cash`
  check, and citation substitution lacks a trusted source digest. The check was
  not weakened. *(The unknown-payable admission and substitution-detection
  boundaries are superseded by the Batch 9 note below; source authentication
  remains open.)*
* **OD-COMP-2 / OD-COMP-4 / OD-COMP-6:** the explicit rupee holding-value
  planner, floored integer targets, atomic missing-evidence errors, frozen
  capacity/condition checks and next-opening settlement path are implemented and
  tested. `CURRENT_DISPOSITIONS` marks `trim_overweight_targets` implemented;
  the historical `BASELINE_DISPOSITIONS` snapshot remains unchanged. The direct
  slot/trim comparison supports `P0-POLICIES` `CONTRACT_PASS`; final
  revision-bound evidence remains host-pending.
* **OD-COMP-3:** the strict formula path, including action-adjusted prices and
  metadata-integrity checks, is implemented and tested. Production admission
  rejects opaque callables, caller `basis_id` labels and caller-authored price
  snapshots. `P0-SIGNALS` remains PARTIAL because AT-SIG-05's literal universal
  guarantee is still unproven for the retained legacy callable interface; this
  is not an attestation-only waiver.
* **OD-COMP-5:** predeclared opening conditions are implemented and replayed from
  the raw opening print; false conditions skip without fees or claims and release
  buy reserves. No after-open decision claims the same opening retrospectively.

### Batch 9 implementation note (host, 2026-11-16)

This note records working-tree implementation state; it does not promote a
contract by itself and does not authorize a strategy run.

* **Conservation:** the unknown-payable deposit false rejection is closed.
  `reconcile` admits a kernel-valid deposit only when replayed and snapshot
  funded cash are both unknown and no withdrawal occurred while any payable
  amount was unknown. A withdrawal in that state remains a hard atomic failure;
  known-liability nonnegative/equality checks are unchanged. External flows may
  now carry a caller-supplied source digest; the kernel binds it to the citation
  and event facts and replay recomputes the binding, so citation/source-digest
  substitution fails closed. The rule is integrity binding, not authentication.
  `P0-CONSERVATION` therefore remains `CONTRACT_PARTIAL` only at the owner-held
  source-anchor/authentication-scope boundary. Focused lane evidence:
  `verification/stage1/batch9/conservation/README.md` (143 passed).
* **Scenario / full liquidation:** a frozen 2022-03-31 reference book built
  read-only from `data/market.duckdb`, a dated explicit tariff and a 20-session
  5%-of-traded-value capacity model now drive an isolated liquidation run and
  `ScenarioPortfolio` reconciliation. The reference fingerprint is unchanged,
  future marks are invisible, fees are charged once per order, no fill exceeds
  capacity and unknown price/capacity yields no fill. The book, tariff and
  capacity remain constructed/modeled, not client-authoritative, broker-
  authoritative or verified executable liquidity. `P0-SCENARIO` and
  `P0-FULL-LIQUIDATION` remain `CONTRACT_PARTIAL` on those certification gaps.
  Focused lane evidence: `verification/stage1/batch9/liquidation/README.md`
  (35 passed including 6 lane tests).
* **Signals:** the known AT-SIG-05 bound-method counterexample is now closed:
  two receiver instances with the same caller-supplied `basis_id` and parameters
  publish distinct `source_digest`s because the admitted callable's live state is
  canonicalized and hashed. Opaque or non-freezable state fails closed, and the
  legacy `rank_momentum` path re-fingerprints after ranking, raising
  `callable_basis_state_changed_during_ranking` rather than publishing stale
  provenance. The strict production paths remain intrinsic and reproducible.
  The literal universal behavioral-distinction wording remains formally unproven
  for adversarial callables (state mutated and restored during ranking, external
  or clock reads, descriptor-backed external state, replaced bytecode).
  `P0-SIGNALS` remains `CONTRACT_PARTIAL` pending an owner disposition: accept
  the enforceable live-state fingerprint and its stated limits, or remove the
  legacy callable interface from the contract surface. Focused lane evidence:
  `verification/stage1/batch9/signals/README.md` (99 passed).
* **Identity / lifecycle integration:** the production `SessionRunner` now accepts
  optional typed `IdentityEvidence` and `LifecycleEvidence`. Due migrations act
  only at `PRE_OPEN_ACTIONS` after both availability and effective date, re-key
  live positions, orders and entitlements while emitted historical events retain
  their stable keys, and re-key pending frozen orders. Retirement reuse and any
  late-session failure roll back the whole step. Lifecycle admission blocks
  negotiation for every non-`LISTED` state and keeps NAV unknown for
  `UNKNOWN`/`DELISTED` without inventing last-close proceeds; verified terminal
  proceeds are recorded but create no synthetic cash. `decision_at` caps the
  information horizon. All inputs are synthetic/caller-supplied and
  unauthenticated, so `P0-IDENTITY`, `P0-LIFECYCLE` and `P0-E2E` remain
  `CONTRACT_PARTIAL`. Focused lane evidence:
  `verification/stage1/batch9/identity/README.md` (14 lane tests; 233-test
  combined selection; full regression 1,906 + 129 subtests).

---

## Owner decisions recorded 2026-11-16 (Batch 9 owner sign-offs)

**Approver:** research owner, final approval authority. The owner answered the
five `verification/stage1/batch9/owner/` requests in conversation on
2026-11-16. These entries record those answers; they do not by themselves
promote any contract row or open the gate. Implementation and revision-bound
verification remain required.

### OD-B9-1 — Calendar acceptance (`P0-BOUNDARY`)

* **Owner answer:** “Accept, to 2026-10-31”.
* **Approved scope:** The Nifty Indices official daily calculation dates are
  accepted as the contemporaneous session source; the MSD notices used for the
  2019-10-21 closure and the three 2024 DR special sessions are accepted; the
  revised-timings citation `NSE/CMTR/42483` for the 2019-10-27 Muhurat session
  is accepted; the 72-date bhavcopy sample is accepted as a cross-check, not as
  exhaustive confirmation of all 1,983 sessions. Coverage end is the evidence
  boundary `2026-10-31` (106 complete month-end calendars).
* **Record:** `verification/stage1/batch8/calendar/owner-acceptance.json`
  (kind `nse_calendar_owner_acceptance`, bound to evidence SHA-256
  `4b8310dba99bcd525f93ec2272fb10974566a56e4f4895eb47c13d2095319883`). Loading
  it against `session-evidence.json` and building `monthly_calendars` succeeds
  and yields 106 month-end calendars, 2018-01 through 2026-10.
* **Consequence:** `P0-BOUNDARY` can now be assessed with digest-bound
  production monthly calendars through 2026-10-31. Assessment and any status
  change remain separate, revision-bound work.

### OD-B9-2 — Continuity corpus scope (`P0-CONTINUITY`)

* **Owner answer:** “Require complete corpus”.
* **Approved scope:** A complete publisher archive or an owner-approved
  equivalent corpus is required before `P0-CONTINUITY` can pass, including
  closure of the residual windows 2018-01-01..2018-03-30 and
  2022-05-04..2022-05-30 and dated symbol/ISIN-change evidence.
* **Consequence:** The row stays blocked until that corpus exists. The existing
  58 screened document-backed edges remain working evidence only. The four
  additional confirmations (legal-name keys, unbound `YAARI` label, PEL split,
  RAYMOND chain) were not separately asked and remain unconfirmed.
* **Fulfilment (partial, 2026-11-17):** The Batch 10 corpus extension stored
  1,113 of 1,119 in-interval NSE Indices releases and proved 15 additional
  two-sided edges (59 parent boundaries total), narrowing the residual windows
  to 2018-01-01..2018-02-04 and 2026-09-08..2026-11-16; six releases remain
  publisher-unretrievable and no `symbol_change`/`isin_change` evidence was
  found corpus-wide, so the row stays `CONTRACT_BLOCKED` under this decision.

### OD-B9-3 — Membership evidence scope (`P0-UNIVERSE`)

* **Owner answer:** “Point evidence + post-2022”.
* **Approved scope:** The 108 exact-date NIFTY 500 snapshots (2013-04-18..
  2022-03-31) are accepted as point membership evidence only. No date between
  snapshots may inherit membership and no continuous daily membership claim is
  authorized. The post-2022 chain **is required** for the Stage-1 gate.
* **Consequence:** The row cannot pass on the 108 snapshots alone; post-2022
  exact-date snapshot evidence must be acquired and admitted first. The alias/
  timing disclosures in the batch 8 `delta_audit.json` remain disclosed
  limitations (not separately confirmed).
* **Fulfilment (2026-11-16):** The batch9/universe lane admitted 65 exact-date
  anchors (2022-03-31..2026-09-30) built walk-forward from official NSE Indices
  press releases only (396 retained digest-bound PDFs, 16 corporate-action
  groups, 32,670 one-day intervals). `P0-UNIVERSE` is promoted to
  `CONTRACT_PASS` on this evidence; dates between anchors and after 2026-09-30
  remain `CoverageUnknown`, three unresolved exclusion rows and the `DUMMYHEG`
  chain-end position are disclosed, and no daily membership claim is made.

### OD-B9-4 — Conservation authentication scope (`P0-CONSERVATION`)

* **Owner answer:** “Yes, Option B (Recommended)”.
* **Approved scope:** `AT-RECON-04` requires replay-verifiable citation/
  source-digest substitution detection, not authentication of the underlying
  bank or broker document. The residual gap — a coordinated forger can replace
  the citation, the source digest and the recomputed binding — is accepted.
  Unknown-payable deposits stay admissible, unknown-payable withdrawals stay
  hard atomic failures, and known-liability `funded_cash` checks are unchanged.
* **Consequence:** The row's remaining authentication qualification is an
  explicit owner scope decision. This does not authorize a strategy run,
  returns or a gate opening; every other contract must still pass.
* **Fulfilment (2026-11-17):** Revision-bound evidence was regenerated at
  c6044d5: the five row suites (`test_accounting_reconciliation.py`,
  `test_accounting_replay.py`, `test_accounting_external_flows.py`,
  `test_accounting_observed_session_external_flows.py`,
  `test_accounting_replay_independence.py`) ran 114 passed, 0 failed,
  0 skipped. `P0-CONSERVATION` is promoted to `CONTRACT_PASS` on this evidence;
  the conservative check was not weakened.

### OD-B9-5 — Signals guarantee scope (`P0-SIGNALS`)

* **Owner answer:** “Accept enforceable rule (Recommended)”.
* **Approved scope:** The live-state fingerprint plus post-ranking recheck is
  the approved AT-SIG-05 guarantee, with the four listed adversarial limits
  explicitly accepted: state mutated and restored within a call; environment/
  filesystem/clock reads; descriptor-backed external state; replaced bytecode
  under identical source text.
* **Consequence:** `P0-SIGNALS` evidence may now be reassessed for
  `CONTRACT_PASS` under the enforceable rule. The reassessment and any status
  change remain separate, revision-bound work. The strict production formula
  paths are unaffected.
* **Fulfilment (2026-11-17):** Revision-bound evidence was regenerated at
  c6044d5: the five row suites (`test_accounting_signals.py`,
  `test_accounting_signal_sizing_clause_contract.py`,
  `test_accounting_strict_formulas.py`,
  `test_accounting_strict_formula_integration.py`,
  `test_accounting_strict_formula_integrity.py`) ran 85 passed, 0 failed,
  0 skipped. `P0-SIGNALS` is promoted to `CONTRACT_PASS` on this evidence; the
  four adversarial limits remain documented for the legacy callable interface
  only.
