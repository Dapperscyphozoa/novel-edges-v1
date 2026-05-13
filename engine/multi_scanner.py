"""Multi-strategy scanner for novel-edges-v1.

Each strategy runs in its own thread, scans its universe, attaches its
strategy-specific data (from engine.novel_data), evaluates and routes
to the per-strategy paper trader.
"""
from __future__ import annotations
import os
import threading
import time
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from .strategies.registry import StrategyConfig, enabled_strategies
from . import db_backup
from . import hl_cache
from . import novel_data


def _set_env_for_engine(strategy: StrategyConfig):
    os.environ["ENGINE_NAME"] = strategy.engine_name
    os.environ["CLOID_PREFIX"] = strategy.cloid_prefix


def _fetch_candles(coin: str, interval: str, n: int) -> Optional[pd.DataFrame]:
    return hl_cache.get_candles(coin, interval, n)


def _attach_strategy_data(strategy: StrategyConfig, df: pd.DataFrame):
    name = strategy.engine_name
    coin = df.attrs.get("coin", "")
    df.attrs["timeframe"] = strategy.timeframe

    if name == "token-unlock-v1":
        df.attrs["supply_velocity"] = novel_data.get_supply_velocity(coin)

    elif name == "hlp-stress-v1":
        df.attrs["hlp_stress"] = novel_data.get_hlp_stress()

    elif name == "contagion-v1":
        whale = novel_data.get_whale_stress_signals()
        if whale:
            df.attrs["whale_stress"] = whale.get("stress_by_coin", {})
            df.attrs["whale_stress_age_sec"] = whale.get("age_sec", 99999)

    elif name == "mev-revert-v1":
        df.attrs["mev_dislocation"] = novel_data.get_mev_dislocation(coin)

    elif name == "listings-decay-v1":
        info = novel_data.get_listing_age_and_funding(coin)
        if info:
            df.attrs["listing_age_hours"] = info.get("listing_age_hours")
            df.attrs["funding_rate_hr"] = info.get("funding_rate_hr")

    elif name == "lst-discount-v1":
        df.attrs["lst_discounts"] = novel_data.get_lst_discounts()

    elif name == "oracle-lag-v1":
        df.attrs["pyth_hl_basis"] = novel_data.get_pyth_hl_basis(coin)


def _scan_one_coin(strategy: StrategyConfig, evaluate_fn, coin: str):
    df = _fetch_candles(coin, strategy.timeframe, strategy.history_bars)
    if df is None or len(df) < 30:
        return
    df.attrs["coin"] = coin
    _attach_strategy_data(strategy, df)

    try:
        signal = evaluate_fn(df)
    except Exception as e:
        print(f"[{strategy.engine_name}] evaluate err on {coin}: {e}", flush=True)
        return

    if signal is None:
        return

    _set_env_for_engine(strategy)
    try:
        from . import strategy_trader
        strategy_trader.execute_signal(strategy, coin, signal)
    except Exception as e:
        import traceback
        print(f"[{strategy.engine_name}] trade err on {coin}: {e}", flush=True)
        print(traceback.format_exc()[:500], flush=True)


def _scan_loop(strategy: StrategyConfig):
    evaluate_fn = strategy.evaluate()
    print(f"[{strategy.engine_name}] scan loop starting "
          f"(interval={strategy.scan_interval_sec}s, "
          f"tf={strategy.timeframe}, "
          f"universe={strategy.universe[:5]}{'…' if len(strategy.universe)>5 else ''} "
          f"({len(strategy.universe)} coins))", flush=True)
    while True:
        try:
            pace_sec = float(os.environ.get("SCAN_PACE_SEC", "3.0"))
            for coin in strategy.universe:
                _scan_one_coin(strategy, evaluate_fn, coin)
                time.sleep(pace_sec)
        except Exception as e:
            print(f"[{strategy.engine_name}] scan loop err: {e}", flush=True)
        time.sleep(strategy.scan_interval_sec)


def start_all():
    strategies = enabled_strategies()
    threads = []
    for i, s in enumerate(strategies):
        delay = i * 5
        def _delayed_start(strat, d):
            time.sleep(d)
            _scan_loop(strat)
        t = threading.Thread(target=_delayed_start, args=(s, delay), daemon=True,
                              name=f"scan_{s.engine_name}")
        t.start()
        threads.append(t)
    print(f"[novel-edges] started {len(strategies)} strategy threads", flush=True)
    return threads
