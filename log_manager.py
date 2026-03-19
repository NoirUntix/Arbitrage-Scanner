"""
Log Manager — persistent state for coin IDs, banned/unstable pairs, client actions.

Persistent files (data/):
  coin_ids.json           — COIN-XXXXX permanent IDs per symbol
  banned_pairs.json       — banned pairs with their excluded alert IDs
  unstable_pairs.json     — system/manual unstable pairs with excluded alert IDs
  analytics_excluded.json — coins excluded from analytics only (not from live view)

Log files (root/):
  banned_pairs.log        — full audit trail of ban events
  unstable_pairs.log      — full audit trail of unstable events
  client_actions.log      — every user action (tab switches, excludes, bans, etc.)
"""

import json, os
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ── Persistent JSON state ─────────────────────────────────────────────────────
COIN_IDS_FILE    = os.path.join(DATA_DIR, "coin_ids.json")
BANNED_FILE      = os.path.join(DATA_DIR, "banned_pairs.json")
UNSTABLE_FILE    = os.path.join(DATA_DIR, "unstable_pairs.json")
AN_EXCL_FILE     = os.path.join(DATA_DIR, "analytics_excluded.json")

# ── Log files ────────────────────────────────────────────────────────────────
BANNED_LOG   = os.path.join(BASE_DIR, "banned_pairs.log")
UNSTABLE_LOG = os.path.join(BASE_DIR, "unstable_pairs.log")
CLIENT_LOG   = os.path.join(BASE_DIR, "client_actions.log")

# ── In-memory state ──────────────────────────────────────────────────────────
_coin_ids: dict     = {}   # symbol -> "COIN-XXXXX"
_coin_counter: int  = 0
_banned: dict       = {}   # key -> record dict
_unstable: dict     = {}   # key -> record dict
_an_excl: dict      = {"symbols": [], "alert_ids": []}


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _log(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _tail(path, n=40):
    if not os.path.exists(path):
        return "(empty)"
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:]).strip() or "(empty)"
    except Exception:
        return "(error reading)"


# ── Boot: load all state from disk ────────────────────────────────────────────

def _reload():
    global _coin_ids, _coin_counter, _banned, _unstable, _an_excl
    _coin_ids = _load(COIN_IDS_FILE, {})
    _coin_counter = max(
        (int(v.split("-")[1]) for v in _coin_ids.values() if "-" in v),
        default=0,
    )
    _banned   = _load(BANNED_FILE,   {})
    _unstable = _load(UNSTABLE_FILE, {})
    _an_excl  = _load(AN_EXCL_FILE,  {"symbols": [], "alert_ids": []})


_reload()


# ── Coin IDs ──────────────────────────────────────────────────────────────────

def get_coin_id(symbol: str) -> str:
    """Return permanent COIN-XXXXX id for a symbol, creating one if new."""
    global _coin_counter
    if symbol not in _coin_ids:
        _coin_counter += 1
        _coin_ids[symbol] = f"COIN-{_coin_counter:05d}"
        _save(COIN_IDS_FILE, _coin_ids)
    return _coin_ids[symbol]


def get_all_coin_ids() -> dict:
    return dict(_coin_ids)


# ── Status checks (fast, used by history.py on every alert) ──────────────────

def is_banned(key: str) -> bool:
    return key in _banned


def is_unstable(key: str) -> bool:
    return key in _unstable


# ── Excluded alert IDs (used by compute_analytics to filter records) ──────────

def get_excluded_alert_ids() -> set:
    """Full set of alert IDs that must be excluded from analytics."""
    excl = set(_an_excl.get("alert_ids", []))
    for v in _banned.values():
        excl.update(v.get("alert_ids", []))
    for v in _unstable.values():
        excl.update(v.get("alert_ids", []))
    return excl


def get_excluded_by_reason() -> dict:
    """Returns {alert_id: reason_str} — used for tagging rows in the UI."""
    out = {}
    for v in _banned.values():
        for aid in v.get("alert_ids", []):
            out[aid] = "banned"
    for v in _unstable.values():
        reason = "system_removal" if v.get("by") == "system" else "manual_removal"
        for aid in v.get("alert_ids", []):
            if aid not in out:
                out[aid] = reason
    for aid in _an_excl.get("alert_ids", []):
        if aid not in out:
            out[aid] = "analytics_excluded"
    return out


def get_analytics_excluded_coins() -> list:
    return list(_an_excl.get("symbols", []))


# ── Ban a pair ────────────────────────────────────────────────────────────────

def ban_pair(key: str, symbol: str, min_ex: str, max_ex: str,
             spread_pct: float, min_price: float, max_price: float,
             trust: str, by: str = "user", alert_ids: list = None) -> dict:
    """
    Mark a pair as permanently banned.
    Idempotent — calling twice has no effect.
    Removes from unstable list if present there.
    """
    if key in _banned:
        return _banned[key]

    alert_ids = list(set(alert_ids or []))
    coin_id   = get_coin_id(symbol)

    was_unstable = key in _unstable
    if was_unstable:
        _unstable.pop(key, None)
        _save(UNSTABLE_FILE, _unstable)

    record = {
        "key":          key,
        "coin_id":      coin_id,
        "symbol":       symbol,
        "min_ex":       min_ex,
        "max_ex":       max_ex,
        "spread_pct":   round(spread_pct, 5),
        "min_price":    min_price,
        "max_price":    max_price,
        "trust":        trust,
        "by":           by,
        "alert_ids":    alert_ids,
        "banned_at":    _ts(),
        "was_unstable": was_unstable,
    }
    _banned[key] = record
    _save(BANNED_FILE, _banned)
    _log(BANNED_LOG,
         f"[{_ts()}] BANNED  {coin_id}  {symbol}  {min_ex}/{max_ex}  "
         f"spread={spread_pct:.4f}%  by={by}  "
         f"alerts=[{','.join(alert_ids)}]  was_unstable={was_unstable}")
    return record


def unban_pair(key: str) -> bool:
    """Move a pair from banned back to unstable for review."""
    if key not in _banned:
        return False
    rec = _banned.pop(key)
    _save(BANNED_FILE, _banned)

    # Re-add to unstable (system) so it shows in unstable tab
    coin_id = rec.get("coin_id", get_coin_id(rec.get("symbol", "?")))
    restore_rec = {
        "key":        key,
        "coin_id":    coin_id,
        "symbol":     rec.get("symbol", "?"),
        "min_ex":     rec.get("min_ex", "?"),
        "max_ex":     rec.get("max_ex", "?"),
        "spread_pct": rec.get("spread_pct", 0),
        "min_price":  rec.get("min_price", 0),
        "max_price":  rec.get("max_price", 0),
        "trust":      rec.get("trust", "medium"),
        "by":         "system",   # system keeps it in unstable after unban
        "alert_ids":  rec.get("alert_ids", []),
        "moved_at":   _ts(),
        "unbanned":   True,
    }
    _unstable[key] = restore_rec
    _save(UNSTABLE_FILE, _unstable)

    _log(BANNED_LOG,
         f"[{_ts()}] UNBANNED  {coin_id}  {rec.get('symbol')}  "
         f"{rec.get('min_ex')}/{rec.get('max_ex')}  by=user  → moved to unstable")
    return True


# ── Mark pair as unstable ─────────────────────────────────────────────────────

def mark_unstable(key: str, symbol: str, min_ex: str, max_ex: str,
                  spread_pct: float, min_price: float, max_price: float,
                  trust: str, by: str = "system", alert_ids: list = None) -> dict:
    """
    Mark a pair as unstable.
    Idempotent — calling twice has no effect.
    Banned takes priority over unstable.
    """
    if key in _banned:
        return _banned[key]
    if key in _unstable:
        return _unstable[key]

    alert_ids = list(set(alert_ids or []))
    coin_id   = get_coin_id(symbol)

    record = {
        "key":        key,
        "coin_id":    coin_id,
        "symbol":     symbol,
        "min_ex":     min_ex,
        "max_ex":     max_ex,
        "spread_pct": round(spread_pct, 5),
        "min_price":  min_price,
        "max_price":  max_price,
        "trust":      trust,
        "by":         by,
        "alert_ids":  alert_ids,
        "moved_at":   _ts(),
    }
    _unstable[key] = record
    _save(UNSTABLE_FILE, _unstable)
    _log(UNSTABLE_LOG,
         f"[{_ts()}] UNSTABLE  {coin_id}  {symbol}  {min_ex}/{max_ex}  "
         f"spread={spread_pct:.4f}%  by={by}  alerts=[{','.join(alert_ids)}]")
    return record


def get_banned_pairs() -> dict:
    return dict(_banned)


def get_unstable_pairs() -> dict:
    return dict(_unstable)


# ── Analytics coin exclusion (by-coin table X button) ─────────────────────────

def exclude_coin_analytics(symbol: str, alert_ids: list):
    """Exclude a whole coin's history from analytics (not from live view)."""
    syms = set(_an_excl.get("symbols", []))
    aids = set(_an_excl.get("alert_ids", []))
    syms.add(symbol)
    aids.update(alert_ids)
    _an_excl["symbols"]   = list(syms)
    _an_excl["alert_ids"] = list(aids)
    _save(AN_EXCL_FILE, _an_excl)
    _log(CLIENT_LOG,
         f"[{_ts()}] EXCLUDE_COIN_ANALYTICS  symbol={symbol}  "
         f"alert_count={len(alert_ids)}")


def include_coin_analytics(symbol: str, alert_ids_to_restore: list = None):
    """Re-include a coin in analytics."""
    syms = set(_an_excl.get("symbols", []))
    syms.discard(symbol)
    _an_excl["symbols"] = list(syms)
    if alert_ids_to_restore:
        aids = set(_an_excl.get("alert_ids", []))
        for aid in alert_ids_to_restore:
            aids.discard(aid)
        _an_excl["alert_ids"] = list(aids)
    _save(AN_EXCL_FILE, _an_excl)
    _log(CLIENT_LOG, f"[{_ts()}] INCLUDE_COIN_ANALYTICS  symbol={symbol}")


# ── Client action log ─────────────────────────────────────────────────────────

def log_client_action(action: str, details: dict = None):
    detail_str = ""
    if details:
        detail_str = "  " + "  ".join(f"{k}={v}" for k, v in details.items())
    _log(CLIENT_LOG, f"[{_ts()}] {action}{detail_str}")


# ── AI context: read all relevant logs and state ──────────────────────────────

def get_log_summary_for_ai() -> str:
    parts = []

    parts.append(f"=== COIN IDs ({len(_coin_ids)} registered) ===")
    for sym, cid in sorted(_coin_ids.items()):
        status = "BANNED" if any(v.get("symbol") == sym for v in _banned.values()) \
            else "UNSTABLE" if any(v.get("symbol") == sym for v in _unstable.values()) \
            else "ok"
        parts.append(f"  {cid} {sym}  [{status}]")

    parts.append(f"\n=== BANNED PAIRS ({len(_banned)}) ===")
    for v in _banned.values():
        parts.append(
            f"  {v['coin_id']} {v['symbol']} {v['min_ex']}/{v['max_ex']}  "
            f"spread={v.get('spread_pct',0):.4f}%  by={v['by']}  banned_at={v['banned_at']}  "
            f"alert_ids=[{','.join(v.get('alert_ids',[]))}]"
        )

    parts.append(f"\n=== UNSTABLE PAIRS ({len(_unstable)}) ===")
    for v in _unstable.values():
        parts.append(
            f"  {v['coin_id']} {v['symbol']} {v['min_ex']}/{v['max_ex']}  "
            f"spread={v.get('spread_pct',0):.4f}%  by={v['by']}  moved_at={v['moved_at']}  "
            f"alert_ids=[{','.join(v.get('alert_ids',[]))}]"
        )

    an_syms = _an_excl.get("symbols", [])
    parts.append(f"\n=== ANALYTICS EXCLUDED COINS: {', '.join(an_syms) or 'none'} ===")
    parts.append(f"    Total excluded alert IDs: {len(get_excluded_alert_ids())}")

    parts.append(f"\n=== RECENT BANNED PAIRS LOG ===\n{_tail(BANNED_LOG, 25)}")
    parts.append(f"\n=== RECENT UNSTABLE PAIRS LOG ===\n{_tail(UNSTABLE_LOG, 25)}")
    parts.append(f"\n=== RECENT CLIENT ACTIONS ===\n{_tail(CLIENT_LOG, 30)}")

    return "\n".join(parts)
