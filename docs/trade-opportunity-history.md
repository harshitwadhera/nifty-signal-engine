# Trade opportunity history

Branch: `feature/trade-opportunity-history`.

This change keeps the live signal cards focused while making confirmed model opportunities durable and visible after the live card returns to **NO TRADE**.

## What is stored

The existing signal lifecycle SQLite tables remain authoritative. No database schema change is required.

A row appears in **Trade Opportunity History** only after a signal has reached `CONFIRMED` (the same lifecycle point that makes the manual setup READY). A candidate that never confirms is not shown as a trade opportunity.

The history API returns the stored signal plan and lifecycle, including:

- confirmation time and observed confirmation price
- NIFTY/BANKNIFTY and CALL/PUT direction
- selected option contract
- structural stop / invalidation
- T1 and T2
- current/final model lifecycle result
- whether the user recorded the trade manually

The model lifecycle continues independently of the browser and independently of whether the user records a manual trade.

## Taken = YES / NO

`Taken = YES` means the signal ID has a persisted row in the existing `user_trades` journal, created only by **I TOOK THIS TRADE**.

`Taken = NO` means no manual trade was recorded for that signal. It does not change the model result.

Therefore a missed opportunity can correctly show, for example:

`NIFTY CALL | T1 HIT -> STOPPED | Taken NO`

This is model-observation history, not a claim of broker execution or personal P&L.

## API and UI

`GET /api/signals/opportunities?limit=25` returns only signals whose persisted lifecycle contains a `CONFIRMED` event and joins the manual journal by `signal_id`.

The Trading Dashboard shows the latest 25 confirmed opportunities below the live NIFTY/BANKNIFTY cards.

The large qualification / WHY NO TRADE gate table is no longer permanently expanded. It remains available from the `ⓘ` control: hover/focus on desktop and tap on touch devices.

Existing signal generation, scoring, confirmation, stop/target lifecycle, manual trade confirmation, alarms, and broker behavior are unchanged.
