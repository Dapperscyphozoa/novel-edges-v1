"""Strategy registry — 7 novel-edge engines.

Each strategy module must expose:
  evaluate_latest_bar(df: pd.DataFrame) -> Optional[dict]
"""
from __future__ import annotations
import importlib
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class StrategyConfig:
    engine_name: str
    cloid_prefix: str
    module_path: str
    timeframe: str = "5m"
    scan_interval_sec: int = 120
    history_bars: int = 200
    universe: List[str] = field(default_factory=list)
    enabled: bool = True

    def evaluate(self):
        mod = importlib.import_module(self.module_path)
        return getattr(mod, "evaluate_latest_bar")


def _csv_env(key: str, default: str) -> List[str]:
    raw = os.environ.get(key, default).strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


# Common HL-perp universe
DEFAULT_UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK", "XRP", "BNB",
                    "ADA", "TON", "NEAR", "ARB", "OP", "SUI", "INJ", "TIA",
                    "JUP", "HYPE", "AAVE", "WIF", "PEPE", "FET", "APT", "ATOM",
                    "DOT", "FIL", "LDO", "WLD", "STX", "IMX", "SEI", "PYTH",
                    "JTO", "STRK", "ENA", "ETHFI", "RENDER"]


def all_strategies() -> List[StrategyConfig]:
    return [
        # 1) Token unlock — supply-velocity fade. Slow signal (6h cache); 15m bars; broad universe.
        StrategyConfig(
            engine_name="token-unlock-v1",
            cloid_prefix="tunlk_",
            module_path="engine.strategies.token_unlock",
            timeframe="1h",
            scan_interval_sec=int(os.environ.get("TUNLK_INTERVAL", "1800")),  # 30min
            history_bars=200,
            universe=_csv_env("TUNLK_UNIVERSE", ",".join(DEFAULT_UNIVERSE)),
            enabled=os.environ.get("TUNLK_ENABLED", "1") == "1",
        ),
        # 2) HLP stress — vault drain → fade extension. Fires on BTC/ETH primarily.
        StrategyConfig(
            engine_name="hlp-stress-v1",
            cloid_prefix="hlpst_",
            module_path="engine.strategies.hlp_stress",
            timeframe="5m",
            scan_interval_sec=int(os.environ.get("HLPST_INTERVAL", "180")),
            history_bars=100,
            universe=_csv_env("HLPST_UNIVERSE",
                              ",".join(["BTC", "ETH", "SOL", "HYPE", "DOGE"])),
            enabled=os.environ.get("HLPST_ENABLED", "1") == "1",
        ),
        # 3) Cross-margin contagion — whale stress forecast. 15m bars; full universe.
        StrategyConfig(
            engine_name="contagion-v1",
            cloid_prefix="ctgon_",
            module_path="engine.strategies.contagion",
            timeframe="15m",
            scan_interval_sec=int(os.environ.get("CTGON_INTERVAL", "120")),
            history_bars=120,
            universe=_csv_env("CTGON_UNIVERSE", ",".join(DEFAULT_UNIVERSE)),
            enabled=os.environ.get("CTGON_ENABLED", "1") == "1",
        ),
        # 4) MEV revert — Uniswap-v3 swap dislocation. Fast scan (60s), narrow universe.
        StrategyConfig(
            engine_name="mev-revert-v1",
            cloid_prefix="mevrv_",
            module_path="engine.strategies.mev_revert",
            timeframe="5m",
            scan_interval_sec=int(os.environ.get("MEVRV_INTERVAL", "45")),
            history_bars=80,
            universe=_csv_env("MEVRV_UNIVERSE",
                              ",".join(["LINK", "AAVE", "LDO", "PEPE", "FET", "RENDER"])),
            enabled=os.environ.get("MEVRV_ENABLED", "1") == "1",
        ),
        # 5) Listings decay — recent-listing IV crush; 15m bars; full universe (filtered by age internally).
        StrategyConfig(
            engine_name="listings-decay-v1",
            cloid_prefix="lstdc_",
            module_path="engine.strategies.listings_decay",
            timeframe="15m",
            scan_interval_sec=int(os.environ.get("LSTDC_INTERVAL", "300")),
            history_bars=120,
            universe=_csv_env("LSTDC_UNIVERSE", ",".join(DEFAULT_UNIVERSE)),
            enabled=os.environ.get("LSTDC_ENABLED", "1") == "1",
        ),
        # 6) LST discount — ETH-only signal.
        StrategyConfig(
            engine_name="lst-discount-v1",
            cloid_prefix="lstds_",
            module_path="engine.strategies.lst_discount",
            timeframe="15m",
            scan_interval_sec=int(os.environ.get("LSTDS_INTERVAL", "600")),
            history_bars=200,
            universe=_csv_env("LSTDS_UNIVERSE", "ETH"),
            enabled=os.environ.get("LSTDS_ENABLED", "1") == "1",
        ),
        # 7) Oracle lag — Pyth vs HL mark; fast scan; major coins.
        StrategyConfig(
            engine_name="oracle-lag-v1",
            cloid_prefix="oralg_",
            module_path="engine.strategies.oracle_lag",
            timeframe="5m",
            scan_interval_sec=int(os.environ.get("ORALG_INTERVAL", "30")),
            history_bars=60,
            universe=_csv_env("ORALG_UNIVERSE",
                              ",".join(["BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK",
                                        "ARB", "OP", "SUI", "BNB", "ADA"])),
            enabled=os.environ.get("ORALG_ENABLED", "1") == "1",
        ),
    ]


def enabled_strategies() -> List[StrategyConfig]:
    return [s for s in all_strategies() if s.enabled]
