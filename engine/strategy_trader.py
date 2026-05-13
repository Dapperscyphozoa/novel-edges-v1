"""Per-strategy trade executor.

Like the unified trader.py but accepts (strategy_config, coin, signal) and
routes each call to the right engine_name namespace. No live trading — paper
only. Logs every signal + closure to a per-strategy SQLite database via
persistence_strategy.py shim.
"""
from __future__ import annotations
import os
import time
import uuid
import urllib.request
import json
from typing import Optional, Tuple

from .strategies.registry import StrategyConfig
from . import bundle_db_backup


# Per-strategy DB connections (lazy-init)
_db_paths = {}


def _state_dir() -> str:
    d = os.environ.get("STATE_DIR", "/tmp/strats-state")
    os.makedirs(d, exist_ok=True)
    return d


def _db_path(engine_name: str) -> str:
    return os.path.join(_state_dir(), f"{engine_name}.db")


_init_lock = __import__("threading").RLock()
_initialized = set()


def _init_db_for_engine(engine_name: str):
    """Init schema for this strategy's DB (run once)."""
    with _init_lock:
        if engine_name in _initialized: return
        import sqlite3
        p = _db_path(engine_name)
        # Restore from GitHub if available
        bundle_db_backup.restore_on_boot(engine_name, p)
        conn = sqlite3.connect(p, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_signal INTEGER NOT NULL,
            coin TEXT, side TEXT, conviction TEXT,
            ref_price REAL, fire_reason TEXT, traded INTEGER DEFAULT 0,
            details TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (
            cloid TEXT PRIMARY KEY,
            ts_open INTEGER NOT NULL,
            coin TEXT, side TEXT, size REAL, entry_px REAL,
            sl_px REAL, tp_px REAL, notional REAL, conviction TEXT,
            size_multiplier REAL, pm_confluence_mult REAL,
            max_hold_ms INTEGER,
            status TEXT DEFAULT 'open'
        )""")
        # Migrate older DBs to add max_hold_ms column if missing
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
            if "max_hold_ms" not in cols:
                conn.execute("ALTER TABLE trades ADD COLUMN max_hold_ms INTEGER")
        except Exception:
            pass
        conn.execute("""CREATE TABLE IF NOT EXISTS closures (
            cloid TEXT PRIMARY KEY,
            ts_close INTEGER NOT NULL,
            exit_px REAL, outcome TEXT, net_pnl REAL, bps_return REAL
        )""")
        conn.close()
        _initialized.add(engine_name)
        # Start background backup thread (only once globally)
        bundle_db_backup.start_background_thread()


def _pm_check(engine_name: str, coin: str, side: str, notional: float,
                sl_distance_pct: Optional[float]) -> dict:
    """Call PM /check for this strategy."""
    pm_url = os.environ.get("PM_URL", "https://portfolio-manager-7df2.onrender.com")
    try:
        body = json.dumps({
            "engine": engine_name, "coin": coin, "side": side,
            "notional": notional, "sl_distance_pct": sl_distance_pct,
            "is_live": False,
        }).encode()
        req = urllib.request.Request(f"{pm_url}/check", data=body, method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"allow": True, "reason": "pm_unreachable_fail_open",
                "size_fraction": 1.0, "confluence_mult": 1.0}


def _has_open_position(engine_name: str, coin: str, side: str) -> bool:
    import sqlite3
    conn = sqlite3.connect(_db_path(engine_name))
    try:
        row = conn.execute(
            "SELECT cloid FROM trades WHERE coin=? AND side=? AND status='open' LIMIT 1",
            (coin, side)).fetchone()
        return row is not None
    finally:
        conn.close()


def execute_signal(strategy: StrategyConfig, coin: str, signal: dict):
    """Execute a paper trade for a signal fire."""
    _init_db_for_engine(strategy.engine_name)
    import sqlite3

    side = signal.get("trade_side", "")
    if side not in ("A", "B"): return

    # Skip if already have open position on this coin/side (dedupe)
    if _has_open_position(strategy.engine_name, coin, side):
        return

    ref_px = float(signal["ref_price"])
    sl_px = float(signal["sl_px"])
    tp_px = float(signal["tp_px"])
    conviction = signal.get("conviction", "strong")

    # Fixed base notional per fire. Multiple fires = natural DCA stacking.
    # No tiered sizing — strong and weak both contribute one unit. User controls
    # account-level scaling separately when ready.
    base_notional = float(os.environ.get("BASE_NOTIONAL_USD", "100"))
    notional = base_notional
    size_mult = 1.0
    sl_distance_pct = abs(ref_px - sl_px) / ref_px

    # PM check
    pm = _pm_check(strategy.engine_name, coin, side, notional, sl_distance_pct)
    if not pm.get("allow", False):
        # Log as untraded signal
        conn = sqlite3.connect(_db_path(strategy.engine_name))
        try:
            conn.execute("INSERT INTO signals (ts_signal, coin, side, conviction, "
                          "ref_price, fire_reason, traded, details) "
                          "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                          (int(time.time()*1000), coin, side, conviction, ref_px,
                           signal.get("fire_reason", ""),
                           json.dumps({"pm_deny": pm.get("reason")}, default=str)))
            conn.commit()
        finally:
            conn.close()
        return

    # NO confluence multiplier. If multiple engines have signals, they each
    # fire their own trade independently — that IS the DCA stack. Don't
    # double-count by inflating one fire on behalf of others.
    conf_mult = 1.0

    # Open the paper trade
    cloid = f"{strategy.cloid_prefix}{uuid.uuid4().hex[:8]}"
    size = notional / ref_px

    conn = sqlite3.connect(_db_path(strategy.engine_name))
    try:
        conn.execute("INSERT INTO signals (ts_signal, coin, side, conviction, "
                      "ref_price, fire_reason, traded, details) "
                      "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                      (int(time.time()*1000), coin, side, conviction, ref_px,
                       signal.get("fire_reason", ""), json.dumps(signal, default=str)))
        # Compute max_hold deadline from signal's max_hold_bars × strategy timeframe
        tf_min_map = {"1m":1, "5m":5, "15m":15, "1h":60, "4h":240}
        tf_min = tf_min_map.get(strategy.timeframe, 5)
        max_hold_bars = signal.get("max_hold_bars", 8)
        max_hold_ms = max_hold_bars * tf_min * 60 * 1000
        conn.execute("INSERT INTO trades (cloid, ts_open, coin, side, size, entry_px, "
                      "sl_px, tp_px, notional, conviction, size_multiplier, "
                      "pm_confluence_mult, max_hold_ms, status) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
                      (cloid, int(time.time()*1000), coin, side, size, ref_px,
                       sl_px, tp_px, notional, conviction, size_mult, conf_mult,
                       max_hold_ms))
        print(f"[{strategy.engine_name}] PAPER OPEN {coin} {side} "
              f"sz={size:.4f} ntl=${notional:.0f} conv={conviction} "
              f"conf×{conf_mult:.1f} reason={signal.get('fire_reason','?')}", flush=True)
        bundle_db_backup.schedule_backup(strategy.engine_name, _db_path(strategy.engine_name))
        conn.commit()
    finally:
        conn.close()
