"""cross-margin-contagion-v1 — whale forced-liquidation precognition.

THESIS
======
HL whales (top-100 by equity) carry concentrated cross-margin positions.
When a whale's account-value-to-margin-used ratio approaches the
maintenance-margin threshold (~1.1×), they are at risk of forced
liquidation. The liquidation engine will dump the largest-notional
position with the thinnest book first (worst slippage to the whale,
best signal to us). Pre-positioning against that asset captures the
forced-sell impulse.

DATA: novel_data.get_whale_stress_signals() — refreshed every 90s
  Returns list of {coin, side, stress_score, n_whales_at_risk}
  where stress_score ∈ [0,1] (1 = imminent liq)

DIRECTION:
  whale at risk LONG  on coin → SHORT signal coin (their long gets dumped)
  whale at risk SHORT on coin → LONG  signal coin (their short gets covered)

CONVICTION:
  strong: n_whales_at_risk >= 2 AND avg stress_score >= 0.6
  weak:   n_whales_at_risk == 1 AND stress_score >= 0.5

EDGE NOT CROWDED: HL leaderboard scraping is already in our infra
(whale_mirror), but using it as a STRESS sensor (not alpha sensor) is novel.
Retail watches whale entries; almost nobody watches the maintenance-margin
ratio of those same wallets.
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
    stress_map = df.attrs.get("whale_stress")    # dict: coin -> {side, score, n_whales}
    if not stress_map:
        return None
    age = df.attrs.get("whale_stress_age_sec", 99999)
    if age > 600:
        return None

    entry = stress_map.get(coin)
    if not entry:
        return None

    score = float(entry.get("stress_score", 0))
    n_whales = int(entry.get("n_whales_at_risk", 0))
    whale_side = entry.get("dominant_side")      # "LONG" or "SHORT" – the side at risk
    if whale_side not in ("LONG", "SHORT"):
        return None

    min_score = float(STRATEGY_PARAMS.get("contagion_min_score", 0.5))
    strong_score = float(STRATEGY_PARAMS.get("contagion_strong_score", 0.65))

    if score < min_score:
        return None

    # Fade the whale: whales long → we short
    is_long = (whale_side == "SHORT")

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
    sl_mult = STRATEGY_PARAMS.get("contagion_sl_atr_mult", 1.2)
    tp_mult = STRATEGY_PARAMS.get("contagion_tp_atr_mult", 4.0)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    strong = (score >= strong_score) and (n_whales >= 2)
    conviction = "strong" if strong else "weak"

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("contagion_max_hold_bars", 18)),
        "fire_reason": f"contagion_{n_whales}whales_score{score:.2f}_{whale_side.lower()}",
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if strong else 0.4,
        "stress_score": score,
        "n_whales_at_risk": n_whales,
        "whale_side": whale_side,
    }
