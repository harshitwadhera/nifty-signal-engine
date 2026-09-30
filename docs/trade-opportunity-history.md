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

Rows sort by their first persisted confirmation time, newest first, with descending
signal ID as the deterministic tie breaker. Filtering happens in SQLite before
`limit`/`offset`; `index=nifty` and `index=banknifty` are supported. The separate
`/api/signals/history` audit continues to sort by creation time and includes candidates.
Confirmation time and lifecycle states come from `signal_events`, even when a legacy
serialized history is missing, null, or incomplete. A damaged serialized record can
still show its persisted identity, confirmation, and state with unavailable plan fields.
Reads do not repair or mutate storage. Page events and manual trades are each fetched
in one batch; there is no per-row manual-trade query or new schema migration.

The **Confirmation / model entry** column uses the observed confirmation price or
recorded model-entry underlying price. A missing observation stays unavailable;
the planned trigger is not substituted. Neither this column nor Taken status claims
an option fill, broker execution, or actual user P&L.

The Trading Dashboard shows the latest 25 confirmed opportunities below the live NIFTY/BANKNIFTY cards.

The large qualification / WHY NO TRADE gate table is available from the `ⓘ` control:
mouse hover, click, native Enter/Space activation, or touch tap. Click pins a hovered
panel; another click, Close, Escape, or tabbing outside closes it. Close/Escape return
focus to the trigger. Visibility and `aria-expanded` share one state, and unique
`aria-controls`/`aria-labelledby` connect each trigger and panel. There is no focus trap.

Polling retains the trigger, panel, close control, and scroll viewport. Gate text
updates without resetting open state, focus, or scroll positions. Desktop panels
are bounded by the signal grid; mobile panels have viewport insets, vertical scrolling,
a sticky Close button, and a keyboard-focusable horizontal table scroll area.

The Options alignment clarification is documented in [Options component availability](options-component-availability.md).
Minimum score remains 60, minimum aligned categories 4, separation 15, breadth coverage
90%, and score version 5.7.0. Existing freshness, contradiction, expiry, entry-window,
confirmation, stop/target lifecycle, manual trade, alarm, and broker rules are unchanged.
