"""novel-edges-v1 config — env-overrideable defaults per engine."""
import os
import json

PM_URL              = os.environ.get("PM_URL", "https://portfolio-manager-7df2.onrender.com")
PM_CHECK_ENABLED    = os.environ.get("PM_CHECK_ENABLED", "1") == "1"

# Common ATR-based trade params (overridden per-strategy if specified)
TRADE_PARAMS = {
    "atr_period":       int(os.environ.get("ATR_PERIOD", "14")),
    "sl_atr_mult":      float(os.environ.get("SL_ATR_MULT", "1.5")),
    "tp_atr_mult":      float(os.environ.get("TP_ATR_MULT", "3.5")),
    "max_hold_bars":    int(os.environ.get("MAX_HOLD_BARS", "20")),
}

# Strategy-specific knobs (each prefix matches engine_name)
STRATEGY_PARAMS = {
    # token-unlock
    "tunlk_min_velocity_pct": 0.005,
    "tunlk_rebound_drop_pct": 0.03,
    "tunlk_sl_atr_mult": 2.0,
    "tunlk_tp_atr_mult": 5.0,
    "tunlk_max_hold_bars": 48,
    # hlp-stress
    "hlp_drain_threshold_pct": -0.005,
    "hlp_rise_threshold_pct": 0.005,
    "hlp_sl_atr_mult": 1.5,
    "hlp_tp_atr_mult": 3.5,
    "hlp_max_hold_bars": 12,
    # contagion
    "contagion_min_score": 0.5,
    "contagion_strong_score": 0.65,
    "contagion_sl_atr_mult": 1.2,
    "contagion_tp_atr_mult": 4.0,
    "contagion_max_hold_bars": 18,
    # mev-revert
    "mev_min_impact_pct": 0.012,
    "mev_strong_impact_pct": 0.025,
    "mev_sl_atr_mult": 0.8,
    "mev_tp_atr_mult": 2.0,
    "mev_max_hold_bars": 4,
    # listings-decay
    "listings_max_age_hours": 72,
    "listings_young_threshold_hours": 24,
    "listings_min_funding_hr": 0.0004,
    "listings_strong_funding_hr": 0.0006,
    "listings_min_wick_pct": 0.03,
    "listings_strong_wick_pct": 0.05,
    "listings_sl_atr_mult": 1.5,
    "listings_tp_atr_mult": 3.5,
    "listings_max_hold_bars": 8,
    # lst-discount
    "lst_discount_threshold": -0.004,
    "lst_strong_discount": -0.008,
    "lst_rich_threshold": 0.015,
    "lst_sl_atr_mult": 2.0,
    "lst_tp_atr_mult": 4.5,
    "lst_max_hold_bars": 96,
    # oracle-lag
    "oracle_basis_threshold_bps": 15,
    "oracle_strong_threshold_bps": 25,
    "oracle_sl_atr_mult": 0.6,
    "oracle_tp_atr_mult": 1.8,
    "oracle_max_hold_bars": 3,
}

_overrides_raw = os.environ.get("STRATEGY_PARAMS_OVERRIDES", "").strip()
if _overrides_raw:
    try:
        STRATEGY_PARAMS.update(json.loads(_overrides_raw))
        print(f"[config] STRATEGY_PARAMS overrides applied: "
              f"{list(json.loads(_overrides_raw).keys())}", flush=True)
    except Exception as e:
        print(f"[config] STRATEGY_PARAMS_OVERRIDES parse failed: {e}", flush=True)
