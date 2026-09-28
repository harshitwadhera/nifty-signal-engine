# Local market simulation fixtures

`market_health_2026-09-28.txt` is the user-supplied Docker log excerpt,
preserved byte-for-byte, including its truncated first line. It is supporting
evidence for options-chain completeness scenarios, not an options API response.

The excerpt includes NIFTY marked stale with 272/276 fresh contracts (98.6%),
NIFTY marked fresh with 276/276 (100%), and BANKNIFTY marked stale with 300/312
(96.2%). A REST refresh reporting all contracts returned does not establish
that every quote passes freshness checks. These aggregate logs do not identify
why individual quotes failed freshness.

The original NIFTY options JSON baseline is still required. It must include the
complete supplied response, including ATM CE/PE records, prices, OI, Greeks,
timestamps and coverage. Do not reconstruct that response from these logs or
the approximate values in the task specification.

Fixtures belong only to local tests. The production Dockerfile copies only
`app/`, and `.dockerignore` excludes `tests/`. No fixture is wired into production
routes, startup, environment defaults or the production database.
