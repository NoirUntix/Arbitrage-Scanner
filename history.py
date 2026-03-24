"""
History Logger — v3
- Coin IDs: stable COIN-XXXXX per symbol+exchange pair, persisted to disk
- Pair status: normal | unstable | banned — persisted, supports restore to normal
- Pinned pairs: immune to auto-unstable, always shown in main view
- Log files: alert_logs.txt, banned_coins.log, unstable_coins.log, client_actions.log
- Analytics: excludes banned/unstable pairs; qualified = gross% >= MIN_GROSS (default 0.80%)
- Alert records include coin_id + pair_key for reliable cross-referencing
"""

import json, os, time, atexit, threading
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")

LOG_FILE     = os.path.join(BASE_DIR, "alert_logs.txt")
BANNED_LOG   = os.path.join(BASE_DIR, "banned_coins.log")
UNSTABLE_LOG = os.path.join(BASE_DIR, "unstable_coins.log")
CLIENT_LOG   = os.path.join(BASE_DIR, "client_actions.log")

COIN_ID_FILE     = os.path.join(DATA_DIR, "coin_ids.json")
PAIR_STATUS_FILE = os.path.join(DATA_DIR, "pair_status.json")
AN_EXCL_FILE     = os.path.join(DATA_DIR, "analytics_excluded.json")
PINNED_FILE      = os.path.join(DATA_DIR, "pinned_pairs.json")

DEDUP_TTL = 600    # 10-min cooldown after alert ends
GRACE_TTL = 8      # seconds grace before marking ended
MIN_GROSS = 0.80   # qualified lower bound — alerts below this don't count toward gross
# MAX_GROSS is intentionally uncapped (all alerts above MIN_GROSS are qualified)

os.makedirs(DATA_DIR, exist_ok=True)

# ── Thread safety ─────────────────────────────────────────────────────────────
_alerts_lock = threading.RLock()   # protects _active_alerts and _dedup

# ── Alert session state ───────────────────────────────────────────────────────
_dedup:         dict = {}
_active_alerts: dict = {}
_session_start       = time.time()
_session_alerts      = 0
_alert_counter       = 0

# ── Coin ID state (loaded from disk) ─────────────────────────────────────────
_coin_ids: dict = {}   # canon_pair_key -> "COIN-XXXXX"
_coin_id_ctr    = 0

# ── Pair status state (loaded from disk) ─────────────────────────────────────
_pair_status: dict = {}   # coin_id -> {status, removal_type, removed_at, ...}
_excluded_ids: set = set()  # coin_ids with status != 'normal'

# ── Pinned pairs (immune to auto-unstable) ────────────────────────────────────
_pinned_keys: set = set()  # canon_pair_keys that are pinned

# ── Analytics symbol exclusions ───────────────────────────────────────────────
_an_excl_symbols: set = set()


# ═══════════════════════════ PERSISTENCE ═════════════════════════════════════

def _load_json_safe(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default

def _save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        pass

def _append_file(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _load_coin_ids():
    global _coin_ids, _coin_id_ctr
    d = _load_json_safe(COIN_ID_FILE, {"ids": {}, "counter": 0})
    _coin_ids    = d.get("ids", {})
    _coin_id_ctr = d.get("counter", 0)

def _save_coin_ids():
    _save_json(COIN_ID_FILE, {"ids": _coin_ids, "counter": _coin_id_ctr})

def _load_pair_status():
    global _pair_status, _excluded_ids
    _pair_status  = _load_json_safe(PAIR_STATUS_FILE, {})
    _excluded_ids = {cid for cid, v in _pair_status.items()
                     if v.get("status") in ("unstable", "banned")}

def _save_pair_status():
    _save_json(PAIR_STATUS_FILE, _pair_status)

def _load_an_exclusions():
    global _an_excl_symbols
    _an_excl_symbols = set(_load_json_safe(AN_EXCL_FILE, []))

def _save_an_exclusions():
    _save_json(AN_EXCL_FILE, list(_an_excl_symbols))

def _load_pinned():
    global _pinned_keys
    _pinned_keys = set(_load_json_safe(PINNED_FILE, []))

def _save_pinned():
    _save_json(PINNED_FILE, list(_pinned_keys))

# Load everything on import
_load_coin_ids()
_load_pair_status()
_load_an_exclusions()
_load_pinned()


# ═══════════════════════════ COIN IDs ════════════════════════════════════════

def _canon_pair_key(symbol: str, ex_a: str, ex_b: str) -> str:
    """Canonical pair key: symbol + alphabetical exchange order."""
    a, b = sorted([ex_a, ex_b])
    return f"{symbol}|{a}|{b}"

def get_or_create_coin_id(symbol: str, ex_a: str, ex_b: str) -> str:
    global _coin_id_ctr
    key = _canon_pair_key(symbol, ex_a, ex_b)
    if key not in _coin_ids:
        _coin_id_ctr += 1
        _coin_ids[key] = f"COIN-{_coin_id_ctr:05d}"
        _save_coin_ids()
    return _coin_ids[key]


# ═══════════════════════════ PAIR STATUS ══════════════════════════════════════

_STATUS_PRIORITY = {"normal": 0, "unstable": 1, "banned": 2}

def mark_pair_status(symbol: str, min_ex: str, max_ex: str,
                     status: str, removal_type: str,
                     spread_pct=None, reason: str = "") -> tuple:
    """
    Mark pair as unstable or banned. Status is NEVER downgraded.
    Returns (coin_id, changed: bool).
    removal_type: 'system' | 'manual'
    """
    coin_id  = get_or_create_coin_id(symbol, min_ex, max_ex)
    current  = _pair_status.get(coin_id, {}).get("status", "normal")

    # No downgrade, no re-write if already at same or higher status
    if _STATUS_PRIORITY.get(current, 0) >= _STATUS_PRIORITY.get(status, 0):
        return coin_id, False

    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pair_key = _canon_pair_key(symbol, min_ex, max_ex)

    _pair_status[coin_id] = {
        "coin_id":               coin_id,
        "status":                status,
        "removal_type":          removal_type,
        "removed_at":            ts,
        "reason":                reason,
        "symbol":                symbol,
        "min_exchange":          min_ex,
        "max_exchange":          max_ex,
        "spread_pct_at_removal": spread_pct,
        "pair_key":              pair_key,
    }
    _excluded_ids.add(coin_id)
    _save_pair_status()

    sp_str = f"{spread_pct:.4f}%" if spread_pct is not None else "n/a"
    log_line = (f"[{ts} UTC] {status.upper()} {coin_id} "
                f"{symbol} {min_ex}↔{max_ex} spread={sp_str} by={removal_type}"
                + (f" — {reason}" if reason else ""))

    if status == "banned":
        _append_file(BANNED_LOG, log_line)
    elif status == "unstable":
        _append_file(UNSTABLE_LOG, log_line)

    return coin_id, True


def get_pair_statuses() -> dict:
    return dict(_pair_status)


def restore_pair_to_normal(symbol: str, min_ex: str, max_ex: str) -> tuple:
    """
    Remove pair from unstable/banned status, restoring it to normal.
    Returns (coin_id, restored: bool).
    """
    coin_id  = get_or_create_coin_id(symbol, min_ex, max_ex)
    if coin_id not in _pair_status:
        return coin_id, False
    ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pair_key = _canon_pair_key(symbol, min_ex, max_ex)
    del _pair_status[coin_id]
    _excluded_ids.discard(coin_id)
    _save_pair_status()
    _append_file(UNSTABLE_LOG,
                 f"[{ts} UTC] RESTORED {coin_id} {symbol} {min_ex}↔{max_ex} — user restore")
    return coin_id, True


def get_coin_id_for_pair(symbol: str, ex_a: str, ex_b: str) -> str:
    return get_or_create_coin_id(symbol, ex_a, ex_b)


# ═══════════════════════════ PINNED PAIRS ════════════════════════════════════

def pin_pair(symbol: str, min_ex: str, max_ex: str) -> str:
    """Pin a pair so it is immune to auto-unstable detection."""
    key = _canon_pair_key(symbol, min_ex, max_ex)
    _pinned_keys.add(key)
    _save_pinned()
    return key

def unpin_pair(symbol: str, min_ex: str, max_ex: str) -> str:
    """Remove pin from a pair."""
    key = _canon_pair_key(symbol, min_ex, max_ex)
    _pinned_keys.discard(key)
    _save_pinned()
    return key

def is_pair_pinned(symbol: str, min_ex: str, max_ex: str) -> bool:
    return _canon_pair_key(symbol, min_ex, max_ex) in _pinned_keys

def get_pinned_pairs() -> list:
    return sorted(_pinned_keys)


# ═══════════════════════════ ANALYTICS EXCLUSIONS ════════════════════════════

def exclude_analytics_symbol(symbol: str):
    _an_excl_symbols.add(symbol)
    _save_an_exclusions()

def unexclude_analytics_symbol(symbol: str):
    _an_excl_symbols.discard(symbol)
    _save_an_exclusions()

def get_analytics_excluded() -> list:
    return sorted(_an_excl_symbols)


# ═══════════════════════════ CLIENT ACTION LOG ════════════════════════════════

def log_client_action(action: str, target: str, details: str = ""):
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} UTC] {action:<22} {target}"
    if details:
        line += f" — {details}"
    _append_file(CLIENT_LOG, line)


# ═══════════════════════════ LOG FILE READING ══════════════════════════════════

_LOG_PATHS = {
    "alerts":   LOG_FILE,
    "banned":   BANNED_LOG,
    "unstable": UNSTABLE_LOG,
    "client":   CLIENT_LOG,
}

def get_log_tail(log_type: str, lines: int = 100) -> str:
    path = _LOG_PATHS.get(log_type, "")
    if not path or not os.path.exists(path):
        return f"[{log_type}.log — empty or not found]"
    try:
        with open(path, encoding="utf-8") as f:
            tail = f.readlines()[-lines:]
        return "".join(tail).strip() or "[empty]"
    except Exception as e:
        return f"[read error: {e}]"

def get_all_logs_summary() -> dict:
    return {lt: get_log_tail(lt, 50) for lt in _LOG_PATHS}


# ═══════════════════════════ ALERT IDs & SESSION ══════════════════════════════

def _next_id() -> str:
    global _alert_counter
    _alert_counter += 1
    return f"ALT-{_alert_counter:05d}"

def _alert_dedup_key(sym, atype, ex_a, ex_b):
    return f"{sym}|{atype}|{min(ex_a, ex_b)}|{max(ex_a, ex_b)}"

def _today_path():
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(DATA_DIR, f"history_{d}.json")

def _load_history_file(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def _save_history_file(path, records):
    with open(path, "w") as f:
        json.dump(records, f)

def _write_session_header():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _append_file(LOG_FILE, f"\n{'='*72}\n  SESSION STARTED: {ts}\n{'='*72}")

def _write_session_footer():
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    dur = round(time.time() - _session_start)
    h, m = divmod(dur // 60, 60); s = dur % 60
    _append_file(LOG_FILE,
        f"\n{'-'*72}\n  SESSION ENDED: {ts}  |  "
        f"Duration: {h:02d}:{m:02d}:{s:02d}  |  Alerts: {_session_alerts}\n{'-'*72}\n")

_write_session_header()
atexit.register(_write_session_footer)


# ═══════════════════════════ TRUST UTIL ══════════════════════════════════════

def _worst_trust(trust_map, ex_a, ex_b):
    ta, tb = trust_map.get(ex_a, "medium"), trust_map.get(ex_b, "medium")
    if "low"    in (ta, tb): return "low"
    if "medium" in (ta, tb): return "medium"
    return "high"


# ═══════════════════════════ RECORD ALERTS ════════════════════════════════════

def record_alerts(scan_data: dict):
    global _session_alerts
    path        = _today_path()
    records     = _load_history_file(path)
    now         = time.time()
    new_records = []
    seen_keys   = set()

    with _alerts_lock:
        for sym, d in scan_data.items():
            a = d.get("analysis", {})

            # ── Spread ──────────────────────────────────────────────────────────
            if a.get("spread_alert"):
                buy_ex     = a.get("min_exchange", "?")
                sell_ex    = a.get("max_exchange", "?")
                dedup_key  = _alert_dedup_key(sym, "spread", buy_ex, sell_ex)
                spread_pct = round(a.get("spread_pct", 0), 5)
                pair_key   = _canon_pair_key(sym, buy_ex, sell_ex)
                coin_id    = get_or_create_coin_id(sym, buy_ex, sell_ex)
                seen_keys.add(dedup_key)

                # Skip recording if this pair is already banned or unstable
                coin_status = _pair_status.get(coin_id, {}).get("status", "normal")
                if coin_status in ("banned", "unstable"):
                    continue

                if dedup_key in _active_alerts and not _active_alerts[dedup_key].get("ended"):
                    al = _active_alerts[dedup_key]
                    al["last_seen_ts"] = now
                    al["max_pct"]      = max(al["max_pct"], spread_pct)
                    al["min_pct"]      = min(al["min_pct"], spread_pct)
                    al["current_pct"]  = spread_pct
                    al["buy_price"]    = a.get("min_price")
                    al["sell_price"]   = a.get("max_price")
                else:
                    last_end = _dedup.get(dedup_key, 0)
                    if now - last_end >= DEDUP_TTL:
                        alert_id = _next_id()
                        trust    = _worst_trust(a.get("trust_map", {}), buy_ex, sell_ex)
                        _active_alerts[dedup_key] = {
                            "id":            alert_id, "coin_id": coin_id,
                            "symbol":        sym,      "type":    "spread",
                            "key":           dedup_key,"pair_key":pair_key,
                            "start_ts":      now,      "last_seen_ts": now,
                            "min_pct":       spread_pct, "max_pct": spread_pct,
                            "current_pct":   spread_pct,
                            "buy_exchange":  buy_ex,   "sell_exchange": sell_ex,
                            "buy_price":     a.get("min_price"),
                            "sell_price":    a.get("max_price"),
                            "trust_level":   trust,    "ended": False, "end_ts": None,
                        }
                        new_records.append({
                            "ts": now, "alert_id": alert_id, "coin_id": coin_id,
                            "pair_key": pair_key, "symbol": sym, "type": "spread",
                            "spread_pct": spread_pct,
                            "buy_exchange": buy_ex,   "sell_exchange": sell_ex,
                            "buy_price":    a.get("min_price"),
                            "sell_price":   a.get("max_price"),
                            "potential_pct": spread_pct, "funding_diff": None,
                            "short_exchange": None, "long_exchange": None,
                            "annual_diff_pct": None, "trust_level": trust,
                        })
                        _session_alerts += 1
                        ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        _append_file(LOG_FILE,
                            f"[{ts_s} UTC] [{alert_id}] [{coin_id}] NEW SPREAD  {sym:<6} "
                            f"+{spread_pct:.4f}%  LONG {buy_ex} @ ${a.get('min_price',0):.4f}  "
                            f"SHORT {sell_ex} @ ${a.get('max_price',0):.4f}  trust={trust}")

            # ── Funding ─────────────────────────────────────────────────────────
            for opp in a.get("funding_opportunities", []):
                if not opp.get("alert"):
                    continue
                short_ex  = opp.get("short_exchange", "?")
                long_ex   = opp.get("long_exchange",  "?")
                dedup_key = _alert_dedup_key(sym, "funding", short_ex, long_ex)
                diff_pct  = round(opp.get("diff_pct", 0), 5)
                pair_key  = _canon_pair_key(sym, short_ex, long_ex)
                coin_id   = get_or_create_coin_id(sym, short_ex, long_ex)
                seen_keys.add(dedup_key)

                # Skip recording if this pair is already banned or unstable
                coin_status = _pair_status.get(coin_id, {}).get("status", "normal")
                if coin_status in ("banned", "unstable"):
                    continue

                if dedup_key in _active_alerts and not _active_alerts[dedup_key].get("ended"):
                    al = _active_alerts[dedup_key]
                    al["last_seen_ts"] = now
                    al["max_pct"]      = max(al["max_pct"], diff_pct)
                    al["min_pct"]      = min(al["min_pct"], diff_pct)
                    al["current_pct"]  = diff_pct
                else:
                    last_end = _dedup.get(dedup_key, 0)
                    if now - last_end >= DEDUP_TTL:
                        annual   = round(diff_pct * (365 * 24 / 8), 2)
                        alert_id = _next_id()
                        trust    = opp.get("trust_level", "medium")
                        _active_alerts[dedup_key] = {
                            "id":             alert_id, "coin_id":  coin_id,
                            "symbol":         sym,      "type":     "funding",
                            "key":            dedup_key,"pair_key": pair_key,
                            "start_ts":       now,      "last_seen_ts": now,
                            "min_pct":        diff_pct, "max_pct": diff_pct,
                            "current_pct":    diff_pct,
                            "short_exchange": short_ex, "long_exchange": long_ex,
                            "annual_diff_pct": annual,  "trust_level": trust,
                            "ended": False, "end_ts": None,
                        }
                        new_records.append({
                            "ts": now, "alert_id": alert_id, "coin_id": coin_id,
                            "pair_key": pair_key, "symbol": sym, "type": "funding",
                            "spread_pct": None,
                            "buy_exchange": None, "sell_exchange": None,
                            "buy_price": None,    "sell_price": None,
                            "potential_pct": diff_pct, "funding_diff": diff_pct,
                            "short_exchange": short_ex, "long_exchange": long_ex,
                            "annual_diff_pct": annual, "trust_level": trust,
                        })
                        _session_alerts += 1
                        ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        _append_file(LOG_FILE,
                            f"[{ts_s} UTC] [{alert_id}] [{coin_id}] NEW FUNDING {sym:<6} "
                            f"+{diff_pct:.4f}%/8h ({annual:.1f}% annual)  "
                            f"SHORT {short_ex}  LONG {long_ex}  trust={trust}")

        # ── End stale alerts ─────────────────────────────────────────────────────
        for key, al in list(_active_alerts.items()):
            if al.get("ended"):
                if al.get("end_ts") and now - al["end_ts"] > 7200:
                    del _active_alerts[key]
                continue
            if key not in seen_keys and now - al["last_seen_ts"] > GRACE_TTL:
                al["ended"]  = True
                al["end_ts"] = now
                _dedup[key]  = now
                dur = round(now - al["start_ts"])
                ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                _append_file(LOG_FILE,
                    f"[{ts_s} UTC] [{al['id']}] [{al.get('coin_id','?')}] ENDED   "
                    f"{al['symbol']:<6} {al['type']:<8} duration={dur}s  "
                    f"max={al['max_pct']:.4f}%  min={al['min_pct']:.4f}%  "
                    f"range={al['max_pct']-al['min_pct']:.4f}%")

    if new_records:
        records.extend(new_records)
        _save_history_file(path, records)


# ═══════════════════════════ GET ACTIVE ════════════════════════════════════════

def get_active_alerts():
    now = time.time()
    active, unstable = [], []
    with _alerts_lock:
        for key, al in _active_alerts.items():
            if al.get("ended"):
                continue
            age  = now - al["start_ts"]
            data = {**al, "age_s": round(age),
                    "range_pct": round(al["max_pct"] - al["min_pct"], 5)}
            (unstable if age > 900 else active).append(data)
    return active, unstable


# ═══════════════════════════ LOAD RANGE ════════════════════════════════════════

def load_range(days: int) -> list:
    today = datetime.now(timezone.utc).date()
    all_r = []
    for i in range(days):
        d    = today - timedelta(days=i)
        path = os.path.join(DATA_DIR, f"history_{d}.json")
        all_r.extend(_load_history_file(path))
    return all_r


# ═══════════════════════════ COMPUTE ANALYTICS ════════════════════════════════

def _infer_pair_key(r: dict) -> str:
    """Derive canonical pair_key from older records that lack the field."""
    sym = r.get("symbol", "?")
    if r.get("type") == "spread":
        a, b = r.get("buy_exchange", "?"), r.get("sell_exchange", "?")
    else:
        a, b = r.get("short_exchange", "?"), r.get("long_exchange", "?")
    return _canon_pair_key(sym, a, b)

def compute_analytics(records: list,
                      coin_filter: str = None,
                      type_filter: str = None) -> dict:
    """
    Returns analytics dict.
    Excluded: pairs whose coin_id is in _excluded_ids (status unstable/banned).
    Also excludes symbols in _an_excl_symbols.
    Qualified: potential_pct >= MIN_GROSS (default 0.80%), no upper cap.
    """
    # Build excluded pair_keys lookup
    excl_pair_keys = {
        info["pair_key"]
        for info in _pair_status.values()
        if info.get("status") in ("unstable", "banned") and "pair_key" in info
    }

    # Attach status metadata to every record (mutates a copy)
    records = [dict(r) for r in records]
    for r in records:
        pk = r.get("pair_key") or _infer_pair_key(r)
        r["pair_key"] = pk  # backfill if missing
        if pk in excl_pair_keys:
            info = next((v for v in _pair_status.values()
                         if v.get("pair_key") == pk), {})
            r["_pair_status"]  = info.get("status", "normal")
            r["_removal_type"] = info.get("removal_type", "")
        else:
            r["_pair_status"]  = "normal"
            r["_removal_type"] = ""

    # Filters
    if coin_filter:
        records = [r for r in records if r.get("symbol") == coin_filter]
    if type_filter in ("spread", "funding"):
        records = [r for r in records if r.get("type") == type_filter]
    if _an_excl_symbols:
        records = [r for r in records if r.get("symbol") not in _an_excl_symbols]

    if not records:
        return _empty_analytics()

    active   = [r for r in records if r["_pair_status"] == "normal"]
    excluded = [r for r in records if r["_pair_status"] != "normal"]

    qualified    = [r for r in active if (r.get("potential_pct") or 0) >= MIN_GROSS]
    total_pot    = sum(r.get("potential_pct", 0) or 0 for r in qualified)

    # By symbol — include excluded rows but tag them
    by_symbol: dict = {}
    for r in records:
        sym = r["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {
                "count": 0, "total_pct": 0, "best_pct": 0, "qualified": 0,
                "excluded": 0,
                "pair_status":  r["_pair_status"],
                "removal_type": r["_removal_type"],
            }
        p = r.get("potential_pct", 0) or 0
        by_symbol[sym]["count"] += 1
        if r["_pair_status"] != "normal":
            by_symbol[sym]["excluded"]     += 1
            by_symbol[sym]["pair_status"]   = r["_pair_status"]
            by_symbol[sym]["removal_type"]  = r["_removal_type"]
        else:
            if p >= MIN_GROSS:
                by_symbol[sym]["total_pct"] = round(by_symbol[sym]["total_pct"] + p, 5)
                by_symbol[sym]["qualified"] += 1
            by_symbol[sym]["best_pct"] = round(max(by_symbol[sym]["best_pct"], p), 5)

    by_type = {"spread": 0, "funding": 0}
    for r in active:
        by_type[r.get("type", "spread")] += 1

    by_pair: dict = {}
    for r in active:
        k = (f"{r.get('buy_exchange','?')} → {r.get('sell_exchange','?')}"
             if r["type"] == "spread"
             else f"{r.get('long_exchange','?')} / {r.get('short_exchange','?')}")
        by_pair.setdefault(k, {"count": 0, "total_pct": 0})["count"] += 1
        p = r.get("potential_pct", 0) or 0
        if p >= MIN_GROSS:
            by_pair[k]["total_pct"] = round(by_pair[k]["total_pct"] + p, 5)

    by_trust: dict = {}
    for r in active:
        t = r.get("trust_level", "medium")
        by_trust[t] = by_trust.get(t, 0) + 1

    buckets: dict = {}
    for r in active:
        hour = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:00")
        buckets.setdefault(hour, {"time": hour, "count": 0, "potential": 0})
        buckets[hour]["count"] += 1
        p = r.get("potential_pct", 0) or 0
        if p >= MIN_GROSS:
            buckets[hour]["potential"] = round(buckets[hour]["potential"] + p, 5)

    timeline = sorted(buckets.values(), key=lambda x: x["time"])
    best     = max(active, key=lambda r: r.get("potential_pct", 0) or 0) if active else None

    return {
        "total_alerts":        len(records),
        "active_alerts":       len(active),
        "excluded_alerts":     len(excluded),
        "qualified_alerts":    len(qualified),
        "total_potential_pct": round(total_pot, 4),
        "avg_potential_pct":   round(total_pot / len(qualified), 4) if qualified else 0,
        "min_gross_threshold": MIN_GROSS,
        "by_symbol":    dict(sorted(by_symbol.items(),
                             key=lambda x: x[1]["total_pct"], reverse=True)),
        "by_type":             by_type,
        "by_trust":            by_trust,
        "by_exchange_pair":    dict(sorted(by_pair.items(),
                                   key=lambda x: x[1]["count"], reverse=True)),
        "timeline":            timeline,
        "best_opportunity":    best,
        "records":             sorted(records, key=lambda r: r["ts"], reverse=True),
        "analytics_excluded":  list(_an_excl_symbols),
    }

def _empty_analytics() -> dict:
    return {
        "total_alerts": 0, "active_alerts": 0, "excluded_alerts": 0,
        "qualified_alerts": 0, "total_potential_pct": 0, "avg_potential_pct": 0,
        "min_gross_threshold": MIN_GROSS,
        "by_symbol": {}, "by_type": {"spread": 0, "funding": 0}, "by_trust": {},
        "by_exchange_pair": {}, "timeline": [], "best_opportunity": None,
        "records": [], "analytics_excluded": list(_an_excl_symbols),
    }
