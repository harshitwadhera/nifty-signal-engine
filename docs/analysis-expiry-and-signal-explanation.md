# Analysis expiry and signal explanations

Branch: `feature/analysis-expiry-and-signal-explanation`.
Base: fetched `origin/main`, `dea546c` (`feature: add live candlestick charts`).

## Policy

`OptionDiscovery.selections()` is the single automatic expiry policy. All dates
come from discovered NFO contracts. The clock must be timezone-aware and is
converted to `Asia/Kolkata`. Expired contracts are excluded, including today's
contracts at or after 15:30 IST.

- NIFTY uses the second active listed expiry on Monday and Tuesday, and the first
  active listed expiry on other days. A missing second expiry returns unavailable.
- BANKNIFTY uses option expiry dates that also occur in the same index's listed
  futures metadata. This excludes weekly options without guessing month-end dates.
  It uses the nearest such monthly expiry until the final zero or one weekday
  before its actual listed expiry, then uses the next monthly expiry. Missing
  monthly metadata or a required next monthly expiry returns unavailable.
- Holiday-adjusted listed dates control selection: an expiry moved to Monday
  rolls on the preceding Friday; a listed Wednesday rolls on Tuesday. The project
  has no exchange holiday calendar, so intermediate weekday holidays and special
  weekend trading sessions are not independently modeled.

Illustrative listed-metadata examples, during market hours:

| Index | Observation | Nearest relevant listed expiry | Analysis expiry |
| --- | --- | --- | --- |
| NIFTY | Monday 28 Sep 2026 | 29 Sep 2026 | 06 Oct 2026 |
| NIFTY | Tuesday 29 Sep 2026 | 29 Sep 2026 | 06 Oct 2026 |
| NIFTY | Wednesday 30 Sep through Friday 02 Oct | 06 Oct 2026 | 06 Oct 2026 |
| BANKNIFTY | Wednesday 16 Sep 2026 | Monthly 29 Sep 2026 | 29 Sep 2026 |
| BANKNIFTY | Monday 28 Sep / Tuesday 29 Sep | Monthly 29 Sep 2026 | 27 Oct 2026 |
| BANKNIFTY | Wednesday 30 Sep 2026 | Monthly 27 Oct 2026 | 27 Oct 2026 |
| BANKNIFTY | Friday 25 Sep, if metadata lists 28 Sep monthly | Monthly 28 Sep 2026 | Next listed monthly |

These dates are test examples, not a hardcoded exchange schedule. For NIFTY the
literal Monday/Tuesday rule still skips the first active listing after an expiry
has been removed at the close.

## Consistency and manual inspection

The automatic REST target, WebSocket strike window, default options response,
full-chain metrics, ATM behavior, and persisted analysis chain all use
`analysis_expiry`. Metrics retain their existing complete-REST-chain requirements.
Fresh streaming overlays remain confined to the same expiry.

`LiveSignals.evaluate()` reads one options response and carries that exact summary
and contract list through scoring and planning. Contract selection requires
`selected_expiry == expiry_selection.analysis_expiry`, then filters contracts by
that expiry and index. Manual-expiry mismatches cannot qualify or select an option.

Explicit UI inspection uses a separate request slot from internal contract
observations made by the existing trade journal. Returning the dropdown to automatic
releases only the UI slot. Expired UI selections recover from a 404 by requesting
the automatic view. Automatic observers never alter the UI selection. Refresh
targets are deduplicated across automatic, UI, and internal requests.

No broker calls are made by response rendering, expiry selection, signal scoring,
or qualification explanation. Refresh timing, quote batching, and metadata caching
are unchanged. A genuinely distinct manual expiry still needs its own normal
full-chain refresh, as before. The front-month futures stream used for underlying
structure remains unchanged; option Greeks use an expiry-matched future when listed.

## API and UI

Options summaries, chain responses, and current signals expose `expiry_selection`.
A partial illustrative signal response:

```json
{
  "expiry_selection": {
    "nearest": "2026-09-29",
    "next": "2026-10-06",
    "monthly": "2026-09-29",
    "next_monthly": "2026-10-27",
    "analysis_expiry": "2026-10-06",
    "analysis_expiry_policy": {
      "code": "NIFTY_NEXT_WEEK_EXPIRY",
      "label": "Next-week expiry",
      "reason": "Current-week expiry skipped for Monday/Tuesday analysis",
      "available": true,
      "timezone": "Asia/Kolkata"
    }
  },
  "category_scores": {
    "options_positioning": {
      "direction": "unavailable",
      "bullish_points": 0,
      "bearish_points": 0,
      "available_weight": 0,
      "maximum_weight": 30,
      "evidence": [],
      "contradictions": []
    }
  },
  "qualification_gates": [
    {
      "key": "option_coverage",
      "label": "Options coverage (%)",
      "passed": true,
      "status": "PASS",
      "actual": 97.22222222222223,
      "required": 95,
      "detail": null
    },
    {
      "key": "options_fresh",
      "label": "Options freshness",
      "passed": false,
      "status": "BLOCK",
      "actual": "stale or unavailable",
      "required": "Fresh within 60s",
      "detail": "Full-chain freshness unavailable"
    }
  ]
}
```

The backend emits gates from its decision predicates, including score, alignment,
separation, all four critical freshness checks, critical values, both coverage
checks, index identity, major contradictions, and analysis expiry consistency.
The planner uses the same entry-window gate it publishes. Planning/lifecycle
reasons are exposed separately as PASS, BLOCK, or INFO. The frontend displays
these fields and does not derive scoring decisions or expiry policy.

Illustrative qualification rows:

| Status | Gate | Observation / Requirement |
| --- | --- | --- |
| BLOCK | Winning score | 31.5 / 70 required |
| BLOCK | Aligned categories | 3 / 4 required |
| PASS | Score separation | 31.5 / 15 required |
| PASS | Structure freshness | Fresh within 60s |
| BLOCK | Options freshness | Full-chain freshness unavailable |
| PASS | Breadth freshness | Fresh within 60s |
| PASS | VIX freshness | Fresh within 60s |
| PASS | Critical values | Spot, futures and VIX present |
| PASS | Options coverage (%) | 97.22 / 95 required |
| PASS | Breadth coverage (%) | 100 / 90 required; full index |
| PASS | Major contradictions | None |
| BLOCK | New-entry window | Outside new-entry window; cutoff 15:00 IST |
| BLOCK | Signal planning | Outside new-entry window |

The visible category table distinguishes currently available points from configured
maximum weights: for example Options `0 / 30` and Breadth `12 / 15`. The maximum
weights come from `SignalConfig`. Existing Evidence, Contradictions, Data quality,
and category details remain available in collapsed sections.

An active signal retains its immutable creation scores and original expiry in
`score_expiry_selection`. The top tables use `current_qualification` and current
policy metadata, with an explicit note separating them from creation evidence.
Passing a gate is not presented as a profitability prediction.

## Validation and focused review

Final required suite runs on the implementation:

| Command | Passed | Failed | Skipped | Duration |
| --- | ---: | ---: | ---: | ---: |
| `node --test tests/*.test.cjs` | 92 | 0 | 0 | 480.1202 ms |
| `.venv/Scripts/python.exe -m pytest -q` | 367 | 0 | 0 | 36.57 s |

Python used the repository's Python 3.12.14 virtual environment. Pytest emitted one
existing Starlette/httpx deprecation warning. An existing chart test had an invalid
fixture (`close=999` above its high); its high was corrected to 999 so the test
exercises incremental updates. Production chart code and behavior are unchanged.

The focused review checked all nearest-expiry references, IST conversion, policy
duplication, manual/stale interactions, broker call counts, same-expiry ATM overlays,
trade contract selection, persistence, and active creation/current evidence. The
remaining nearest-expiry selection in `app/streaming/instruments.py` is the existing
front-month futures resolver, not automatic option selection.

No changes to weights, thresholds, trade lifecycle, stops/targets, authentication,
database schema, order behavior, third-party JavaScript, or chart behavior. No AI
or broker order placement was added.

Remaining validation limits: broker behavior is tested with deterministic mocks,
not a live Zerodha session. Browser surfaces were unavailable, so DOM rendering
tests passed but a visual layout check was not completed. Listed expiry adjustments
are supported; a separate exchange trading calendar is still absent. A newly
selected chain may remain unavailable until its next scheduled REST refresh.

## Files changed

- `app/main.py`
- `app/options/discovery.py`
- `app/options/service.py`
- `app/options/storage.py`
- `app/signals/decision.py`
- `app/signals/engine.py`
- `app/signals/live.py`
- `app/signals/models.py`
- `app/signals/planning.py`
- `app/signals/selection.py`
- `app/static/index.html`
- `app/static/options.js`
- `app/static/signals.js`
- `app/static/style.css`
- `docs/analysis-expiry-and-signal-explanation.md`
- `tests/charts.test.cjs`
- `tests/options.test.cjs`
- `tests/signals.test.cjs`
- `tests/test_analysis_expiry.py`
- `tests/test_execution.py`
- `tests/test_options.py`
- `tests/test_signal_explanation.py`
