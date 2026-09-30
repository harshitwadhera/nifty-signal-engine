# Options component availability

Branch: `fix/options-component-availability`, based on freshly fetched `origin/main`
at `79c3140`. Scoring version: `5.6.0`. No merge or deployment is part of this change.

The old analytics layer required every selected-expiry contract to have a fresh
price before exposing walls and full-expiry PCR. Its summary `stale` flag also
made the signal engine discard all Options evidence, including fresh ATM quotes.
Qualification separately enforced 95% coverage and non-stale full-chain refreshes.
Thus even 208/214 fresh contracts could become 0/30 available.

## Component rules

| Component | Maximum | Availability and directional evidence |
| --- | ---: | --- |
| Positioning flow | 10.5 | Requires usable fresh price/OI/session-OI-change observations and valid price/OI classifications on **both** sides. One-sided flow receives zero budget: the existing pooled zone-vote design has no independent side weights. Writing requires SHORT_BUILDUP; unwinding requires SHORT_COVERING or LONG_UNWINDING and negative session OI change. OI increases alone never identify writing. Usable observations without directional zones are neutral. |
| ATM CE/PE behavior | 9 | Each side independently earns 4.5 available weight: fresh positive LTP, LIQUID/MODERATE liquidity, positive ordered bid/ask with depth, valid IV within the existing limit, and valid price/OI positioning. Missing or stale sides earn neither points nor weight. |
| OI wall breakout | 4.5 | Positive fresh CALL and PUT OI candidates must identify both walls, with positive spot and an ordered call-wall/put-wall range. Missing, one-sided, or crossed walls remove this component only. Inside the range remains neutral. Walls are the strongest **observed usable** OI levels; they do not claim full-chain completeness. |
| PCR confirmation | 6 | OI, volume, and near-ATM OI PCR each receive 2 available points when calculable. Each ratio independently uses matched CE/PE strikes with fresh, finite, nonnegative values for its own field and a positive call denominator. Missing one ratio does not remove others. Near-ATM volume PCR remains informational. PCR can only confirm an existing non-PCR anchor; conflict remains a contradiction and cannot reverse the anchor. |

OI and volume calculations do not require option-price or IV validity; positioning
and ATM behavior do. Aggregate ratios/walls/flow still use the existing REST batch;
ATM may use the existing same-expiry fresh streaming overlay. No new requests,
subscriptions, timers, or broker polling are introduced.

Fixed category budgets remain Price/Trend 30, Options 30, Breadth 15, VIX 10,
Futures 15. No missing evidence is renormalized. The sum of the four available
component weights is the Options available weight. The existing Tally direction
margin applies to that sum: zero is unavailable, small net evidence is neutral,
and otherwise its sign determines bullish/bearish. VIX stays non-directional.
Max pain still requires complete fresh priced-chain OI and contributes no points.

## Quality and compatibility

- `coverage.expected_contracts`, `fresh_contracts`, `percent`, and
  `non_fresh_contracts` describe the selected REST chain. The existing fresh-priced
  contract coverage definition is retained (with positive finite LTP validation).
  `received_contracts` is retained as a deprecated alias of `fresh_contracts`, not
  a count of all broker-returned quotes. REST logs separately count returned and
  `unreturned_contracts`.
- `full_chain_fresh` retains the complete fresh-priced-chain diagnostic. Legacy
  summary `stale` remains its inverse for existing consumers; neither field
  controls scoring or qualification. Per-contract `stale` remains authoritative.
- `near_atm_quality` describes the same REST chain's configured ATM +/- window.
  `atm_quality` describes the actual ATM scoring rows **after** fresh live overlays.
  The scopes may differ; a fresh streaming ATM quote can coexist with 0% REST
  coverage. OI, OI-change and volume contract counts remain separate diagnostics.
- `positioning_quality` and `oi_quality` count eligible observations on each side.
  `pcr_quality` reports each ratio's paired population and availability.
  `pcr_scope` is now `matched_fresh_strikes_selected_expiry`; UI labels say
  "Usable-strike PCR" instead of implying complete-expiry calculations.
- `component_availability` is computed by the actual scorer, including reasons
  and ATM side checks, after the streaming overlay. Signal decisions expose the
  same information under `data_quality.options_quality`, using their scoring
  configuration. Options API/live defaults use the unchanged default config.
- The derived `evidence_fresh` marker means analytics filtered the aggregate
  observations by row freshness at `timestamp`. It does **not** assert complete
  coverage, or that a component exists. Unversioned historical aggregate inputs
  lacking this provenance are unavailable when rescored; retained historical
  signal creation scores/explanations are unchanged. Replays should regenerate
  summaries from their captured rows. ATM remains independently validated.
- `minimum_option_coverage=95` remains accepted, validated and serialized as a
  deprecated diagnostic reference. It no longer changes decisions. No replacement
  percentage or minimum available Options weight was introduced.

## Qualification and UI

`options_fresh` is replaced by `options_signal_data`: at least one usable scoring
component and a current summary observation within the existing 60-second critical
age limit. It does not require a recent complete REST batch. If every component
fails, the gate blocks. ATM gates are PASS when usable and INFO otherwise; lost
ATM sides already lose their scoring weight. Final contract selection still
requires its own fresh liquid option after qualification.

Overall chain coverage and full-chain freshness are INFO. Minimum score 70,
minimum aligned categories 4, minimum separation 15, breadth coverage 90% with the
full-index requirement, contradictions, and index/expiry consistency are unchanged.
ATM-only Options evidence yields at most 30+9+15+15 = 69 directional points with
all other directional categories full, so it cannot qualify.

Illustrative API excerpt (independent broader-component data may differ):

```json
{
  "full_chain_fresh": false,
  "coverage": {"expected_contracts": 214, "fresh_contracts": 208, "percent": 97.2},
  "near_atm_quality": {"expected_contracts": 42, "fresh_contracts": 42, "percent": 100},
  "atm_quality": {
    "ce": {"fresh": true, "liquidity": "LIQUID", "usable": true},
    "pe": {"fresh": true, "liquidity": "LIQUID", "usable": true}
  },
  "component_availability": {
    "positioning_flow": {"available_weight": 10.5, "maximum_weight": 10.5, "reason": "Fresh price/OI positioning on both sides"},
    "atm_behavior": {"available_weight": 9, "maximum_weight": 9, "reason": "2 of 2 ATM sides usable"},
    "oi_wall_breakout": {"available_weight": 0, "maximum_weight": 4.5, "reason": "Insufficient valid OI-wall data or range"},
    "pcr_confirmation": {"available_weight": 4, "maximum_weight": 6, "usable_ratios": 2, "reason": "2 of 3 ratios usable; confirmation only"}
  },
  "options_total_available_weight": 23.5
}
```

The dashboard can display `Options | bullish | 16.5 | 0 | 23.5 / 30`.
Both pages keep detailed quality in a collapsed **OPTIONS DATA QUALITY** section:

```text
Overall chain: 208 / 214 (97.2%) · diagnostic only
Full chain fresh: No (informational)
ATM CE: Fresh · LIQUID
ATM PE: Fresh · LIQUID
ATM behavior: 9 / 9 available
Positioning flow: 10.5 / 10.5 available
OI walls: 0 / 4.5 unavailable · Insufficient valid OI-wall data or range
PCR: 4 / 6 available · 2 of 3 ratios usable; confirmation only
```

The NIFTY 208/214 and BANKNIFTY 266/314 deterministic fixtures both retain 9/9
ATM and 30/30 total availability when broader evidence is usable; the inside-wall
vote is neutral, resulting in 25.5 bullish Options points. Their final 85.5-point
CALL qualifications are fixture outcomes, not claims about production direction.
Both have `full_chain_fresh=false` and unavailable max pain. Staling only distant
rows after a 100% snapshot keeps these component weights stable. At 212/214
(99.1%), staling both ATM rows independently removes all 9 ATM points.

## Diagnostics and review

Minute health logs retain automatic index+expiry, expected/fresh/non-fresh counts,
coverage, near-ATM counts, ATM side freshness/liquidity, all four component weights,
total available weight, and full-chain freshness. The logger's explicit whitelist
still excludes messages, exceptions, credentials, tokens, and URLs.

The review covers row filtering, zero denominators, neutral flow, one-sided data,
PCR anchoring, fixed budgets and component sums, score/coverage gates, the live
freshness adapter, API labels, active creation/current explanation separation,
expiry consistency, contract selection's unchanged metadata-completeness check,
and broker call counts. Complete-chain checks remain only for informational max
pain and diagnostics. Contract selection requires complete **metadata**, not fresh
quotes at every strike, to find an actual adjacent contract.

Expiry policy, manual inspection, all other category scoring, trade confirmation,
entry cutoff, stop/target logic, lifecycle and STOP alarms, page split, charts,
authentication, and database schema are unchanged.

Remaining limits: matched-strike ratios and observed OI walls describe available
data and can change when previously unseen contracts become fresh. Pairing avoids
one-sided ratio inflation but cannot reconstruct missing market observations.
One-sided flow is intentionally unavailable. Validation uses deterministic broker
mocks and DOM rendering tests; no live broker session or production replay was run.
