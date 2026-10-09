# Signal entry timing and diagnostics

The October 7–8 history contained five candidates and no confirmations. Four
records retained a live breach, three retained qualifying 1-minute structure;
their terminal reasons were two timeouts, two early R:R failures and one option
eligibility failure. These records do not contain all intervening ticks or
quotes and cannot establish hypothetical fills or profitability.

## Candidate deadlines

The configured candidate lifetime remains 900 seconds by default. A fresh live
breach received before that deadline can extend it to the next 5-minute candle
close plus the observation freshness allowance (60 seconds). Extension is
bounded by the original deadline plus 360 seconds and the configured new-entry
cutoff (15:00 IST by default). Once extended, later breaches do not roll the
deadline forward. The deadline persists across restart.

For example, a candidate created at 12:05:06 with a breach at 12:20:02 can now
wait until 12:26:00, allowing evaluation of the 12:25 close. This is additional
observation time, not confirmation. A late-delivered tick cannot revive an
expired candidate. Existing terminal records and same-session structural
deduplication remain unchanged.

## Breakouts and retests

A candidate created partway through a 5-minute candle can use that candle only
when a fresh live breach was witnessed after candidate creation, within the
candle, and inside its price range. Completed/nonpartial candle, freshness,
direction, contract and R:R checks still apply. Stop-first handling of ambiguous
OHLC remains conservative. Current price must still be beyond the trigger.

A `breakout_retest` plan created beyond an already-broken level does not treat
its first already-beyond tick as a new breach. Early entry requires a subsequent
live observation at/across the level and a fresh breach. Normal 5-minute retest
confirmation remains available. Two completed post-breach 1-minute candles
are still required for EARLY_SETUP; early setups are not confirmed opportunities.

## Risk/reward and missed moves

Planned R:R uses the structural trigger. Current entry R:R uses the fresh live
underlying with the same stop and T1; the minimum remains 1.5 by default.
If current entry R:R falls below the minimum before confirmation while price
is beyond the trigger, the candidate waits for a pullback within its existing
deadline. An EARLY_SETUP is demoted, and its previous trigger evidence is
cleared. It cannot be manually confirmed until qualification, R:R and fresh
timing evidence qualify again. Stops and targets are never moved to improve R:R.

If T1 is observed before entry, the setup becomes INVALIDATED with the explicit
reason `First target reached before entry; planned opportunity already consumed`.
The model does not record a profitable trade or target hit for a missed entry.
Normal 5-minute confirmation still uses the worse of candle close/current price.

## Option eligibility

New plans prefer liquid ATM, then one-step ITM. Existing plans revalidate their
exact token, symbol, expiry, strike and option type against the current eligible
set. A different preferred ATM no longer invalidates an existing one-step ITM.
OTM/deeper ITM, stale or illiquid contracts, changed expiry, incomplete metadata
and mismatched identities are still rejected. The plan never silently switches
contracts. Manual early-entry validation uses the same rule.

## Display and evidence

The signal API adds `entry_diagnostics` with current underlying/R:R, minimum
R:R, candidate deadline, status and reason. Active candidate records also retain
the latest evaluated entry price/R:R in the journal for diagnosis. Stale views
do not present a current R:R.

The dashboard distinguishes no setup, waiting for trigger, retest, pullback,
confirmation, blocked qualification and terminal states. A retained breach is
labelled historical evidence. The qualification popover keeps status words
readable on mobile and scrolls horizontally for long gate explanations.

Scoring thresholds, category alignment and contradiction rules are unchanged.
The tests in `tests/test_signal_entry_regressions.py` reproduce the numerical
constraints and exercise synthetic continuations. They are regression tests,
not a backtest or a reconstruction of unrecorded market activity.
