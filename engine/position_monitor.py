"""Position monitor — dumb enforcer of what each strategy specifies.

Each strategy's signal dict specifies: sl_px, tp_px, max_hold_bars.
This monitor only enforces those three.

Anything fancier (partial TPs, breakeven, trailing, basis-convergence exits,
z-score reversion exits) is the STRATEGY's responsibility, embedded in its
signal logic or a future per-strategy exit_condition hook. No universal rules.
"""
from __future__ import annotations
import os
import time
import sqlite3
import urllib.request
import json
import threading

from .strategies.registry import enabled_strategies
from . import bundle_db_backup


def _state_dir() -> str:
    d = os.environ.get("STATE_DIR", "/tmp/strats-state")
    os.makedirs(d, exist_ok=True)
    return d


def _db_path(engine_name: str) -> str:
    return os.path.join(_state_dir(), f"{engine_name}.db")


def _hl_mids() -> dict:
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=b'{"type":"allMids"}',
            headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return {k: float(v) for k, v in json.loads(r.read()).items()}
    except Exception as e:
        print(f"[monitor] hl_mids err: {e}", flush=True)
        return {}


def _check_open_trades(engine_name: str, mids: dict):
    p = _db_path(engine_name)
    if not os.path.exists(p): return
    conn = sqlite3.connect(p, isolation_level=None)
    try:
        rows = conn.execute("""SELECT cloid, ts_open, coin, side, size, entry_px,
                                       sl_px, tp_px, notional, conviction, max_hold_ms
                                FROM trades WHERE status='open'""").fetchall()
        if not rows: return

        for cloid, ts_open, coin, side, size, entry_px, sl_px, tp_px, notional, conviction, max_hold_ms in rows:
            cur = mids.get(coin)
            if not cur: continue
            is_long = (side == "B")

            outcome = None
            exit_px = cur

            if is_long and cur <= sl_px:       outcome, exit_px = "SL", sl_px
            elif (not is_long) and cur >= sl_px: outcome, exit_px = "SL", sl_px
            elif is_long and cur >= tp_px:     outcome, exit_px = "TP", tp_px
            elif (not is_long) and cur <= tp_px: outcome, exit_px = "TP", tp_px
            elif max_hold_ms and (int(time.time()*1000) - ts_open) > max_hold_ms:
                outcome, exit_px = "TIME", cur

            if outcome:
                if is_long: gross = (exit_px - entry_px) * size
                else:        gross = (entry_px - exit_px) * size
                fees = notional * 0.0005
                net = gross - fees
                bps = (net / notional) * 1e4 if notional > 0 else 0
                conn.execute("""INSERT OR REPLACE INTO closures
                                (cloid, ts_close, exit_px, outcome, net_pnl, bps_return)
                                VALUES (?, ?, ?, ?, ?, ?)""",
                              (cloid, int(time.time()*1000), exit_px, outcome, net, bps))
                conn.execute("UPDATE trades SET status=? WHERE cloid=?",
                              ("closed_" + outcome.lower(), cloid))
                print(f"[{engine_name}] PAPER CLOSE {coin} {side} {outcome} "
                      f"net=${net:+.2f} ({bps:+.0f}bp) entry={entry_px:.4f} "
                      f"exit={exit_px:.4f}", flush=True)
                bundle_db_backup.schedule_backup(engine_name, _db_path(engine_name))
    finally:
        conn.close()


def _monitor_loop():
    interval = int(os.environ.get("MONITOR_INTERVAL_SEC", "30"))
    strategies = enabled_strategies()
    engine_names = [s.engine_name for s in strategies]
    print(f"[monitor] starting (interval={interval}s, engines={len(engine_names)})", flush=True)
    while True:
        try:
            mids = _hl_mids()
            if mids:
                for name in engine_names:
                    _check_open_trades(name, mids)
        except Exception as e:
            print(f"[monitor] loop err: {e}", flush=True)
        time.sleep(interval)


def start():
    t = threading.Thread(target=_monitor_loop, daemon=True, name="position_monitor")
    t.start()
    return t
