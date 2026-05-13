"""Shared HL data cache. Single fetcher per (coin, interval), serves cached
copies to all strategies. Drops API load ~7x for overlapping coins.

Also fetches OI snapshots on a slow timer and builds rolling OI history
for oi-divergence-v1.
"""
from __future__ import annotations
import json
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple
import numpy as np
import pandas as pd


# Cache: {(coin, interval): {"ts": fetched_at, "df": dataframe}}
_CACHE: Dict[Tuple[str, str], dict] = {}
_LOCK = threading.RLock()

# Cache TTL per timeframe (seconds). Aim for < bar duration so fresh data flows.
_TTL = {
    "1m":  30,    # half a bar
    "5m":  60,    # well within bar
    "15m": 120,
    "1h":  300,
    "4h":  600,
}

# OI history: {coin: [(ts, oi_usd), ...]} — last 200 snapshots
_OI_HISTORY: Dict[str, list] = {}
_OI_LOCK = threading.RLock()


def get_candles(coin: str, interval: str, n: int) -> Optional[pd.DataFrame]:
    """Return cached candles if fresh, else fetch + cache + return."""
    key = (coin, interval)
    ttl = _TTL.get(interval, 60)
    now = time.time()

    with _LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached["ts"] < ttl:
            df = cached["df"]
            return df.iloc[-n:] if len(df) > n else df

    # Need to fetch
    try:
        end_ms = int(now * 1000)
        sec_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
        start_ms = end_ms - (max(n, 50) * sec_map.get(interval, 300) * 1000)
        body = json.dumps({"type": "candleSnapshot",
                            "req": {"coin": coin, "interval": interval,
                                    "startTime": start_ms, "endTime": end_ms}}).encode()
        req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=body,
                                       headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = json.loads(r.read())
        if not raw or not isinstance(raw, list): return None
        df = pd.DataFrame(raw)
        for c_src, c_dst in [("c","close"),("h","high"),("l","low"),
                              ("o","open"),("v","volume")]:
            df[c_dst] = df[c_src].astype(float)
        df.index = pd.to_datetime(df["T"].astype(int), unit="ms", utc=True)
        df.attrs["coin"] = coin

        with _LOCK:
            _CACHE[key] = {"ts": now, "df": df}
        return df.iloc[-n:] if len(df) > n else df
    except Exception as e:
        print(f"[hl_cache] fetch err {coin} {interval}: {e}", flush=True)
        return None


def get_mids() -> Optional[dict]:
    """Cached HL allMids (1s TTL)."""
    key = ("__MIDS__", "1s")
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.time() - cached["ts"] < 1.5:
            return cached["df"]   # actually a dict here

    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=b'{"type":"allMids"}',
            headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            mids = {k: float(v) for k, v in json.loads(r.read()).items()}
        with _LOCK:
            _CACHE[key] = {"ts": time.time(), "df": mids}
        return mids
    except Exception: return None


def _refresh_oi_history():
    """Fetch HL metaAndAssetCtxs every 15min, append OI snapshot per coin."""
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=b'{"type":"metaAndAssetCtxs"}',
            headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read())
        if not isinstance(data, list) or len(data) < 2: return
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
        now = time.time()
        with _OI_LOCK:
            for u, ctx in zip(universe, ctxs):
                name = u.get("name", "")
                if not name or not ctx: continue
                oi = ctx.get("openInterest")
                mark = ctx.get("markPx")
                if oi is None or mark is None: continue
                try:
                    oi_usd = float(oi) * float(mark)
                except: continue
                hist = _OI_HISTORY.setdefault(name, [])
                hist.append((now, oi_usd))
                # Keep last 200 snapshots (=50h at 15min cadence)
                if len(hist) > 200: hist[:] = hist[-200:]
    except Exception as e:
        print(f"[hl_cache] OI refresh err: {e}", flush=True)


def get_oi_history(coin: str, n: int) -> Optional[np.ndarray]:
    """Return last n OI values for a coin, or None if not enough history."""
    with _OI_LOCK:
        hist = _OI_HISTORY.get(coin, [])
        if len(hist) < n: return None
        return np.array([v for _, v in hist[-n:]])


def _oi_loop():
    """Background thread: refresh OI every 15min."""
    while True:
        try:
            _refresh_oi_history()
        except Exception as e:
            print(f"[hl_cache] oi loop err: {e}", flush=True)
        time.sleep(900)   # 15 minutes


_oi_thread_started = False


def start_oi_thread():
    global _oi_thread_started
    if _oi_thread_started: return
    _oi_thread_started = True
    # Fetch immediately to seed history (otherwise oi-divergence won't fire
    # until 16 cycles × 15min = 4 hours after deploy)
    _refresh_oi_history()
    t = threading.Thread(target=_oi_loop, daemon=True, name="hl_oi_refresher")
    t.start()
    print(f"[hl_cache] OI refresher started (interval=15min, seeded with 1 snapshot)",
          flush=True)
