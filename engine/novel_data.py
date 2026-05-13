"""novel_data.py — data feeds for the 7 novel-edge strategies.

Each fetcher caches with appropriate TTL. Background threads keep caches
warm. Returns None/empty on failure (strategies all guard against this).

Fetchers:
  - get_supply_velocity(coin)      → token-unlock
  - get_hlp_stress()               → hlp-stress
  - get_whale_stress_signals()     → contagion
  - get_mev_dislocation(coin)      → mev-revert
  - get_listing_age_and_funding()  → listings-decay
  - get_lst_discounts()            → lst-discount
  - get_pyth_hl_basis(coin)        → oracle-lag

All HTTP calls have aggressive timeouts (5s default) and fallback to
cached values on failure. Background refresh threads run with daemon=True
and never crash the parent process.
"""
from __future__ import annotations
import json
import os
import threading
import time
import urllib.request
import urllib.parse
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────
# Generic helpers
# ──────────────────────────────────────────────────────────────────────

_LOCK = threading.RLock()

def _http_get(url: str, timeout: float = 5.0, headers: Optional[dict] = None) -> Optional[dict]:
    hdrs = {"User-Agent": "novel-edges/1.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None

def _http_post(url: str, payload: dict, timeout: float = 5.0) -> Optional[dict]:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────
# 1) Supply velocity (Coingecko free) — token-unlock
# ──────────────────────────────────────────────────────────────────────
# Maps HL coin symbol → coingecko id for top-30 unlock-relevant coins
COIN_TO_GECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "AVAX": "avalanche-2",
    "DOGE": "dogecoin", "XRP": "ripple", "LINK": "chainlink", "BNB": "binancecoin",
    "ADA": "cardano", "TON": "the-open-network", "NEAR": "near", "ARB": "arbitrum",
    "OP": "optimism", "SUI": "sui", "INJ": "injective-protocol",
    "TIA": "celestia", "JUP": "jupiter-exchange-solana", "APT": "aptos",
    "FET": "fetch-ai", "RENDER": "render-token", "ATOM": "cosmos",
    "DOT": "polkadot", "FIL": "filecoin", "LDO": "lido-dao", "AAVE": "aave",
    "WLD": "worldcoin-wld", "STX": "blockstack", "IMX": "immutable-x",
    "SEI": "sei-network", "PYTH": "pyth-network", "JTO": "jito-governance-token",
    "STRK": "starknet", "ENA": "ethena", "ETHFI": "ether-fi",
}

_SUPPLY_CACHE: Dict[str, dict] = {}
_SUPPLY_TTL = 6 * 3600

def _refresh_supply():
    """Fetch circulating supply for tracked coins; compute 24h/7d delta vs cached history."""
    now = time.time()
    # Batch — Coingecko allows up to 250 ids in one call
    ids = ",".join(COIN_TO_GECKO.values())
    url = (f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
           f"&ids={urllib.parse.quote(ids)}&order=market_cap_desc&per_page=250&page=1"
           f"&sparkline=false&price_change_percentage=24h")
    data = _http_get(url, timeout=8.0)
    if not isinstance(data, list):
        return
    by_gecko = {row["id"]: row for row in data if isinstance(row, dict)}
    inv_map = {v: k for k, v in COIN_TO_GECKO.items()}
    with _LOCK:
        for gid, row in by_gecko.items():
            coin = inv_map.get(gid)
            if not coin:
                continue
            circ_now = float(row.get("circulating_supply") or 0)
            if circ_now <= 0:
                continue
            existing = _SUPPLY_CACHE.get(coin, {})
            hist = existing.get("history", [])
            hist.append((now, circ_now))
            # prune > 8 days
            cutoff = now - 8 * 86400
            hist = [(t, s) for (t, s) in hist if t > cutoff]
            # compute deltas
            def _at_or_before(target_ts):
                pts = [(t, s) for t, s in hist if t <= target_ts]
                return pts[-1][1] if pts else None
            circ_24h = _at_or_before(now - 86400)
            d24 = (circ_now - circ_24h) / circ_24h if circ_24h else None
            # 7d avg daily
            d7 = []
            for k in range(1, 8):
                target = now - k * 86400
                target_prev = now - (k+1) * 86400
                cur = _at_or_before(target)
                prv = _at_or_before(target_prev)
                if cur and prv and prv > 0:
                    d7.append((cur - prv) / prv)
            d7_avg = sum(d7) / len(d7) if d7 else None
            _SUPPLY_CACHE[coin] = {
                "history": hist,
                "circ_now": circ_now,
                "delta_24h_pct": d24,
                "delta_7d_avg_pct": d7_avg,
                "ts": now,
            }

def get_supply_velocity(coin: str) -> Optional[dict]:
    with _LOCK:
        v = _SUPPLY_CACHE.get(coin)
        if not v:
            return None
        return {
            "delta_24h_pct": v.get("delta_24h_pct"),
            "delta_7d_avg_pct": v.get("delta_7d_avg_pct"),
            "age_sec": time.time() - v.get("ts", 0),
        }


# ──────────────────────────────────────────────────────────────────────
# 2) HLP stress — HL vault drain
# ──────────────────────────────────────────────────────────────────────
HLP_VAULT_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"   # HLP leader
_HLP_HISTORY = deque(maxlen=300)   # (ts, equity_usd) — 25h at 5min cadence
_HLP_LATEST = {"ts": 0, "data": None}

def _refresh_hlp():
    now = time.time()
    res = _http_post(
        "https://api.hyperliquid.xyz/info",
        {"type": "vaultDetails", "vaultAddress": HLP_VAULT_ADDRESS},
        timeout=8.0,
    )
    if not isinstance(res, dict):
        return
    portfolio = res.get("portfolio") or []
    # portfolio is a list of [period_name, data_dict] pairs; we want "allTime"->accountValueHistory tail
    eq = None
    for entry in portfolio:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == "day":
            avh = (entry[1] or {}).get("accountValueHistory") or []
            if avh:
                eq = float(avh[-1][1])
                break
    if eq is None:
        return
    with _LOCK:
        _HLP_HISTORY.append((now, eq))
        # compute drain rates
        def _at_or_before(target_ts):
            pts = [(t, e) for t, e in _HLP_HISTORY if t <= target_ts]
            return pts[-1][1] if pts else None
        eq_1h = _at_or_before(now - 3600)
        eq_24h = _at_or_before(now - 86400)
        d1h = ((eq - eq_1h) / eq_1h) if eq_1h else None
        d24h = ((eq - eq_24h) / eq_24h) if eq_24h else None
        _HLP_LATEST["data"] = {
            "equity_now": eq, "equity_1h_ago": eq_1h, "equity_24h_ago": eq_24h,
            "drain_1h_pct": d1h, "drain_24h_pct": d24h,
        }
        _HLP_LATEST["ts"] = now

def get_hlp_stress() -> Optional[dict]:
    with _LOCK:
        d = _HLP_LATEST.get("data")
        if not d:
            return None
        return {**d, "age_sec": time.time() - _HLP_LATEST.get("ts", 0)}


# ──────────────────────────────────────────────────────────────────────
# 3) Whale stress (HL clearinghouseState for top N) — contagion
# ──────────────────────────────────────────────────────────────────────
# Reused leaderboard endpoint; cache top-N addresses, refresh state every 90s.
_WHALES_LIST: List[str] = []     # addresses
_WHALES_LAST_REFRESH = 0
_WHALES_STATE_CACHE: Dict[str, Any] = {"ts": 0, "stress_by_coin": {}}

def _refresh_whale_list(top_n: int = 30):
    """Scrape HL leaderboard for top-N by 30d PnL (filters: equity > $50k)."""
    global _WHALES_LIST, _WHALES_LAST_REFRESH
    if time.time() - _WHALES_LAST_REFRESH < 3600:
        return
    try:
        # Use undocumented stats endpoint
        req = urllib.request.Request(
            "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        rows = data.get("leaderboardRows", [])
        scored = []
        for row in rows:
            try:
                eq = float(row.get("accountValue", 0))
                if eq < 50000:
                    continue
                # vault leaders excluded
                if row.get("vaultAddress"):
                    continue
                # month window
                wp = row.get("windowPerformances", [])
                month = next((wp for wp in wp if wp[0] == "month"), None)
                if not month or not isinstance(month, list) or len(month) < 2:
                    continue
                pnl_30d = float(month[1].get("pnl", 0))
                if pnl_30d <= 0:
                    continue
                scored.append((pnl_30d, row.get("ethAddress")))
            except Exception:
                continue
        scored.sort(reverse=True)
        _WHALES_LIST = [addr for (_, addr) in scored[:top_n] if addr]
        _WHALES_LAST_REFRESH = time.time()
    except Exception:
        pass

def _whale_state(addr: str) -> Optional[dict]:
    return _http_post(
        "https://api.hyperliquid.xyz/info",
        {"type": "clearinghouseState", "user": addr},
        timeout=6.0,
    )

def _refresh_whale_stress():
    now = time.time()
    _refresh_whale_list()
    if not _WHALES_LIST:
        return
    stress_by_coin = defaultdict(lambda: {
        "scores": [], "n_long": 0, "n_short": 0, "notional_long": 0.0, "notional_short": 0.0,
    })
    for addr in _WHALES_LIST:
        state = _whale_state(addr)
        if not state:
            continue
        ms = state.get("marginSummary", {})
        account_value = float(ms.get("accountValue", 0))
        total_margin = float(ms.get("totalMarginUsed", 0))
        if account_value <= 0 or total_margin <= 0:
            continue
        # ratio: > 1.5 healthy, < 1.2 stressed, < 1.1 imminent
        ratio = account_value / total_margin
        # stress score: 0 if ratio>=1.5, 1 if ratio<=1.05, linear between
        if ratio >= 1.5:
            continue
        score = max(0.0, min(1.0, (1.5 - ratio) / 0.45))
        # find largest position (worst-slippage candidate for liq engine)
        positions = state.get("assetPositions", [])
        if not positions:
            continue
        best = None; best_notl = 0.0
        for ap in positions:
            pos = ap.get("position", {})
            sz = float(pos.get("szi", 0))
            entry_px = float(pos.get("entryPx", 0) or 0)
            if entry_px <= 0:
                continue
            notional = abs(sz) * entry_px
            if notional > best_notl:
                best_notl = notional
                best = pos
        if best is None:
            continue
        coin = best.get("coin")
        side = "LONG" if float(best.get("szi", 0)) > 0 else "SHORT"
        s = stress_by_coin[coin]
        s["scores"].append(score)
        if side == "LONG":
            s["n_long"] += 1; s["notional_long"] += best_notl
        else:
            s["n_short"] += 1; s["notional_short"] += best_notl
        time.sleep(0.1)   # rate-limit safe

    out = {}
    for coin, s in stress_by_coin.items():
        if not s["scores"]:
            continue
        avg_score = sum(s["scores"]) / len(s["scores"])
        if s["n_long"] >= s["n_short"]:
            dominant_side = "LONG"; n = s["n_long"]
        else:
            dominant_side = "SHORT"; n = s["n_short"]
        out[coin] = {
            "stress_score": avg_score,
            "n_whales_at_risk": len(s["scores"]),
            "dominant_side": dominant_side,
            "n_long": s["n_long"], "n_short": s["n_short"],
        }
    with _LOCK:
        _WHALES_STATE_CACHE["ts"] = now
        _WHALES_STATE_CACHE["stress_by_coin"] = out

def get_whale_stress_signals() -> Optional[dict]:
    with _LOCK:
        if not _WHALES_STATE_CACHE.get("stress_by_coin"):
            return None
        return {
            "stress_by_coin": _WHALES_STATE_CACHE["stress_by_coin"],
            "age_sec": time.time() - _WHALES_STATE_CACHE.get("ts", 0),
        }


# ──────────────────────────────────────────────────────────────────────
# 4) MEV dislocation (Uniswap v3 subgraph) — mev-revert
# ──────────────────────────────────────────────────────────────────────
# Maps HL coin → Uniswap v3 pool address (ETH-quoted, deepest tier)
UNI_V3_POOLS = {
    "LINK": "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8",   # LINK/ETH 0.3%
    "AAVE": "0x5ab53ee1d50eef2c1dd3d5402789cd27bb52c1bb",   # AAVE/ETH 0.3%
    "UNI":  "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801",   # UNI/ETH 0.3%
    "PEPE": "0x11950d141ecb863f01007add7d1a342041227b58",   # PEPE/ETH 0.3%
    "FET":  "0xb022d57b6c95cb19f24bf2d8b6c9f17d12e63f78",   # FET/ETH 0.3% (approx)
    "MKR":  "0xe8c6c9227491c0a8156a0106a0204d881bb7e531",   # MKR/ETH 0.3%
    "RENDER": "0x80d9ec9c7c6f5b6b9b6b6b6b6b6b6b6b6b6b6b6b", # placeholder
    "LDO":  "0xa3f558aebaecaf0e11ca4b2199cc5ed341edfd74",   # LDO/ETH 1%
}
_MEV_CACHE: Dict[str, dict] = {}

# GeckoTerminal — free public API for DEX trades (no key required, rate limit ~30/min)
GECKO_TRADES_BASE = "https://api.geckoterminal.com/api/v2/networks/eth/pools"

def _refresh_mev():
    """Poll GeckoTerminal for >$50k trades on tracked Uniswap v3 pools."""
    import datetime as _dt
    now = time.time()
    for coin, pool in UNI_V3_POOLS.items():
        url = (f"{GECKO_TRADES_BASE}/{pool}/trades?"
               f"trade_volume_in_usd_greater_than=50000")
        res = _http_get(url, timeout=6.0)
        if not isinstance(res, dict):
            time.sleep(0.3)
            continue
        trades = res.get("data") or []
        if not trades:
            time.sleep(0.3)
            continue
        # Find the largest recent trade (last 180s)
        best = None
        for tr in trades:
            attrs = tr.get("attributes") or {}
            usd = float(attrs.get("volume_in_usd", 0) or 0)
            ts_iso = attrs.get("block_timestamp")
            if not ts_iso:
                continue
            try:
                ts = _dt.datetime.fromisoformat(ts_iso.replace("Z","+00:00")).timestamp()
            except Exception:
                continue
            if now - ts > 240:
                continue
            kind = attrs.get("kind")  # "buy" = bought token0/perp asset → DEX push UP
            if not kind:
                continue
            direction = "UP" if kind == "buy" else "DOWN"
            # impact estimate: usd / pool_TVL_proxy (~$20M typical for tracked pools)
            impact_raw = min(0.10, usd / 20_000_000)
            impact = impact_raw if direction == "UP" else -impact_raw
            if best is None or usd > best.get("usd", 0):
                best = {
                    "direction_dex_pushed": direction,
                    "pct_impact": impact, "ts": ts, "usd": usd,
                }
        if best:
            with _LOCK:
                _MEV_CACHE[coin] = best
        time.sleep(0.3)  # rate-limit safe (geckoterminal ~30 rpm)

def get_mev_dislocation(coin: str) -> Optional[dict]:
    with _LOCK:
        v = _MEV_CACHE.get(coin)
        if not v:
            return None
        age = time.time() - v.get("ts", 0)
        return {
            "direction_dex_pushed": v.get("direction_dex_pushed"),
            "pct_impact": v.get("pct_impact"),
            "age_sec": age,
        }


# ──────────────────────────────────────────────────────────────────────
# 5) Listing age + funding (HL meta) — listings-decay
# ──────────────────────────────────────────────────────────────────────
_LISTING_FIRST_SEEN: Dict[str, float] = {}    # coin → first-seen ts
_LISTING_FUNDING: Dict[str, float] = {}       # coin → current hourly funding (decimal)
_LISTING_TS = 0

def _refresh_listings():
    global _LISTING_TS
    now = time.time()
    res = _http_post(
        "https://api.hyperliquid.xyz/info",
        {"type": "metaAndAssetCtxs"},
        timeout=6.0,
    )
    if not isinstance(res, list) or len(res) < 2:
        return
    meta = (res[0] or {}).get("universe", [])
    ctxs = res[1] or []
    with _LOCK:
        for i, u in enumerate(meta):
            name = u.get("name") if isinstance(u, dict) else None
            if not name:
                continue
            if name not in _LISTING_FIRST_SEEN:
                _LISTING_FIRST_SEEN[name] = now
            ctx = ctxs[i] if i < len(ctxs) else {}
            try:
                _LISTING_FUNDING[name] = float(ctx.get("funding", 0))
            except Exception:
                _LISTING_FUNDING[name] = 0
        _LISTING_TS = now

def get_listing_age_and_funding(coin: str) -> Optional[dict]:
    with _LOCK:
        first_seen = _LISTING_FIRST_SEEN.get(coin)
        funding = _LISTING_FUNDING.get(coin)
        if first_seen is None or funding is None:
            return None
        age_hr = (time.time() - first_seen) / 3600.0
        return {"listing_age_hours": age_hr, "funding_rate_hr": funding}


# ──────────────────────────────────────────────────────────────────────
# 6) LST discounts (Coingecko) — lst-discount
# ──────────────────────────────────────────────────────────────────────
LST_GECKO_IDS = ["staked-ether", "rocket-pool-eth", "coinbase-wrapped-staked-eth", "ethereum"]
_LST_LATEST = {"ts": 0, "data": None}

def _refresh_lst():
    now = time.time()
    url = ("https://api.coingecko.com/api/v3/simple/price?ids="
           + ",".join(LST_GECKO_IDS) + "&vs_currencies=usd")
    data = _http_get(url, timeout=6.0)
    if not isinstance(data, dict):
        return
    eth = (data.get("ethereum") or {}).get("usd")
    if not eth:
        return
    discs = {}
    # Only stETH is a rebasing token that should price 1:1 with ETH; the others
    # have accrued staking premium baked in. We track all three but average
    # over stETH only (the cleanest signal). The strategy can also act on
    # rETH / cbETH if the WIDENING from baseline is what matters (TODO).
    mapping = {"staked-ether": "stETH", "rocket-pool-eth": "rETH",
               "coinbase-wrapped-staked-eth": "cbETH"}
    for gid, sym in mapping.items():
        px = (data.get(gid) or {}).get("usd")
        if not px:
            continue
        discs[sym] = (px / eth) - 1.0
    if "stETH" not in discs:
        return
    avg = discs["stETH"]   # primary signal
    with _LOCK:
        _LST_LATEST["data"] = {**discs, "avg_discount": avg}
        _LST_LATEST["ts"] = now

def get_lst_discounts() -> Optional[dict]:
    with _LOCK:
        d = _LST_LATEST.get("data")
        if not d:
            return None
        return {**d, "age_sec": time.time() - _LST_LATEST.get("ts", 0)}


# ──────────────────────────────────────────────────────────────────────
# 7) Pyth vs HL mark — oracle-lag
# ──────────────────────────────────────────────────────────────────────
# Pyth price feed IDs (hex). https://www.pyth.network/developers/price-feed-ids
PYTH_FEED_IDS = {
    "BTC": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH": "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "AVAX": "93da3352f9f1d105fdfe4971cfa80e9dd777bfc5d0f683ebb6e1294b92137bb7",
    "DOGE": "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "LINK": "8ac0c70fff57e9aefdf5edf44b51d62c2d433653cbb2cf5cc06bb115af04d221",
    "ARB": "3fa4252848f9f0a1480be62745a4629d9eb1322aebab8a791e344b3b9c1adcf5",
    "OP": "385f64d993f7b77d8182ed5003d97c60aa3361f3cecfe711544d2d59165e9bdf",
    "SUI": "23d7315113f5b1d3ba7a83604c44b94d79f4fd69af77f804fc7f920a6dc65744",
    "MATIC": "5de33a9112c2b700b8d30b8a3402c103578ccfa2765696471cc672bd5cf6ac52",
    "BNB": "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "ADA": "2a01deaec9e51a579277b34b122399984d0bbf57e2458a7e42fecd2829867a0d",
}
_PYTH_CACHE: Dict[str, dict] = {}
_HL_MARK_CACHE: Dict[str, float] = {}
_HL_MARK_TS = 0

def _refresh_hl_marks():
    global _HL_MARK_TS
    res = _http_post("https://api.hyperliquid.xyz/info", {"type": "allMids"}, timeout=4.0)
    if not isinstance(res, dict):
        return
    with _LOCK:
        for k, v in res.items():
            try:
                _HL_MARK_CACHE[k] = float(v)
            except Exception:
                pass
        _HL_MARK_TS = time.time()

def _refresh_pyth():
    """Fetch Pyth prices for all tracked symbols (batched 4 per call, Hermes limit).
    Hermes v2 returns 404 when > ~5 ids per query."""
    ids_all = list(PYTH_FEED_IDS.items())   # [(coin, feed_id)]
    if not ids_all:
        return
    feed_to_coin = {v.lower(): k for k, v in PYTH_FEED_IDS.items()}
    now_ms = int(time.time() * 1000)
    BATCH = 4
    for i in range(0, len(ids_all), BATCH):
        chunk = ids_all[i:i+BATCH]
        q = "&".join(f"ids[]=0x{fid}" for (_, fid) in chunk)
        url = f"https://hermes.pyth.network/v2/updates/price/latest?{q}&parsed=true"
        res = _http_get(url, timeout=4.0)
        if not isinstance(res, dict):
            continue
        parsed = res.get("parsed") or []
        with _LOCK:
            for item in parsed:
                fid = (item.get("id") or "").lower()
                coin = feed_to_coin.get(fid)
                if not coin:
                    continue
                p = item.get("price") or {}
                try:
                    price_int = int(p.get("price"))
                    expo = int(p.get("expo"))
                    pub_time = int(p.get("publish_time"))
                except Exception:
                    continue
                real_price = price_int * (10 ** expo)
                age_ms = max(0, now_ms - pub_time * 1000)
                _PYTH_CACHE[coin] = {"price": real_price, "publish_time": pub_time,
                                      "age_ms": age_ms, "fetched_ts": time.time()}
        time.sleep(0.2)

def get_pyth_hl_basis(coin: str) -> Optional[dict]:
    with _LOCK:
        p = _PYTH_CACHE.get(coin)
        mark = _HL_MARK_CACHE.get(coin)
        if not p or mark is None or mark <= 0:
            return None
        pyth_px = float(p["price"])
        if pyth_px <= 0:
            return None
        basis_bps = ((pyth_px - mark) / mark) * 10000.0
        # age: take max of pyth publish age + HL mark age
        hl_age_ms = int((time.time() - _HL_MARK_TS) * 1000) if _HL_MARK_TS > 0 else 99999
        age_ms = max(p.get("age_ms", 99999), hl_age_ms)
        return {
            "pyth_price": pyth_px, "hl_mark": mark,
            "basis_bps": basis_bps, "age_ms": age_ms,
        }


# ──────────────────────────────────────────────────────────────────────
# Background refresh threads
# ──────────────────────────────────────────────────────────────────────
def _loop(name: str, fn, interval: float):
    while True:
        try:
            fn()
        except Exception as e:
            print(f"[novel_data:{name}] error: {e}", flush=True)
        time.sleep(interval)

_STARTED = False
def start_all():
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    schedule = [
        ("supply", _refresh_supply, 3600.0),       # 1h
        ("hlp", _refresh_hlp, 300.0),               # 5min
        ("whales", _refresh_whale_stress, 90.0),    # 90s
        ("mev", _refresh_mev, 30.0),                # 30s
        ("listings", _refresh_listings, 120.0),     # 2min
        ("lst", _refresh_lst, 600.0),               # 10min
        ("pyth", _refresh_pyth, 5.0),               # 5s
        ("hl_marks", _refresh_hl_marks, 4.0),       # 4s
    ]
    for name, fn, interval in schedule:
        t = threading.Thread(target=_loop, args=(name, fn, interval),
                              daemon=True, name=f"novel_{name}")
        t.start()
    print(f"[novel_data] started {len(schedule)} background refresh threads",
          flush=True)
