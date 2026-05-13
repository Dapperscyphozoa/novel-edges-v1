"""Per-strategy SQLite DB backup to GitHub. Bundle-aware.

Differs from engine/db_backup.py: this version takes engine_name as
an argument on every call so the bundle can back up 7 different DBs.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Optional, Dict


_GITHUB_OWNER = "Dapperscyphozoa"
_GITHUB_REPO = "multica"
_BRANCH = "main"
_DEBOUNCE_SECONDS = 30
_MAX_DB_BYTES = 50 * 1024 * 1024

_lock = threading.RLock()
_pending: Dict[str, str] = {}   # engine_name → db_path needing upload
_sha_cache: Dict[str, str] = {}
_last_hash: Dict[str, str] = {}
_last_upload_ts: Dict[str, float] = {}
_thread_started = False


def _api_url(engine_name: str) -> str:
    return (f"https://api.github.com/repos/{_GITHUB_OWNER}/{_GITHUB_REPO}"
             f"/contents/engine_state/{engine_name}/state.db")


def _hdrs() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "bundle-dbbackup/1.0"}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok: h["Authorization"] = f"Bearer {tok}"
    return h


def restore_on_boot(engine_name: str, db_path: str) -> bool:
    """Download DB from engine_state/{engine_name}/state.db on multica repo."""
    # Skip if local DB already has data
    if os.path.exists(db_path) and os.path.getsize(db_path) > 1024:
        try:
            req = urllib.request.Request(f"{_api_url(engine_name)}?ref={_BRANCH}", headers=_hdrs())
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read())
            _sha_cache[engine_name] = body.get("sha", "")
        except Exception: pass
        return False

    try:
        req = urllib.request.Request(f"{_api_url(engine_name)}?ref={_BRANCH}", headers=_hdrs())
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
        _sha_cache[engine_name] = body.get("sha", "")
        if body.get("encoding") == "base64" and body.get("content"):
            data = base64.b64decode(body["content"].replace("\n", ""))
        elif body.get("download_url"):
            with urllib.request.urlopen(
                urllib.request.Request(body["download_url"], headers=_hdrs()),
                timeout=60) as r2:
                data = r2.read()
        else:
            return False
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with open(db_path, "wb") as f: f.write(data)
        print(f"[bundle_db_backup] {engine_name}: restored {len(data)} bytes from GitHub",
              flush=True)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[bundle_db_backup] {engine_name}: no prior backup — starting fresh",
                  flush=True)
        else:
            print(f"[bundle_db_backup] {engine_name}: restore HTTP {e.code}", flush=True)
        return False
    except Exception as e:
        print(f"[bundle_db_backup] {engine_name}: restore err: {e}", flush=True)
        return False


def schedule_backup(engine_name: str, db_path: str):
    """Mark a strategy's DB as dirty. Background thread will upload."""
    with _lock:
        _pending[engine_name] = db_path


def _upload_one(engine_name: str, db_path: str) -> bool:
    """Upload one strategy's DB to GitHub."""
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0: return False
    if os.path.getsize(db_path) > _MAX_DB_BYTES: return False
    try:
        with open(db_path, "rb") as f: data = f.read()
        h = hashlib.sha256(data).hexdigest()
        if _last_hash.get(engine_name) == h: return True   # no change

        payload = {
            "message": f"{engine_name}: state @ {int(time.time())}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": _BRANCH,
        }
        if _sha_cache.get(engine_name):
            payload["sha"] = _sha_cache[engine_name]
        req = urllib.request.Request(_api_url(engine_name), method="PUT",
                                       data=json.dumps(payload).encode(),
                                       headers=_hdrs())
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read())
        _sha_cache[engine_name] = body.get("content", {}).get("sha", "")
        _last_hash[engine_name] = h
        return True
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # sha conflict — refresh and retry
            try:
                req = urllib.request.Request(f"{_api_url(engine_name)}?ref={_BRANCH}",
                                               headers=_hdrs())
                with urllib.request.urlopen(req, timeout=15) as r:
                    info = json.loads(r.read())
                _sha_cache[engine_name] = info.get("sha", "")
                payload["sha"] = _sha_cache[engine_name]
                req = urllib.request.Request(_api_url(engine_name), method="PUT",
                                               data=json.dumps(payload).encode(),
                                               headers=_hdrs())
                with urllib.request.urlopen(req, timeout=60) as r:
                    body2 = json.loads(r.read())
                _sha_cache[engine_name] = body2.get("content", {}).get("sha", "")
                _last_hash[engine_name] = h
                return True
            except Exception: return False
        print(f"[bundle_db_backup] {engine_name}: upload HTTP {e.code}", flush=True)
        return False
    except Exception as e:
        print(f"[bundle_db_backup] {engine_name}: upload err: {e}", flush=True)
        return False


def _loop():
    while True:
        try:
            time.sleep(_DEBOUNCE_SECONDS)
            now = time.time()
            with _lock:
                pending_copy = dict(_pending)
                _pending.clear()
            for engine_name, db_path in pending_copy.items():
                last = _last_upload_ts.get(engine_name, 0)
                if now - last >= _DEBOUNCE_SECONDS:
                    _upload_one(engine_name, db_path)
                    _last_upload_ts[engine_name] = now
        except Exception as e:
            print(f"[bundle_db_backup] loop err: {e}", flush=True)
            time.sleep(60)


def start_background_thread():
    global _thread_started
    if _thread_started: return
    _thread_started = True
    t = threading.Thread(target=_loop, daemon=True, name="bundle_db_backup")
    t.start()
    print(f"[bundle_db_backup] background thread started", flush=True)
