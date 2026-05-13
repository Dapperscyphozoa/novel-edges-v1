"""token-unlock-structurer-v1 — vesting cliff sell-pressure fade.

THESIS
======
Token unlocks introduce structural sell pressure 24-72h before the event
(insiders front-run their own distribution). Public unlock calendars are
mostly behind paywalls (TokenUnlocks, CryptoRank), but circulating-supply
*velocity* (rate of change) is observable for free from CoinGecko's
public endpoints, and reliably spikes 24-48h around any major unlock.

When circulating supply increases >0.5% in 24h (relative to a 7d baseline),
the asset is in an active unlock window → SHORT bias until the spike
normalizes. Optional rebound after the dust settles (when delta returns
to baseline + price has dropped >3%) → LONG.

DATA: novel_data.get_supply_velocity(coin) — refreshed every 6h
DIRECTION:
  delta_24h_pct > supply_min_velocity AND not yet faded → SHORT
  delta_24h_pct returns to baseline AND price still depressed → LONG (rebound)

EDGE NOT CROWDED BY RETAIL: retail watches "unlock date" calendars;
they don't run velocity-of-circulating-supply scans, and they don't
distinguish pre-unlock-bleed from post-unlock-rebound.
"""
from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from ..config import STRATEGY_PARAMS, TRADE_PARAMS


def evaluate_latest_bar(df: pd.DataFrame) -> Optional[dict]:
    if df is None or len(df) < 30:
        return None

    coin = df.attrs.get("coin", "")
    supply_data = df.attrs.get("supply_velocity")
    if not supply_data:
        return None

    velocity_24h = supply_data.get("delta_24h_pct")     # % change vs 24h ago
    baseline_7d = supply_data.get("delta_7d_avg_pct")    # avg daily 7d
    age_sec = supply_data.get("age_sec", 99999)

    if velocity_24h is None or baseline_7d is None:
        return None
    if age_sec > 21600:  # 6h cache max
        return None

    min_velocity = float(STRATEGY_PARAMS.get("tunlk_min_velocity_pct", 0.005))   # 0.5%
    rebound_drop_pct = float(STRATEGY_PARAMS.get("tunlk_rebound_drop_pct", 0.03))

    closes = df["close"].values
    ref_px = float(closes[-1])

    # SHORT signal: velocity spike vs baseline
    spike = velocity_24h - baseline_7d
    price_3d = float(closes[-min(72, len(closes)-1)]) if len(closes) > 72 else float(closes[0])
    drop_pct = (ref_px - price_3d) / price_3d

    is_long = None
    fire_tag = None
    if spike >= min_velocity:
        is_long = False
        fire_tag = f"unlock_spike_{spike*100:+.2f}pct"
    elif abs(spike) < min_velocity / 4 and drop_pct <= -rebound_drop_pct:
        # Spike normalized AND price has bled — rebound trade
        is_long = True
        fire_tag = f"unlock_rebound_drop{drop_pct*100:+.2f}pct"
    else:
        return None

    h, l = df["high"].values, df["low"].values
    pc = pd.Series(closes).shift(1)
    tr = pd.concat([pd.Series(h) - pd.Series(l),
                    (pd.Series(h) - pc).abs(),
                    (pd.Series(l) - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    if not atr or atr <= 0:
        return None

    sl_mult = STRATEGY_PARAMS.get("tunlk_sl_atr_mult", 2.0)
    tp_mult = STRATEGY_PARAMS.get("tunlk_tp_atr_mult", 5.0)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("tunlk_max_hold_bars", 48)),
        "fire_reason": fire_tag,
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": "strong" if abs(spike) >= min_velocity * 2 else "weak",
        "size_multiplier": 1.0 if abs(spike) >= min_velocity * 2 else 0.4,
        "velocity_24h_pct": velocity_24h,
        "baseline_7d_pct": baseline_7d,
        "spike_pct": spike,
    }
