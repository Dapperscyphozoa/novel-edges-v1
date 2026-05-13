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
                    "service": "strategies-bundle-v1",
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

            else:
                _json(self, 404, {"error": "not_found", "path": u.path})
        except Exception as e:
            _json(self, 500, {"error": "internal", "detail": str(e)[:200]})


def main():
    port = int(os.environ.get("PORT", "10000"))
    print(f"strategies-bundle-v1 starting on :{port}", flush=True)
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
