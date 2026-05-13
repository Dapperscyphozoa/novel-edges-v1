"""lst-discount-v1 — liquid-staking-token premium/discount mean reversion.

THESIS
======
stETH / rETH / cbETH / wstETH should trade at parity to ETH (or at a
small premium for accrued staking rewards). When the discount widens
beyond ~0.4% (stETH/ETH < 0.996), it signals one of:
  (a) Forced redemptions from a CeFi/treasury holder (e.g. Celsius, FTX
      era) → ETH spot supply shock about to hit;
  (b) Lido / Rocket / Coinbase staking-queue stress;
  (c) Risk-off rotation away from leveraged staking baskets.

Historically the discount mean-reverts to 0 within 3-14 days. On HL we
cannot trade stETH directly, but we trade the *ETH perp* against the
discount signal:

  Discount widening (stETH cheaper) → LONG ETH perp (basis convergence
    pressure pushes ETH spot down → futures basis flips → mean-revert long)
  Discount tightening rapidly from a wide level → SHORT ETH perp
    (the rotation back into LST has absorbed selling pressure; perp
    overshoots the convergence)

DATA: novel_data.get_lst_discounts() — refreshed every 10min via CoinGecko
  free API. Returns:
  {"stETH": -0.0023, "rETH": +0.0145, "cbETH": +0.0089, "avg": ...}

DIRECTION (only ETH coin):
  avg_discount < discount_threshold (e.g. -0.004) → LONG ETH
  avg_discount in normal range (>-0.002) AND price has bled from oversold
    → done (no signal)
  avg_discount > rich_threshold (e.g. +0.015) → SHORT ETH (overheated rotation)

EDGE NOT CROWDED: LST/ETH basis is an ETH-native trader's tool. Almost
nobody on HL maps it to a perp-direction signal.
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
    if coin != "ETH":
        return None     # only ETH trades on this signal

    lst = df.attrs.get("lst_discounts")
    if not lst:
        return None
    age = lst.get("age_sec", 99999)
    if age > 1800:
        return None

    avg_disc = lst.get("avg_discount")
    if avg_disc is None:
        return None

    discount_threshold = float(STRATEGY_PARAMS.get("lst_discount_threshold", -0.004))
    strong_discount = float(STRATEGY_PARAMS.get("lst_strong_discount", -0.008))
    rich_threshold = float(STRATEGY_PARAMS.get("lst_rich_threshold", 0.015))

    is_long = None
    fire_tag = None
    is_strong = False

    if avg_disc <= discount_threshold:
        is_long = True
        fire_tag = f"lst_disc_{avg_disc*100:+.2f}pct_LONG"
        is_strong = avg_disc <= strong_discount
    elif avg_disc >= rich_threshold:
        is_long = False
        fire_tag = f"lst_rich_{avg_disc*100:+.2f}pct_SHORT"
        is_strong = False
    else:
        return None

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
    sl_mult = STRATEGY_PARAMS.get("lst_sl_atr_mult", 2.0)
    tp_mult = STRATEGY_PARAMS.get("lst_tp_atr_mult", 4.5)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    conviction = "strong" if is_strong else "weak"

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("lst_max_hold_bars", 96)),  # 96×15m = 24h
        "fire_reason": fire_tag,
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if is_strong else 0.4,
        "avg_lst_discount": avg_disc,
        "per_lst": {k: v for k, v in lst.items() if k not in ("age_sec", "avg_discount")},
    }
