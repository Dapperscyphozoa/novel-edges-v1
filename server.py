"""HTTP server for strategies-bundle-v1.

Per-strategy endpoints:
  GET  /health                            — service health
  GET  /strategies                        — list of enabled strategies
  GET  /state/{engine_name}               — open trades + recent closures
  GET  /signals/{engine_name}?limit=20    — recent signal evaluations
  GET  /trades/{engine_name}?limit=20     — recent trades
  GET  /closures/{engine_name}?limit=20   — recent closures
  GET  /pnl/{engine_name}                 — aggregate stats per strategy
  GET  /pnl                               — aggregate stats across all strategies
"""
from __future__ import annotations
import os
import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from engine.strategies.registry import enabled_strategies, all_strategies
from engine import multi_scanner, position_monitor, hl_cache


def _state_dir() -> str:
    d = os.environ.get("STATE_DIR", "/tmp/strats-state")
    os.makedirs(d, exist_ok=True)
    return d


def _db_path(engine_name: str) -> str:
    return os.path.join(_state_dir(), f"{engine_name}.db")


def _json(handler, status, payload):
    body = json.dumps(payload, default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _query_db(engine_name: str, sql: str, params: tuple = ()) -> list:
    p = _db_path(engine_name)
    if not os.path.exists(p): return []
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        try:
            if u.path == "/health":
                _json(self, 200, {
                    "ok": True,
                    "service": "novel-edges-v1",
                    "strategies": [s.engine_name for s in enabled_strategies()],
                    "ts": int(time.time()*1000),
                })

            elif u.path == "/strategies":
                _json(self, 200, {
                    "enabled": [
                        {"engine_name": s.engine_name, "timeframe": s.timeframe,
                         "scan_interval_sec": s.scan_interval_sec,
                         "universe": s.universe}
                        for s in enabled_strategies()
                    ],
                    "all": [s.engine_name for s in all_strategies()],
                })

            elif u.path.startswith("/state/"):
                name = u.path.split("/state/", 1)[1]
                open_trades = _query_db(name, "SELECT * FROM trades WHERE status='open'")
                recent_closures = _query_db(name,
                    "SELECT * FROM closures ORDER BY ts_close DESC LIMIT 10")
                _json(self, 200, {
                    "engine": name,
                    "open_trades": open_trades,
                    "recent_closures": recent_closures,
                })

            elif u.path.startswith("/signals/"):
                name = u.path.split("/signals/", 1)[1]
                limit = int(q.get("limit", ["20"])[0])
                rows = _query_db(name,
                    "SELECT * FROM signals ORDER BY ts_signal DESC LIMIT ?",
                    (limit,))
                _json(self, 200, {"engine": name, "signals": rows})

            elif u.path.startswith("/trades/"):
                name = u.path.split("/trades/", 1)[1]
                limit = int(q.get("limit", ["20"])[0])
                rows = _query_db(name,
                    "SELECT * FROM trades ORDER BY ts_open DESC LIMIT ?", (limit,))
                _json(self, 200, {"engine": name, "trades": rows})

            elif u.path.startswith("/closures/"):
                name = u.path.split("/closures/", 1)[1]
                limit = int(q.get("limit", ["20"])[0])
                rows = _query_db(name,
                    "SELECT * FROM closures ORDER BY ts_close DESC LIMIT ?", (limit,))
                _json(self, 200, {"engine": name, "closures": rows})

            elif u.path == "/pnl":
                summary = {}
                for s in enabled_strategies():
                    closures = _query_db(s.engine_name,
                        "SELECT outcome, net_pnl, bps_return FROM closures")
                    n = len(closures)
                    n_tp = sum(1 for c in closures if c["outcome"] == "TP")
                    n_sl = sum(1 for c in closures if c["outcome"] == "SL")
                    total_pnl = sum(c["net_pnl"] or 0 for c in closures)
                    wr = (n_tp / n * 100) if n > 0 else None
                    summary[s.engine_name] = {
                        "n_closed": n, "n_tp": n_tp, "n_sl": n_sl,
                        "win_rate_pct": wr, "total_pnl_usd": total_pnl,
                    }
                _json(self, 200, summary)

            elif u.path.startswith("/pnl/"):
                name = u.path.split("/pnl/", 1)[1]
                closures = _query_db(name,
                    "SELECT outcome, net_pnl, bps_return FROM closures")
                n = len(closures)
                n_tp = sum(1 for c in closures if c["outcome"] == "TP")
                total_pnl = sum(c["net_pnl"] or 0 for c in closures)
                _json(self, 200, {
                    "engine": name, "n_closed": n, "n_tp": n_tp,
                    "win_rate_pct": (n_tp/n*100) if n > 0 else None,
                    "total_pnl_usd": total_pnl,
                })

            elif u.path.startswith("/diagnostics/"):
                name = u.path.split("/diagnostics/", 1)[1]
                # Per-engine, per-coin readiness snapshot.
                from engine.strategies.registry import all_strategies
                from engine import novel_data
                from engine.config import STRATEGY_PARAMS
                strat = next((s for s in all_strategies() if s.engine_name == name), None)
                if not strat:
                    _json(self, 404, {"error": "unknown_engine", "engine": name})
                    return
                rows = []
                for coin in strat.universe:
                    row = {"coin": coin, "ready": False, "reason": "", "values": {}}
                    if name == "token-unlock-v1":
                        v = novel_data.get_supply_velocity(coin)
                        if not v: row["reason"] = "no_supply_data"
                        elif v.get("delta_24h_pct") is None: row["reason"] = "warmup_no_24h_delta"
                        else:
                            spike = (v["delta_24h_pct"] or 0) - (v.get("delta_7d_avg_pct") or 0)
                            thresh = STRATEGY_PARAMS.get("tunlk_min_velocity_pct", 0.003)
                            row["values"] = {"d24h": v["delta_24h_pct"], "d7d_avg": v.get("delta_7d_avg_pct"),
                                             "spike": spike, "threshold": thresh,
                                             "age_sec": v.get("age_sec")}
                            row["ready"] = abs(spike) >= thresh
                            row["reason"] = "ok" if row["ready"] else f"spike_{spike*100:+.3f}pct_under_thresh"
                    elif name == "hlp-stress-v1":
                        v = novel_data.get_hlp_stress()
                        if not v: row["reason"] = "no_hlp_data"
                        elif v.get("drain_1h_pct") is None: row["reason"] = "warmup_no_1h_drain"
                        else:
                            drain = v["drain_1h_pct"]
                            drain_t = STRATEGY_PARAMS.get("hlp_drain_threshold_pct", -0.003)
                            rise_t = STRATEGY_PARAMS.get("hlp_rise_threshold_pct", 0.003)
                            row["values"] = {"drain_1h_pct": drain, "drain_24h_pct": v.get("drain_24h_pct"),
                                             "drain_thresh": drain_t, "rise_thresh": rise_t,
                                             "age_sec": v.get("age_sec")}
                            row["ready"] = drain <= drain_t or drain >= rise_t
                            row["reason"] = "ok" if row["ready"] else f"drain_{drain*100:+.3f}pct_in_no_signal_zone"
                    elif name == "contagion-v1":
                        whale = novel_data.get_whale_stress_signals()
                        if not whale: row["reason"] = "no_whale_data"
                        else:
                            entry = (whale.get("stress_by_coin") or {}).get(coin)
                            if not entry: row["reason"] = "no_whale_in_this_coin"
                            else:
                                score = entry.get("stress_score", 0); n = entry.get("n_whales_at_risk", 0)
                                thresh = STRATEGY_PARAMS.get("contagion_min_score", 0.5)
                                row["values"] = {"score": score, "n_whales": n,
                                                 "side": entry.get("dominant_side"),
                                                 "threshold": thresh,
                                                 "age_sec": whale.get("age_sec")}
                                row["ready"] = score >= thresh
                                row["reason"] = "ok" if row["ready"] else f"score_{score:.2f}_under_thresh"
                    elif name == "mev-revert-v1":
                        v = novel_data.get_mev_dislocation(coin)
                        if not v: row["reason"] = "no_fresh_mev_swap"
                        else:
                            impact = v.get("pct_impact", 0)
                            thresh = STRATEGY_PARAMS.get("mev_min_impact_pct", 0.008)
                            age = v.get("age_sec", 99999)
                            row["values"] = {"impact_pct": impact, "direction": v.get("direction_dex_pushed"),
                                             "age_sec": age, "threshold": thresh}
                            row["ready"] = abs(impact) >= thresh and age <= 90
                            row["reason"] = "ok" if row["ready"] else (
                                f"impact_{impact*100:+.2f}pct_under_thresh" if abs(impact) < thresh
                                else f"stale_age_{age:.0f}s")
                    elif name == "listings-decay-v1":
                        v = novel_data.get_listing_age_and_funding(coin)
                        if not v: row["reason"] = "not_in_listing_cache"
                        else:
                            age_hr = v.get("listing_age_hours", 99999)
                            fund = v.get("funding_rate_hr", 0)
                            min_fund = STRATEGY_PARAMS.get("listings_min_funding_hr", 0.00025)
                            row["values"] = {"age_hr": age_hr, "funding_hr": fund,
                                             "max_age_hr": 72, "min_funding_hr": min_fund}
                            young = age_hr <= 72
                            funded = abs(fund) >= min_fund
                            row["ready"] = young and funded
                            row["reason"] = ("ok" if row["ready"] else
                                "too_old" if not young else f"funding_{fund*100:+.4f}_under_thresh")
                    elif name == "lst-discount-v1":
                        v = novel_data.get_lst_discounts()
                        if not v: row["reason"] = "no_lst_data"
                        else:
                            avg = v.get("avg_discount", 0)
                            disc_t = STRATEGY_PARAMS.get("lst_discount_threshold", -0.0025)
                            rich_t = STRATEGY_PARAMS.get("lst_rich_threshold", 0.015)
                            row["values"] = {"avg_disc": avg, "stETH": v.get("stETH"),
                                             "disc_thresh": disc_t, "rich_thresh": rich_t,
                                             "age_sec": v.get("age_sec")}
                            row["ready"] = avg <= disc_t or avg >= rich_t
                            row["reason"] = "ok" if row["ready"] else f"avg_{avg*100:+.3f}pct_at_parity"
                    elif name == "oracle-lag-v1":
                        v = novel_data.get_pyth_hl_basis(coin)
                        if not v: row["reason"] = "no_pyth_basis_data"
                        else:
                            basis = v.get("basis_bps", 0)
                            thresh = STRATEGY_PARAMS.get("oracle_basis_threshold_bps", 10)
                            age = v.get("age_ms", 99999)
                            row["values"] = {"basis_bps": basis, "pyth": v.get("pyth_price"),
                                             "hl_mark": v.get("hl_mark"),
                                             "age_ms": age, "threshold_bps": thresh}
                            row["ready"] = abs(basis) >= thresh and age <= 5000
                            row["reason"] = "ok" if row["ready"] else (
                                f"basis_{basis:+.2f}bp_under_thresh" if abs(basis) < thresh
                                else f"stale_age_{age}ms")
                    rows.append(row)
                ready_n = sum(1 for r in rows if r["ready"])
                _json(self, 200, {
                    "engine": name,
                    "universe_size": len(rows),
                    "ready_count": ready_n,
                    "coins": rows,
                })

            else:
                _json(self, 404, {"error": "not_found", "path": u.path})
        except Exception as e:
            _json(self, 500, {"error": "internal", "detail": str(e)[:200]})


def main():
    port = int(os.environ.get("PORT", "10000"))
    print(f"novel-edges-v1 starting on :{port}", flush=True)
    print(f"  enabled strategies: {[s.engine_name for s in enabled_strategies()]}", flush=True)

    # Start shared HL data + OI cache (seeds OI history immediately)
    hl_cache.start_oi_thread()
    # Start scanners and monitor
    from engine import novel_data
    novel_data.start_all()
    multi_scanner.start_all()
    position_monitor.start()

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
