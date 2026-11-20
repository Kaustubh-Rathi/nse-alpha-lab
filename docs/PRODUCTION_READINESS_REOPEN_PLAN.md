# Production-readiness reopening

Status: active implementation; **not** a completion or authorization record.

## Approved scope

The owner requested completion of the actual engine after the research work.
During this reopening, the owner explicitly approved:

1. A production boundary of `SessionRunner.step(SessionInput)`, returning
   immutable session snapshots, ledger events and decision provenance.
2. Explicit strategy targets supplied to the execution/accounting boundary,
   rather than inventing automatic weights, trims or exits.
3. Prior-close cash reservations remain fixed. An upward opening gap reduces
   or skips the integer fill under the existing partial-fill policy. There are
   no additional deposits, borrowing, or same-open use of unsettled sale
   proceeds.

This is a new production workstream, **not** a retroactive expansion of Batch
6's test-only authorization. Legacy runners remain unmigrated. Historical
strategy execution, return/performance publication and P0 gate opening remain
unauthorized.

## Orchestration plan

The main agent owns task selection, assignment, independent review and integration. Implementation is delegated in small, non-overlapping file-ownership slices using the requested DeepSeek v4.1 Flash agents. The provider permits only two simultaneous requests, so additional tasks are queued rather than retried in a loop. No background agent is intentionally left running.

| Priority | Reviewed finding | Delegated next work | Acceptance before continuing |
|---|---|---|---|
| R1 | Production runner first slice is drafted but unreviewed and still lacks full phase-30/evidence integration | Independent adversarial review first, then bounded correction | Correct public seam, atomicity, fees/settlements, frozen sells, immutable prefix and targeted regressions |
| R2 | Automatic signal provenance previously mixed behaviorally different global-dependent functions | Independent review of the implementation and its tests | State-aware behavior is represented conservatively; no untested loophole promoted to a contract pass |
| R3 | Annual holiday lists do not constitute complete calendars; new amendment notices contradict annual-only candidates | Review artifact integrity and define the source-bound calendar evidence seam | Exact byte/date binding, distinct notices/availability, explicit unknown coverage, no inferred completeness |
| R4 | Membership CSV now has correct half-open boundary tests but no historical `known_at` | Implement/review exact-date, source-and-availability-bound snapshot adapter | Unknown coverage is not nonmembership; mutations, ambiguity and unavailable observations fail closed |
| R5 | Five old FAIL descriptions are stale; 20 other rows also lack current clause evidence | Audited clauses and small mathematical corrections, never blanket promotions | Named current evidence for each clause and a defensible change-control review |
| R6 | Production corporate actions, dividends, identity, lifecycle, scenarios and liquidation are still not orchestrated by the runner | Bounded phase-specific modules/tests after the first slice review | Exact effects and fail-closed unresolved branches; no synthetic production admission |
| R7 | Canonical reports describe older revisions; research inventory is stale | Re-run tests, regenerate inventories, review revision-bound proof | All failures resolved or named; no gate/status promotion without contract evidence |

Explicitly not delegated as a completion shortcut: paid-data requirements, fabricated history, legacy migration, return/performance generation, or changing expected results without an independent defect diagnosis.

- Diagnose current code, not just stale contract descriptions.
- Test through approved public boundaries, one failing behavior at a time.
- Expected economics come from independent hand calculations or primary
  evidence, never production output.
- Synthetic fixtures are regression tools, not historical admission evidence
  and not sufficient grounds for contract promotion.
- Preserve existing goldens, raw datasets, baseline inventory and historical
  verification artifacts. Write new correction/audit records instead.
- Missing calendar, membership, lifecycle, capacity or documentary evidence is
  unknown; it is not a zero, an exclusion, an ordinary trading day, or permission
  to fabricate a fill.
- Preserve unrelated `src/nse_pipeline/analytics/` and research files.

## Work lanes

| Lane | Scope | Initial deliverable | Acceptance |
|---|---|---|---|
| R1 | Production observed-session execution | New isolated runner and public-boundary tests | Due pre-open settlements precede fills; sells precede buys; opening prints are separate from completed bars; fee-inclusive reserves, frozen ranks and independent replay |
| R2 | Remaining signal provenance defect | Global-state-sensitive callable admission fix | Same source with different relevant global state cannot silently receive identical automatic provenance; red/green evidence retained |
| R3 | Calendar admission honesty | Additive primary-document date/hash audit and diagnostic-only candidate-calendar tests | Annual holiday lists do not certify complete sessions; publication conflicts are exposed and resolved against exact PDF bytes |
| R4 | Membership interval evidence | Exclusive exit-date regression and scoped claims | `[valid_from, valid_to)` is tested on the exact exit/entry day; effective-date consistency is not described as full historical knowability |
| R5 | Contract reconciliation | Clause-by-clause assessment of stale FAILs and unresolved acceptance rules | No blanket status promotion from passing component tests; genuinely unresolved decisions remain explicit |
| R6 | Production evidence integration | Reviewed calendar and membership adapters feeding the runner | Source-byte binding, separate effective/available times, explicit coverage, and fail-closed unknown intervals |
| R7 | Revision-bound integration review | New focused/regression, inventory, checker and clause evidence | Exact tested source revision, preserved historical evidence, no mandatory skips or unexplained failures |

## Initial runner slice limits

The first implementation slice orchestrates supplied observed sessions and
explicit trade targets. It must not masquerade as a complete historical
strategy engine. In particular:

- A supplied session stream does not authenticate an exchange calendar.
- A source citation or SHA-256 validates a reference/binding, not source
  authenticity, semantic completeness or historical public availability.
- Caller-supplied identities are not proof of symbol/ISIN continuity.
- A flat research cost or settlement specification must remain labelled as an
  assumption, not a verified historical schedule.
- Corporate-action and dividend orchestration, evidence-backed lifecycle
  gates, signal/universe/ADV composition and target-policy integration require
  subsequent reviewed slices if not implemented in the first one.

No runner slice is complete merely because this plan names it. Outstanding
limitations must remain in its handoff and readiness evidence.

## Known findings to retest

1. Several original FAIL descriptions appear stale after earlier fixes, but
   need current clause-level verification rather than source inspection alone.
2. A custom price-basis function can reference global state not represented
   in its automatic provenance.
3. The ADV traded-session acceptance requirement needs to be reconciled with
   the approved missingness/coverage policy before choosing a threshold.
4. The acquired calendar transcriptions have conflicting publication-date
   fields and do not prove that every amendment/special session was collected.
5. The community membership interval helper used an inclusive exit boundary;
   upstream intervals are half-open. Its CSV lacks historical `known_at`.
6. The Batch 6 harness admitted full bars before opening execution and was
   tailored to zero costs. It must not be copied into production.

## Lane completion evidence (2026-11-15)

| Lane | Verdict | Evidence |
|---|---|---|
| R1 | Reviewed; corrections applied | verification/stage1/readiness-reopen/runner-review.md, runner-red-green.txt |
| R2 | Approved with corrections; corrections verified (49 green) | verification/stage1/readiness-reopen/signals-review.md, signal-provenance-red.txt, signal-provenance-result.json |
| R3 | Accepted | verification/stage1/readiness-reopen/calendar-amendment-review.md, calendar-amendments/ |
| R4 | Fixed and verified (7/7) | tests/test_nifty_membership_port.py |
| R5 | Complete; owner decisions OD-R5-1 and OD-R5-2 recorded | verification/stage1/readiness-reopen/clause-reconciliation.md, docs/STAGE1_DECISION_REGISTER.md |
| R6 | Accepted (45 targeted green) | verification/stage1/readiness-reopen/runner-evidence-review.md |
| R7 | CLEAN at 2d6989c (1569 passed, 0 failed, 0 skipped; inventory integrity ok) | verification/stage1/readiness-reopen/r7-revision-bound.md/.json, r7-inventory-check.json |

The overall P0 gate remains CLOSED. These lanes fix the named defects and
provide revision-bound evidence; they do not generate strategy returns,
publish performance, or promote contract status without clause evidence.

## Readiness criterion

The engine is ready only after the production pipeline, all mandatory contract
clauses, required historical inputs and revision-bound independent evidence
are satisfied. Until then the overall gate remains **CLOSED**, with remaining
items named rather than called complete.
