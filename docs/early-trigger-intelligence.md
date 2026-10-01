# Early trigger intelligence

Branch: `feature/early-trigger-intelligence`.

This feature adds a deterministic intermediate lifecycle state without changing the scoring model or the existing 5-minute confirmation rule.

## States

- **WAIT FOR TRIGGER / CANDIDATE**: the structural plan is valid, but the live trigger has not produced meaningful follow-through.
- **EARLY SETUP / RISK**: the live WebSocket price has breached the existing trigger and completed 1-minute structure shows continuation or a failed retest/rejection. The normal 5-minute confirmation is still pending.
- **READY / CONFIRMED**: the existing completed 5-minute confirmation, direction re-check, contract re-selection and risk/reward validation have passed.

EARLY SETUP is higher risk and manual. It is never equivalent to CONFIRMED and never places an order automatically.

## Data path

No new market-data source or candle engine is introduced.

1. Existing Zerodha WebSocket ticks flow through `MarketState`.
2. `LiveSignals` subscribes to the same bounded tick bus used by other runtime consumers.
3. The first fresh directional crossing of the existing trigger is persisted in the existing signal record as `trigger_watch`.
4. Existing aggregated 1-minute candles are inspected during the normal signal-observer cycle.
5. Existing completed 5-minute candles remain the only path to CONFIRMED.

Pre-breach ticks are not persisted, so the watcher does not write SQLite on every market tick.

## Exact EARLY SETUP rule

A tick breach alone is never sufficient.

After the first fresh live breach, the system requires two valid completed 1-minute candles after the breach:

1. both completed 1-minute closes remain beyond the trigger in the planned direction;
2. the latest 1-minute candle has a directional body;
3. the latest close extends beyond the preceding close in the planned direction; and
4. the latest candle shows either:
   - a trigger retest/rejection that closes back on the correct side, or
   - directional continuation structure:
     - CALL: non-lower low and higher high;
     - PUT: non-higher high and lower low.
5. the existing signal engine must still return the original CALL/PUT direction.

No percentage move threshold and no new confidence threshold are used.

If a completed 1-minute candle closes back through the trigger before normal confirmation, the trigger watch is cleared. An EARLY_SETUP returns to CANDIDATE/WAIT FOR TRIGGER and must receive a new live breach before it can become early again.

A touch of the structural invalidation level still invalidates the pre-confirmed signal.

## Persistence

The existing `SignalRecord` stores a small `trigger_watch` object containing:

- signal ID
- direction
- trigger level
- breach timestamp
- breach price
- underlying symbol
- selected option symbol
- lifecycle state at breach
- latest evaluated 1-minute structure

No new database or table is introduced. Existing journal serialization persists this field across restart.

## Manual trade behavior

Showing EARLY_SETUP does not activate stop monitoring.

The user may explicitly click **I TOOK THIS TRADE** from either EARLY_SETUP or CONFIRMED. Only that explicit action creates the existing manual trade journal row and starts manual stop/target monitoring.

Manual trade metadata records whether the user entered from EARLY_SETUP or CONFIRMED.

## Opportunity history

`GET /api/signals/opportunities` remains confirmed-only because it is still backed by the persisted `CONFIRMED` lifecycle event.

Therefore:

- CANDIDATE -> not included
- EARLY_SETUP -> not included
- CONFIRMED -> included
- a manual trade recorded early does not itself make the model opportunity confirmed

## Scoring

This feature does not change the scoring engine.

The following remain unchanged:

- minimum score 60
- minimum aligned categories
- minimum score separation
- Price / Trend, Options, Breadth, VIX and Futures weights
- sparse Options behavior
- PCR confirmation semantics
- max-pain semantics
- expiry selection
- contradiction/freshness gates
