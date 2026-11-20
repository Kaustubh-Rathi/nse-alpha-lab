# Stage-1 production accounting inputs

`nse_pipeline.backtest.observed_session.SessionRunner` is a production
accounting/execution composition over caller-supplied inputs. It is not a strategy,
an authenticated exchange-data adapter, or historical-readiness authorization.
The overall P0 gate remains **CLOSED**.

This guide describes admitted interfaces and mechanics, not canonical contract
status. The owner follow-up decisions `OD-COMP-1`..`OD-COMP-6` are recorded in
`docs/STAGE1_DECISION_REGISTER.md`; implementation does not itself promote a
contract, and final status counts remain host-owned and pending adjudication.

## Atomic session boundary

Construct a runner with explicit initial cash, date, initial provenance, costs and
execution assumptions. Optional opening positions/claims carry initial provenance.
Call `step(SessionInput(...))` once per explicitly observed session, beginning on
the construction date.

Each successful step returns immutable phase snapshots, typed ledger events,
valuation, frozen next-session orders/sells, consumed provenance and independent
reconciliation. The runner commits only after validation and replay succeed.
A failed step leaves the prior book, event stream and result prefix unchanged.

| Phase | Orchestration |
|---|---|
| 10 | Admit the session and explicit external capital flows |
| 20 | Settle due trade/other claims |
| 30 | Expire evidenced missing-print buys; apply share actions and dividends |
| 40 | Evaluate frozen opening conditions, then execute frozen sells before buys using separate opening prints |
| 50 | Settle due post-opening claims, including eligible dividends |
| 60 | Consume completed bars and value the book |
| 70 | Plan explicit holding-value trims, then freeze next-session intents and fee-inclusive reserves |
| 80 | Wrap the audit snapshot without fabricating an accounting event |

An opening print is a distinct input, never a completed bar's open/high/low/close.
Missing prints for still-live frozen orders require per-order `HaltEvidence`;
absence alone never authorizes a fill or silent expiry.

## Opening-price conditions

`OpeningCondition(min_price=..., max_price=...)` is a typed, immutable inclusive
band frozen with a buy, sell or trim target before the next session opens. Bounds
are exact positive decimals, strings or integers; floats and bools are rejected,
and a missing or inverted band is an error. The condition is evaluated only
against the separately supplied raw opening print, never that session's close,
high, low, completed volume or turnover, and never the slipped fill price.

A passed condition fills under the ordinary execution and cost assumptions; an
adverse slippage adjustment can still move the execution price away from the raw
opening print used for the decision. A known false condition is an explicit,
immutable skip at phase 40: it creates no fee, claim, fill or position change, and
a skipped buy releases its reservation exactly once. Missing opening evidence is
still not a skip; it requires explicit `HaltEvidence`.

Only predeclared conditions are admitted. There is no general after-opening
decision mode, and no new unrestricted decision made after observing the opening
may claim that same opening's official price.

## Phase-30 inputs

`SessionInput` accepts empty-by-default immutable tuples:

- `share_actions`: `ShareActionInput(action, position_id, provenance)`.
  `position_id=None` selects the kernel's order-only cancellation path.
- `dividend_entitlements`:
  `DividendEntitlementInput(position_id, terms, provenance)`.
- `dividend_accruals`: `DividendAccrualInput(dividend_id, provenance)`.
- `dividend_confirmations`:
  `DividendConfirmationInput(dividend_id, payment, provenance)`.
- `dividend_voids`: `DividendVoidInput(dividend_id, evidence, reason, provenance)`.
- `dividend_corrections`:
  `DividendCorrectionInput(dividend_id, corrected_amount, evidence, reason, provenance)`.

They execute in the order above, retaining each tuple's order. Each category
rejects duplicate identifiers. Provenance must be available by phase 30; the kernel
also checks the terms' own effective date, key and evidence availability.
Entitlements require an explicit `valuation_policy=ValuationPolicy(...)` on the
runner. Same-day gross payment can settle only at phase 50, not fund that day's
opening buys. Withholding and partial-payment models remain unsupported.

The kernel cancels same-key buy orders affected by share actions. A share action
affecting a frozen sell position rejects the entire session rather than applying
a pre-action quantity to post-action units. No adjusted sell quantity is invented.
Simultaneous share-action/dividend unit ambiguity remains quarantined.

## Signal price basis and strict formulas

`nse_pipeline.backtest.formulas` admits a bounded, deterministic expression
language, not Python code. A `Formula` is an immutable expression tree plus exact
`Fraction` parameter bindings. Admitted leaves are `raw_close` and
`scoped_adjusted_close`; admitted operations are constant, parameter, add,
subtract, multiply and divide. Tree size, depth, numeric magnitude and result size
are bounded, division by zero fails closed, and nonpositive prices are unavailable
rather than filled. A canonical JSON record and its SHA-256 identity bind the
declared expression and its parameter values; that fingerprint does **not**
authenticate the market or action evidence used to evaluate it.

Production admission has two routes:

- `rank_admitted_momentum(ledger, as_of, eligible, spec)` ranks the built-in raw
  close basis and accepts no caller callable or `basis_id`.
- `rank_formula_momentum(ledger, as_of, eligible, formula, spec=..., adjustment_evidence=...)`
  ranks one validated strict formula. A raw-only formula requires
  `price_basis="close"`; an action-adjusted formula requires
  `price_basis="adj_close"`. A formula mixing raw and adjusted leaves is rejected
  as ambiguous.

`compose_targets(..., formula=..., adjustment_evidence=...)` admits the same
validated strict formula for signal ranks. The execution decision close remains the
valid same-session raw ledger close even when ranking uses adjusted prices. Ranked
signal prices stay exact `Fraction` values; they are never rounded through
`Decimal`, and they never replace the raw execution price.

The compatibility `rank_momentum` callable/`basis_id` interface remains
experimental and is not production admission. It is not presented as satisfying
AT-SIG-05's literal behavioral-uniqueness guarantee, and nothing here narrows that
clause to an attestation-only waiver. Canonical clause disposition remains
host-owned.

Action-adjusted formulas require an explicit caller `AdjustmentEvidence` or
per-key `AdjustmentEvidenceSet` with status `verified`; the declaration is
unauthenticated input metadata. A missing, wrong-key, unknown, quarantined,
unsupported or unavailable declaration yields an explicit unavailable reason
instead of a substitute price. Factors are filtered by the decision event, history
is truncated at the as-of horizon, and future-spanning or future-effective evidence
cannot change an earlier value or provenance. The existing scoped-adjustment
machinery decides factor applicability, scope and version. Authoritative corporate
action documents remain an external dependency.

## Explicit external capital

`external_flows=(ExternalFlowInput(flow_id, amount, direction, provenance), ...)`
executes at phase 10 in supplied order. Use typed
`ExternalFlowDirection.DEPOSIT` or `.WITHDRAWAL`; amount must be a positive exact
Decimal, decimal string or integer, not a float.

The kernel emits `flow:<flow_id>` with signed `cash` and `external_flows` postings.
This moves outside capital, not income, and creates no claim, holding or order.
Withdrawals cannot consume reservations, payable funding, unknown available cash,
or receipts that settle later in the session. Provenance supplies the citation
and remains in the consumed prefix. Later session failure rolls back the flow.

The economic rule is owner-approved under `OD-COMP-1`: explicit external capital is
outside capital, not investment profit, and only known settled, unreserved cash
after payable funding can fund a withdrawal. The mechanism is implemented and
independently replayed.

A kernel-valid deposit while a payable amount is unknown is now admissible in
`reconcile` and through `SessionRunner`. Admission requires replayed and snapshot
funded cash to be equally unknown and no withdrawal to have occurred while any
payable amount was unknown. A withdrawal in that state remains a hard atomic
failure; known-liability `funded_cash` checks remain nonnegative and exactly
equal, unchanged.

`record_external_flow(..., source_digest=...)` binds the flow citation and source
digest into the ledger event, and replay recomputes the binding. Citation or
source-digest substitution therefore fails closed. This is integrity, not
authentication: a coordinated forger can replace the citation, replace the source
digest and recompute a self-consistent binding. `P0-CONSERVATION` remains
qualified only on that source-authentication boundary until the owner supplies a
trusted anchor or explicitly decides that replay-verifiable substitution
detection satisfies the clause.

## Explicit-target trims

`nse_pipeline.backtest.trims.plan_trims(book, on, requests, *, bars, capacities,
portfolio)` is a pure, causal composition step: it moves neither shares nor cash and
creates no order, fill, claim or financing. Each `TrimRequest(order_id,
position_id, target_holding_value, rank)` supplies one explicit rupee target for one
existing position. The module infers no weight, uses no whole-portfolio NAV or cash
denominator, and never selects positions by itself.

The integer target is `floor(target_holding_value / decision_close)` using exact
`Fraction` arithmetic against the caller-supplied valid raw same-session close. The
proposed reduction is `min(delivered_shares, shares - target_shares)`. A zero target
is an explicit full reduction, not a missing value. A reduction at or below the
target, or one with no delivered shares, is an intentional persisted no-op with its
reason; it is never a silent skip.

Missing information is an error, never a skip. The whole call rejects atomically
when a requested position is unknown, inactive or blocked/unresolved; when its
same-session bar is missing, invalid, zero/negative, duplicate or from another
session; when a positive reduction has missing or unknown (`None`) capacity; when
identifiers are duplicated; when the book session does not match; or when explicit
requests are supplied while `PortfolioSpec.trim_overweight_targets` is false.
Beginning with `OD-COMP-6`, the earlier skip-with-reason behavior is superseded for
explicit trim admission. An existing position is never erased to make a request
succeed.

A supplied capacity of exactly zero is **known** absence of executable liquidity,
not unknown capacity. The proposed quantity is still reported with a zero cap and a
`known_zero_capacity` reason; the runner must not fill it. The planning module does
not shrink the requested quantity to capacity; partial fills, expiry, costs,
settlement and share-action safety remain the runner's rules.

`SessionRunner` admits the same intent through `SessionInput.trim_targets`, a tuple
of `TrimTarget(request: TrimRequest, capacity: CapacityInput | SellCapacityInput,
provenance: InputProvenance, condition: OpeningCondition | None = None)`. Trims
require an explicit `PortfolioSpec` on the runner with `trim_overweight_targets=True`
and a `max_positions` value equal to the runner's. A missing spec, a disabled trim
flag or a mismatched slot limit rejects the session rather than silently overriding
the configuration.

Planning runs at phase 70, after valuation and before buy reservations, over the
session's valid raw closes and the frozen per-target capacity and condition. Each
positive decision becomes a frozen next-opening `SellTarget` carrying that same
frozen capacity and condition, so no later input can replace them. A trim may not
duplicate an explicit sell of the same position or an explicit buy of the same
security, and two trim targets are never netted against one security; selling one
security while buying a different one is not a conflict. `SessionResult.trim_decisions`
persists every decision, including intentional no-ops and their reasons.

## Cash, rounding and settlement

Whole-share rounding never creates outside capital or free cash. A ₹1,000
fee-inclusive buy budget at a ₹30 decision price reserves and fills 33 shares
(₹990) and leaves ₹10 available when no fees are charged; the remainder is neither
spent nor invented. With fees, the reserve is fee-inclusive and the remainder is
correspondingly smaller.

A ₹1,000 holding-value target at ₹30 floors to 33 shares. Trimming 100 shares to 33
proposes 67 shares; at ₹30 those sale proceeds are ₹2,010 and become cash and
available cash only through the runner's normal trade settlement, never at the same
opening and never as an artificial credit. The rounded remainder is realized only
by actual fills and settlement.

## Target composition

`nse_pipeline.backtest.targets.compose_targets(...)` consumes an observation
ledger, an as-of session, all six explicit specifications, `TargetRequest` records,
settled cash, security status and the partial-ADV choice. It does not read runtime
cash from the reserved `PortfolioSpec.initial_cash` field.

The immutable `TargetPlan` retains research/explicit official membership, momentum
ranks, capacity, principal allocations, sizing reasons, and explicit exclusions.
Ranks may come from the built-in raw-close basis or from one validated strict
formula, including an action-adjusted formula; the execution decision close used
below is always the raw same-session close, never an adjusted signal price. Only
requested securities participate. Missing capacity is `None`, not zero; neither
unknown nor zero capacity creates an order target. Candidate notional is
capacity-clipped before canonical `size_portfolio_orders` sizing.

For P3, callers declare `unresolved_keys` and supply `ScreenMark` value/price
records. A missing screen mark remains unavailable. The composition derives
`requested_quantity = floor(allocated_notional / decision_close)` using the valid
same-session close, even when P3 sizing used a different screen price.
`rank` is the signal rank; `order_index` is the contiguous candidate sizing index.

Plans do not submit or reserve orders. A caller translates nonzero `plan.targets`
into `BuyTarget` records with explicit order IDs, provenance, capacity and
fee-inclusive budgets. Execution can still reduce/skip the frozen quantity at the
next opening. Existing holdings and their occupied slots, outstanding reservations,
settlement, and financing are outside target composition; callers must not mistake
the supplied cash for automatically verified available book funding.

## Capacity and liquidity

The declared default capacity proxy is trailing-20 observed-session average daily
traded value multiplied by a 5% sizing participation rate, floored to paise. The
window is observed sessions, not a reviewed exchange calendar, and "daily average"
means end-of-day turnover. It is not opening-auction volume, imbalance, order-book
depth or executable capacity at the open, and no dataset here supplies those. For
target composition and buy admission, a capacity of `None` is unknown and creates
no target; an explicit zero is known no-executable liquidity and is never conflated
with unknown.

Present equity rows carry non-null volume and turnover across the audited coverage;
absent security or session rows are unknown, not zero-volume observations, and the
data cannot distinguish a halt from a missing row.

Sell and trim capacity is supplied as `SellCapacityInput(principal_notional,
provenance)`, a nonnegative rupee cap with explicit provenance. Zero is **known**
no-executable capacity, distinct from unknown. The rupee cap is floored to paise so
it is never rounded upward into an extra share, and `bounded_sell` applies
`min(global declared cap, supplied frozen cap)`, so a supplied cap can only tighten
the global limit. The legacy `SellTarget(capacity=None)` path remains disclosed for
compatibility; it carries no per-order ADV certification. All capacity is a modeled
daily-turnover proxy, not opening-auction depth or executable opening liquidity. See
`verification/stage1/completion/liquidity-coverage.md` for the audit limitations.

## Boundaries

Calendar and official membership admission are enforced only when their reviewed
ports are supplied; limitations explicitly disclose their absence.
Identity migration and lifecycle-state admission are integrated only when their
optional reviewed inputs are supplied; both remain caller-authored and are not
authenticated. Caller attestations do not authenticate documentary terms or
guarantee behaviorally unique custom signal provenance.

These APIs infer no automatic weighting, no system-chosen security, and no
portfolio-NAV-driven trim rule. Strategy intent remains explicit input: distinct
rupee `BuyTarget` budgets, caller-supplied `TargetRequest` notionals, and explicit
`TrimRequest`/`TrimTarget` holding-value targets. A supplied target is never
inferred by the system, and a trim is never triggered by a weight, rank or NAV
shortcut.

They also produce no strategy return, performance statistic or production evidence;
no measurement-driver result is a strategy return, and none were produced. For the
current contract disposition and revision-bound proof, consult
`docs/STAGE1_P0_CONTRACT_MATRIX.md` and `verification/stage1/completion/`.

The direct AT-POL-05 slot/trim evidence is recorded in
`verification/stage1/completion/policies-trim-adjudication.md`. The host adjudicated
`P0-POLICIES` PASS on its five clauses in the working tree; final revision-bound
evidence remains pending, and the aggregate gate remains **CLOSED**.
