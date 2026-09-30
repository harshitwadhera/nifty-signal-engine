# Options component availability

Branch: `fix/options-component-availability`, based on freshly fetched `origin/main`
at `79c3140`. Scoring version: `5.7.0`. No merge or deployment is part of this change.

The old analytics layer required every selected-expiry contract to have a fresh
price before exposing walls and full-expiry PCR. Its summary `stale` flag also
made the signal engine discard all Options evidence, including fresh ATM quotes.
Qualification separately enforced 95% coverage and non-stale full-chain refreshes.
Thus even 208/214 fresh contracts could become 0/30 available.

## Component rules

| Component | Maximum | Availability and directional evidence |
| --- | ---: | --- |
| Positioning flow | 10.5 | Requires usable fresh price/OI/session-OI-change observations and valid price/OI classifications on **both** sides. Its available budget is scaled by the weaker side's usable-observation fraction, so one fresh CALL and one fresh PUT cannot claim the same 10.5 points as a broadly observed chain. There is no hard percentage cutoff. Writing requires SHORT_BUILDUP; unwinding requires SHORT_COVERING or LONG_UNWINDING and negative session OI change. |
| ATM CE/PE behavior | 9 | Each side independently earns 4.5 available weight: fresh positive LTP, LIQUID/MODERATE liquidity, positive ordered bid/ask with depth, valid IV within the existing limit, and valid price/OI positioning. This component is intentionally independent of broad-chain population. |
| OI wall breakout | 4.5 | Positive fresh CALL and PUT OI candidates must identify both walls, with positive spot and an ordered call-wall/put-wall range. When walls exist, the available budget is scaled by balanced fresh OI population on the two sides. Sparse data therefore remains observable but cannot claim a full wall budget. |
| PCR confirmation | 6 | OI, volume, and near-ATM OI PCR each have at most 2 available points. Each ratio uses matched fresh CE/PE strikes, and its available budget is proportional to matched-pair population for that ratio. There is no hard coverage threshold. PCR can only confirm an existing non-PCR anchor and cannot establish or reverse direction. |

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

Overall chain coverage and full-chain freshness are INFO. Minimum score is now 60,
while minimum aligned categories remain 4, minimum separation remains 15, breadth
coverage remains 90% with the full-index requirement, and contradiction/index/expiry
checks are unchanged. Category budgets still sum to 100; only the winning-score gate
was lowered. With the new threshold, ATM-only Options evidence can reach 69 directional
points when Price, Breadth and Futures are all fully aligned, so it can qualify if every
other gate also passes.

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

The NIFTY 208/214 and BANKNIFTY 266/314 deterministic fixtures retain 9/9 ATM
availability, while positioning, wall and PCR budgets scale proportionally with
their own usable populations. They therefore remain useful instead of collapsing
to 0/30, but incomplete broad-chain evidence cannot claim the same budget as a
fully observed chain. A sparse 2/214 fixture with only ATM CE/PE fresh keeps 9/9 ATM and receives only a
small fractional budget for broader components. Under the new 60-point winning-score
gate, such a snapshot can still qualify when the other three directional categories
are fully aligned and all remaining gates pass. No hard global percentage cutoff is used.
Both incomplete fixtures have `full_chain_fresh=false` and unavailable max pain.

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
