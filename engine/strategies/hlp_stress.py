"""hlp-stress-v1 — HLP (Hyperliquidity Provider) drain signal.

THESIS
======
HLP is HL's market-maker vault. It absorbs adverse selection from takers
and ADL counterparty losses. When HLP equity drains faster than a normal
session, the venue is under stress: either (a) directional flow is
concentrated and predictable, or (b) liquidations are cascading and ADL
risk is rising. Both → EXIT longs (or open shorts) on the highest-OI coins.

When HLP equity *rises* faster than normal (positive flow capture), MM
profit is being made by fading takers — directional trades are getting
faded → bias TOWARD mean reversion on stretched moves.

DATA: novel_data.get_hlp_stress() — refreshed every 5min
  hlp_equity_now, hlp_equity_1h_ago, hlp_equity_24h_ago,
  drain_1h_pct, drain_24h_pct, drain_zscore_24h

DIRECTION on signal coin (BTC default):
  drain_1h_pct < drain_threshold (e.g. -0.5% in 1h) → SHORT (ADL risk)
  rise_1h_pct  > rise_threshold (e.g. +0.5%)       → fade extension
                                                       (LONG if price down,
                                                        SHORT if price up)

EDGE NOT CROWDED: retail does not track HLP vault equity. The vaultDetails
endpoint is public but only HL-natives watch it.
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
    stress = df.attrs.get("hlp_stress")
    if not stress:
        return None

    age = stress.get("age_sec", 99999)
    if age > 900:  # 15min staleness limit
        return None

    drain_1h = stress.get("drain_1h_pct")
    if drain_1h is None:
        return None

    drain_thresh = float(STRATEGY_PARAMS.get("hlp_drain_threshold_pct", -0.005))
    rise_thresh = float(STRATEGY_PARAMS.get("hlp_rise_threshold_pct", 0.005))

    closes = df["close"].values
    ref_px = float(closes[-1])
    # Recent price velocity (1h)
    bars_per_hr = 12 if "5m" in str(df.attrs.get("timeframe", "5m")) else 4
    price_1h_chg = (ref_px - closes[-bars_per_hr]) / closes[-bars_per_hr] if len(closes) > bars_per_hr else 0

    is_long = None
    fire_tag = None
    conviction = "weak"

    if drain_1h <= drain_thresh:
        # HLP draining → venue under stress → fade strength, ride weakness
        # Take SHORT on price extension UP (forced longs about to ADL)
        if price_1h_chg > 0.005:
            is_long = False
            fire_tag = f"hlp_drain_{drain_1h*100:+.2f}_extup{price_1h_chg*100:+.2f}"
            conviction = "strong" if drain_1h <= drain_thresh * 2 else "weak"
        else:
            return None

    elif drain_1h >= rise_thresh:
        # HLP gaining = MM fading takers profitably → mean-revert stretched moves
        if price_1h_chg > 0.012:
            is_long = False
            fire_tag = f"hlp_gain_{drain_1h*100:+.2f}_fade_up{price_1h_chg*100:+.2f}"
        elif price_1h_chg < -0.012:
            is_long = True
            fire_tag = f"hlp_gain_{drain_1h*100:+.2f}_fade_dn{price_1h_chg*100:+.2f}"
        else:
            return None
        conviction = "weak"   # fade trades are inherently smaller
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

    sl_mult = STRATEGY_PARAMS.get("hlp_sl_atr_mult", 1.5)
    tp_mult = STRATEGY_PARAMS.get("hlp_tp_atr_mult", 3.5)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("hlp_max_hold_bars", 12)),
        "fire_reason": fire_tag,
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if conviction == "strong" else 0.4,
        "drain_1h_pct": drain_1h,
        "price_1h_chg": price_1h_chg,
    }
