"""mev-revert-v1 — sandwich/MEV mean-reversion on perps.

THESIS
======
Large Uniswap v3 swaps (>$200k notional with >2% pool-price-impact)
create transient on-chain price dislocations that mean-revert within
1-3 blocks (~12-36 seconds). When the same asset is listed as an HL
perp, the perp's mark momentarily tracks the dislocated DEX price (via
oracle blending), then snaps back. Take the snap-back.

DATA: novel_data.get_mev_dislocations() — refreshed every 15s via
  Uniswap v3 subgraph (The Graph free tier). Returns:
  {coin: {direction_dex_pushed, pct_impact, ts}}

DIRECTION:
  DEX push UP  (large buy) → SHORT (HL perp will catch up briefly, then revert)
  DEX push DOWN (large sell) → LONG

EDGE NOT CROWDED: pure MEV bots focus on atomic same-block arb on DEXs;
they do NOT carry the cross-venue inventory required to fade on a CEX/perp.
That asymmetry is why the dislocation persists for seconds rather than
milliseconds.

LIMITATIONS:
  - Only works for ERC-20s with both Uniswap v3 deep pool AND HL listing.
  - Requires very fresh data (age_sec < 60s).
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from ..config import STRATEGY_PARAMS, TRADE_PARAMS


def evaluate_latest_bar(df: pd.DataFrame) -> Optional[dict]:
    if df is None or len(df) < 20:
        return None

    coin = df.attrs.get("coin", "")
    disloc = df.attrs.get("mev_dislocation")
    if not disloc:
        return None
    age = disloc.get("age_sec", 99999)
    if age > 90:  # only fresh dislocations
        return None

    impact_pct = disloc.get("pct_impact", 0)
    direction = disloc.get("direction_dex_pushed")    # "UP" or "DOWN"

    min_impact = float(STRATEGY_PARAMS.get("mev_min_impact_pct", 0.012))
    strong_impact = float(STRATEGY_PARAMS.get("mev_strong_impact_pct", 0.025))

    if abs(impact_pct) < min_impact:
        return None
    if direction not in ("UP", "DOWN"):
        return None

    is_long = (direction == "DOWN")  # fade the push

    closes = df["close"].values
    h, l = df["high"].values, df["low"].values
    pc = pd.Series(closes).shift(1)
    tr = pd.concat([pd.Series(h) - pd.Series(l),
                    (pd.Series(h) - pc).abs(),
                    (pd.Series(l) - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    if not atr or atr <= 0:
        return None

    ref_px = float(closes[-1])
    # tight SL, fast TP — this is a snap-back trade, not a swing
    sl_mult = STRATEGY_PARAMS.get("mev_sl_atr_mult", 0.8)
    tp_mult = STRATEGY_PARAMS.get("mev_tp_atr_mult", 2.0)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    strong = abs(impact_pct) >= strong_impact
    conviction = "strong" if strong else "weak"

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("mev_max_hold_bars", 4)),  # ~20min on 5m
        "fire_reason": f"mev_{direction.lower()}_{impact_pct*100:+.2f}pct_age{int(age)}s",
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if strong else 0.3,
        "impact_pct": impact_pct,
        "dex_direction": direction,
        "dislocation_age_sec": age,
    }
