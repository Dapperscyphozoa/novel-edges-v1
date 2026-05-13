"""oracle-lag-v1 — Pyth-vs-HL-mark divergence trade.

THESIS
======
HL's mark price is an exponential moving average (EMA) of mid prices,
so it lags fast moves. Pyth Network publishes spot prices with
sub-second latency, sourced from a broad CEX panel. When Pyth diverges
from HL mark by >0.15%, the HL mark will mean-revert to the Pyth price
within 30-120 seconds. We don't trade the convergence ON HL (HL's funding
absorbs it); we trade the *direction* implied by Pyth being ahead.

Specifically: if Pyth shows +0.2% above HL mark, the broader market is
already there → momentum LONG on HL until HL mark catches up.
Symmetric short.

The novelty over a pure CEX-vs-HL basis is that Pyth aggregates ~10 venues
(Binance, OKX, Coinbase, Kraken, Bybit, KuCoin, MEXC, ...) into one feed
with cryptographic update guarantees. The signal is therefore higher
SNR than any single-venue basis.

DATA: novel_data.get_pyth_hl_basis(coin) — refreshed every 5s
  Returns: {"pyth_price", "hl_mark", "basis_bps", "age_ms"}

DIRECTION:
  basis_bps > +threshold → LONG  (Pyth ahead, HL will follow up)
  basis_bps < -threshold → SHORT (Pyth ahead, HL will follow down)

CONVICTION:
  strong: |basis_bps| >= strong_threshold AND age_ms < 2000
  weak:   threshold <= |basis_bps| < strong_threshold

EDGE NOT CROWDED: oracle-vs-mark basis is a derivative-DEX-MM tool.
Public-side retail does not run Pyth feeds.
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
    basis = df.attrs.get("pyth_hl_basis")
    if not basis:
        return None

    basis_bps = basis.get("basis_bps")
    age_ms = basis.get("age_ms", 99999)
    if basis_bps is None:
        return None
    if age_ms > 5000:    # 5s freshness
        return None

    threshold_bps = float(STRATEGY_PARAMS.get("oracle_basis_threshold_bps", 15))
    strong_threshold_bps = float(STRATEGY_PARAMS.get("oracle_strong_threshold_bps", 25))

    if abs(basis_bps) < threshold_bps:
        return None

    is_long = basis_bps > 0

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
    sl_mult = STRATEGY_PARAMS.get("oracle_sl_atr_mult", 0.6)
    tp_mult = STRATEGY_PARAMS.get("oracle_tp_atr_mult", 1.8)
    sl_px = ref_px - sl_mult * atr if is_long else ref_px + sl_mult * atr
    tp_px = ref_px + tp_mult * atr if is_long else ref_px - tp_mult * atr

    is_strong = abs(basis_bps) >= strong_threshold_bps and age_ms < 2000
    conviction = "strong" if is_strong else "weak"

    return {
        "fire_ts": df.index[-1], "ref_price": ref_px, "atr": atr,
        "trade_side": "B" if is_long else "A", "is_long": is_long,
        "sl_px": float(sl_px), "tp_px": float(tp_px),
        "max_hold_bars": int(STRATEGY_PARAMS.get("oracle_max_hold_bars", 3)),  # ~15min on 5m
        "fire_reason": f"oracle_basis{basis_bps:+.1f}bps_age{age_ms}ms",
        "raw_direction": "LONG" if is_long else "SHORT",
        "conviction": conviction,
        "size_multiplier": 1.0 if is_strong else 0.4,
        "basis_bps": basis_bps,
        "age_ms": age_ms,
        "pyth_price": basis.get("pyth_price"),
        "hl_mark": basis.get("hl_mark"),
    }
