"""listings-decay-v1 — new-perp-listing volatility decay fade.

THESIS
======
When HL lists a new perp, the asset trades with abnormally high realized
volatility for the first 24-72 hours. Funding rates spike (often >0.05%/hr,
~44% APR), spreads widen, and price action is overwhelmingly driven by
new-listing FOMO + arb-bot front-running. After ~72h the realized vol
collapses toward the universe median. Fading the directional wicks during
the high-RV window earns the vol-decay premium.

DATA: novel_data.get_recent_listings(max_age_hours=72) — derived from
  HL meta universe ordering (newer coins are appended at the end). When
  a coin appears for the first time, record listing_ts. Used to identify
  "young" coins.

DIRECTION:
  Coin is within listings window AND price has moved >3% in last bar
  AND funding is >0.04%/hr (longs paying heavily) → SHORT (fade wick up)
  AND funding is <-0.04%/hr → LONG (fade wick down)

CONVICTION:
  strong: listing age < 24h, wick > 5%, funding |>| 0.06%/hr
  weak:   listing age 24-72h, wick > 3%

EDGE NOT CROWDED: retail chases new listings; almost nobody systematically
fades the first-72h vol with funding-rate confirmation.
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
    listing_age_hr = df.attrs.get("listing_age_hours")
    funding_rate_hr = df.attrs.get("funding_rate_hr")  # hourly funding as decimal

    if listing_age_hr is None or funding_rate_hr is None:
        return None

    max_age_hr = float(STRATEGY_PARAMS.get("listings_max_age_hours", 72))
    young_thresh_hr = float(STRATEGY_PARAMS.get("listings_young_threshold_hours", 24))
    if listing_age_hr > max_age_hr:
        return None

    min_funding = float(STRATEGY_PARAMS.get("listings_min_funding_hr", 0.0004))    # 0.04%/hr
    strong_funding = float(STRATEGY_PARAMS.get("listings_strong_funding_hr", 0.0006))

    # Recent wick measurement: max price excursion vs current
    closes = df["close"].values
    if len(closes) < 5:
        return None
    last_5_high = float(np.max(df["high"].values[-5:]))
    last_5_low = float(np.min(df["low"].values[-5:]))
    ref_px = float(closes[-1])
    wick_up_pct = (last_5_high - ref_px) / ref_px
    wick_dn_pct = (ref_px - last_5_low) / ref_px

    min_wick = float(STRATEGY_PARAMS.get("listings_min_wick_pct", 0.03))
    strong_wick = float(STRATEGY_PARAMS.get("listings_strong_wick_pct", 0.05))

    is_long = None
    fire_tag = None

    if funding_rate_hr >= min_funding and wick_up_pct >= min_wick:
        # Heavy long funding, big wick up → fade UP → SHORT
        is_long = False
        fire_tag = f"list_short_age{listing_age_hr:.1f}h_fund{funding_rate_hr*100:.3f}_wick{wick_up_pct*100:.1f}"
    elif funding_rate_hr <= -min_funding and wick_dn_pct >= min_wick:
        # Heavy short funding, big wick down → fade DN → LONG
        is_long = True
        fire_tag = f"list_long_age{listing_age_hr:.1f}h_fund{funding_rate_hr*100:.3f}_wick{wick_dn_pct*100:.1f}"
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

    sl_mult = STRATEGY_PARAMS.get("listings_sl_atr_mult", 1.5)
    tp_mult = STRATEGY_PARAMS.get("listings_tp_atr_mult", 3.5)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    is_strong = (
        listing_age_hr < young_thresh_hr
        and abs(funding_rate_hr) >= strong_funding
        and max(wick_up_pct, wick_dn_pct) >= strong_wick
    )
    conviction = "strong" if is_strong else "weak"

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("listings_max_hold_bars", 8)),
        "fire_reason": fire_tag,
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if is_strong else 0.4,
        "listing_age_hours": listing_age_hr,
        "funding_rate_hr": funding_rate_hr,
        "wick_up_pct": wick_up_pct,
        "wick_dn_pct": wick_dn_pct,
    }
