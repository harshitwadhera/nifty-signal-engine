# Trading Dashboard and Market Analysis

Branch: `feature/split-trade-analysis-pages`.
Base: latest fetched `main`, `8e490ce`, the merged analysis-expiry and signal-explanation
feature from PR #4, including its holiday-shift rollover fix.

## Routes and content

`/` serves the Trading Dashboard from `app/static/index.html`. It retains Zerodha
connection/feed status, the NIFTY 50 / BANK NIFTY / INDIA VIX overview and freshness,
both candlestick charts with all existing controls, complete signal qualification
and category tables, plan/option/liquidity details, collapsed evidence, and manual
trade controls with active tracking and STOP alarms. Chart help remains available
in a disclosure to give the charts more space.

`/analysis` serves `app/static/analysis.html` through the same FileResponse/static
architecture. It owns both Market Structure panels and both Options Intelligence
panels. Existing day/opening/previous-session levels, EMAs, futures and VWAP are
preserved. Options retain expiry inspection, PCR, walls, max pain, ATM details,
OI additions, freshness and coverage; existing backend writing/unwinding data and
automatic expiry metadata are now also visible.

The structure UI has confirmed swing high/low fields but does not calculate them.
The current structure endpoint does not supply these fields, so they display
`Unavailable`, with an explanatory note. This split does not add analytics or
derive swing values in JavaScript.

Both pages have a title, subtitle, same-origin navigation and `aria-current`.
The few navigation lines are duplicated in the two HTML files to avoid a new
runtime component or templating/build dependency. `/kite/login`, the callback,
`/api/*` and `/static/*` retain their existing behavior.

## Script and polling ownership

| Page | Deferred scripts, in order | Recurring browser requests |
| --- | --- | --- |
| Trading Dashboard | `market_hours.js`, `dashboard.js`, `charts.js`, `signals.js`, `trades.js` | Connection/live overview and trade checks every 2 s; charts and signals every 5 s |
| Market Analysis | `market_hours.js`, `structure.js`, `options.js` | Structure every 5 s; one options poll per index every 5 s |

Each component still loads once immediately. The shared Monday–Friday 09:00
inclusive / 15:40 exclusive IST guard is unchanged. Dashboard Refresh, chart
refresh/timeframe changes, and expiry selection continue to work outside that
window. The dashboard retains its existing REST fallback behavior.

The Trading Dashboard does not request the detailed structure/options endpoints.
Market Analysis does not request connection/live overview, signals, candles or
trades, and does not create alarm timers. Backend workers remain independent of
which page is open; their cadence and trading behavior are unchanged.

## Active trade and alarm behavior

The dashboard's Market Analysis link explicitly opens a new tab (`target="_blank"`,
`rel="noopener"`). This leaves the original dashboard and its audio state running.
Both pages explain that browser STOP alarms require an open Trading Dashboard tab
with sound armed. The Analysis page has no duplicate trade or alarm polling.

`trades.js` is unchanged. Existing active/unverified trade checks continue outside
market hours, STOP sound remains latched until acknowledgement/closure while its
page is open, and acknowledgement does not close a trade. Closing or navigating
away from the Trading Dashboard still invokes its existing pagehide cleanup.
Browser audio permission and background-tab limitations still apply.

## Mobile and verification

Charts stack at the existing 720 px breakpoint. Structure/options/signals and the
overview stack at 650 px. Grid tracks and cards can shrink, long labels/values wrap,
navigation wraps, buttons/selectors have a 44 px minimum height, and signal tables
use their own horizontal scrolling wrapper. The page does not hide overflow to
mask layout errors. Trade dialogs use border-box sizing and a viewport width limit.

New route tests verify HTML, script lists, navigation, static assets, security
headers and absence of broker calls from static page requests. Node tests load
each page's actual script list against only the IDs in that HTML, so missing DOM
dependencies cannot be hidden by fabricated test elements. They verify exact
endpoint/timer ownership, market-hours boundaries, manual controls and an active
STOP alarm/acknowledgement on the split dashboard. Existing chart and trade tests
also run unchanged.

| Command | Passed | Failed | Skipped | Duration |
| --- | ---: | ---: | ---: | ---: |
| `node --test tests/*.test.cjs` | 106 | 0 | 0 | 562.2122 ms |
| `.venv/Scripts/python.exe -m pytest -q` | 381 | 0 | 0 | 37.31 s |

Pytest emitted one existing Starlette/httpx deprecation warning.

Real-browser desktop/mobile review could not run: the computer-use inventory
contained no available browser surfaces. DOM and responsive CSS contract checks
passed, but actual rendering and iPhone interaction remain visual review items.
No live Zerodha session was used for validation.

## Files changed

- `app/main.py`
- `app/static/index.html`
- `app/static/analysis.html`
- `app/static/options.js`
- `app/static/structure.js`
- `app/static/style.css`
- `docs/split-trade-analysis-pages.md`
- `tests/options.test.cjs`
- `tests/pages.test.cjs`
- `tests/polling.test.cjs`
- `tests/structure.test.cjs`
- `tests/test_pages.py`
