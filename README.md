# novel-edges-v1

7 non-crowded edge engines on Hyperliquid. PAPER-ONLY by default; each engine gated by env var (e.g. `TUNLK_ENABLED=0`).

## Engines

| # | Name                    | Edge                                                            | Cadence | TF  |
|---|-------------------------|-----------------------------------------------------------------|---------|-----|
| 1 | token-unlock-v1         | Circulating-supply velocity spike → SHORT bias                  | 30min   | 1h  |
| 2 | hlp-stress-v1           | HLP vault drain → fade extension on BTC/ETH                     | 3min    | 5m  |
| 3 | contagion-v1            | Whale margin-ratio stress → fade their dominant position        | 2min    | 15m |
| 4 | mev-revert-v1           | Uniswap v3 large-swap dislocation → mean revert on perp         | 45s     | 5m  |
| 5 | listings-decay-v1       | New-listing high-funding + wick → fade extension                | 5min    | 15m |
| 6 | lst-discount-v1         | stETH/rETH/cbETH discount → ETH direction signal                | 10min   | 15m |
| 7 | oracle-lag-v1           | Pyth-vs-HL-mark basis → momentum trade until HL catches up      | 30s     | 5m  |

## Data sources (all free)

- HL `info` (vaultDetails, allMids, metaAndAssetCtxs, clearinghouseState)
- HL undocumented leaderboard (`stats-data.hyperliquid.xyz`)
- Coingecko `coins/markets` + `simple/price`
- Pyth Hermes `v2/updates/price/latest`
- Uniswap v3 subgraph (The Graph hosted-service)

## Endpoints

- `GET /health`
- `GET /strategies`
- `GET /state/{engine_name}`
- `GET /signals/{engine_name}?limit=20`
- `GET /trades/{engine_name}?limit=20`
- `GET /closures/{engine_name}?limit=20`
- `GET /pnl` — aggregate across all engines
- `GET /pnl/{engine_name}`

## Promotion criteria (per engine, independently)

- 50+ closed paper trades
- Live PF within 30% of any backtest PF (if backtested)
- Live WR within 10pp of any backtest WR
- No crash loops in 48h
