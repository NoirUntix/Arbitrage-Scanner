"""
History Logger — v2
- Alert IDs assigned to each new alert (ALT-XXXXX)
- 10-minute cooldown between same-pair alerts
- Tracks min/max spread per alert lifetime (not per-scan)
- Active alerts tracked in memory; ended alerts start cooldown
"""

import json, os, time, atexit
from datetime import datetime, timezone, timedelta

BASE_DIR  = os.path.dirname(__file__)
DATA_DIR  = os.path.join(BASE_DIR, "data")
LOG_FILE  = os.path.join(BASE_DIR, "alert_logs.txt")
DEDUP_TTL = 600       # 10 minutes cooldown after alert ends before new one allowed
GRACE_TTL = 8         # seconds without seeing a pair before considering alert ended
MIN_GROSS = 0.20      # only count toward "total gross %" if >= this

os.makedirs(DATA_DIR, exist_ok=True)

_dedup: dict         = {}   # key -> timestamp when alert ended (for cooldown)
_active_alerts: dict = {}   # key -> active alert info dict
_session_start       = time.time()
_session_alerts      = 0
_alert_counter       = 0


def _next_id() -> str:
    global _alert_counter
    _alert_counter += 1
    return f"ALT-{_alert_counter:05d}"


def _alert_key(sym, atype, ex_a, ex_b):
    return f"{sym}|{atype}|{min(ex_a, ex_b)}|{max(ex_a, ex_b)}"

def _today_path():
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(DATA_DIR, f"history_{d}.json")

def _load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def _save_json(path, records):
    with open(path, "w") as f:
        json.dump(records, f)

def _append_log(line):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def _write_session_header():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _append_log(f"\n{'='*72}\n  SESSION STARTED: {ts}\n{'='*72}")

def _write_session_footer():
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    dur = round(time.time() - _session_start)
    h, m = divmod(dur // 60, 60); s = dur % 60
    _append_log(
        f"\n{'-'*72}\n  SESSION ENDED: {ts}  |  "
        f"Duration: {h:02d}:{m:02d}:{s:02d}  |  Alerts: {_session_alerts}\n{'-'*72}\n"
    )

_write_session_header()
atexit.register(_write_session_footer)


def _worst_trust(trust_map, ex_a, ex_b):
    t_a = trust_map.get(ex_a, "medium")
    t_b = trust_map.get(ex_b, "medium")
    if "low"    in (t_a, t_b): return "low"
    if "medium" in (t_a, t_b): return "medium"
    return "high"


def record_alerts(scan_data: dict):
    global _session_alerts
    path        = _today_path()
    records     = _load_json(path)
    now         = time.time()
    new_records = []
    seen_keys   = set()

    for sym, d in scan_data.items():
        a = d.get("analysis", {})

        # ── Spread alert ──────────────────────────────────────────────────────
        if a.get("spread_alert"):
            buy_ex     = a.get("min_exchange", "?")
            sell_ex    = a.get("max_exchange", "?")
            key        = _alert_key(sym, "spread", buy_ex, sell_ex)
            spread_pct = round(a.get("spread_pct", 0), 5)
            seen_keys.add(key)

            if key in _active_alerts and not _active_alerts[key].get("ended"):
                # Update existing active alert — track min/max over lifetime
                al = _active_alerts[key]
                al["last_seen_ts"] = now
                al["max_pct"]      = max(al["max_pct"], spread_pct)
                al["min_pct"]      = min(al["min_pct"], spread_pct)
                al["current_pct"]  = spread_pct
                al["buy_price"]    = a.get("min_price")
                al["sell_price"]   = a.get("max_price")
            else:
                # Check 10-min cooldown since last alert ended for this pair
                last_end = _dedup.get(key, 0)
                if now - last_end >= DEDUP_TTL:
                    alert_id = _next_id()
                    trust    = _worst_trust(a.get("trust_map", {}), buy_ex, sell_ex)
                    _active_alerts[key] = {
                        "id":            alert_id,
                        "symbol":        sym,
                        "type":          "spread",
                        "key":           key,
                        "start_ts":      now,
                        "last_seen_ts":  now,
                        "min_pct":       spread_pct,
                        "max_pct":       spread_pct,
                        "current_pct":   spread_pct,
                        "buy_exchange":  buy_ex,
                        "sell_exchange": sell_ex,
                        "buy_price":     a.get("min_price"),
                        "sell_price":    a.get("max_price"),
                        "trust_level":   trust,
                        "ended":         False,
                        "end_ts":        None,
                    }
                    rec = {
                        "ts":             now,
                        "alert_id":       alert_id,
                        "symbol":         sym,
                        "type":           "spread",
                        "spread_pct":     spread_pct,
                        "buy_exchange":   buy_ex,
                        "sell_exchange":  sell_ex,
                        "buy_price":      a.get("min_price"),
                        "sell_price":     a.get("max_price"),
                        "potential_pct":  spread_pct,
                        "funding_diff":   None,
                        "short_exchange": None,
                        "long_exchange":  None,
                        "annual_diff_pct": None,
                        "trust_level":   trust,
                    }
                    new_records.append(rec)
                    _session_alerts += 1
                    ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    _append_log(
                        f"[{ts_s} UTC] [{alert_id}] NEW SPREAD  {sym:<6} "
                        f"+{spread_pct:.4f}%  "
                        f"LONG {buy_ex} @ ${a.get('min_price', 0):.4f}  "
                        f"SHORT {sell_ex} @ ${a.get('max_price', 0):.4f}  "
                        f"trust={trust}"
                    )

        # ── Funding alerts ────────────────────────────────────────────────────
        for opp in a.get("funding_opportunities", []):
            if not opp.get("alert"):
                continue
            short_ex = opp.get("short_exchange", "?")
            long_ex  = opp.get("long_exchange",  "?")
            key      = _alert_key(sym, "funding", short_ex, long_ex)
            diff_pct = round(opp.get("diff_pct", 0), 5)
            seen_keys.add(key)

            if key in _active_alerts and not _active_alerts[key].get("ended"):
                al = _active_alerts[key]
                al["last_seen_ts"] = now
                al["max_pct"]      = max(al["max_pct"], diff_pct)
                al["min_pct"]      = min(al["min_pct"], diff_pct)
                al["current_pct"]  = diff_pct
            else:
                last_end = _dedup.get(key, 0)
                if now - last_end >= DEDUP_TTL:
                    annual   = round(diff_pct * (365 * 24 / 8), 2)
                    alert_id = _next_id()
                    trust    = opp.get("trust_level", "medium")
                    _active_alerts[key] = {
                        "id":             alert_id,
                        "symbol":         sym,
                        "type":           "funding",
                        "key":            key,
                        "start_ts":       now,
                        "last_seen_ts":   now,
                        "min_pct":        diff_pct,
                        "max_pct":        diff_pct,
                        "current_pct":    diff_pct,
                        "short_exchange": short_ex,
                        "long_exchange":  long_ex,
                        "annual_diff_pct": annual,
                        "trust_level":    trust,
                        "ended":          False,
                        "end_ts":         None,
                    }
                    rec = {
                        "ts":             now,
                        "alert_id":       alert_id,
                        "symbol":         sym,
                        "type":           "funding",
                        "spread_pct":     None,
                        "buy_exchange":   None,
                        "sell_exchange":  None,
                        "buy_price":      None,
                        "sell_price":     None,
                        "potential_pct":  diff_pct,
                        "funding_diff":   diff_pct,
                        "short_exchange": short_ex,
                        "long_exchange":  long_ex,
                        "annual_diff_pct": annual,
                        "trust_level":    trust,
                    }
                    new_records.append(rec)
                    _session_alerts += 1
                    ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    _append_log(
                        f"[{ts_s} UTC] [{alert_id}] NEW FUNDING {sym:<6} "
                        f"+{diff_pct:.4f}%/8h ({annual:.1f}% annual)  "
                        f"SHORT {short_ex}  LONG {long_ex}  trust={trust}"
                    )

    # ── End alerts no longer seen ─────────────────────────────────────────────
    for key, al in list(_active_alerts.items()):
        if al.get("ended"):
            # Clean up very stale ended alerts (> 2h)
            if al.get("end_ts") and now - al["end_ts"] > 7200:
                del _active_alerts[key]
            continue

        if key not in seen_keys and now - al["last_seen_ts"] > GRACE_TTL:
            al["ended"]  = True
            al["end_ts"] = now
            _dedup[key]  = now   # start 10-min cooldown
            dur = round(now - al["start_ts"])
            ts_s = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            _append_log(
                f"[{ts_s} UTC] [{al['id']}] ENDED   {al['symbol']:<6} {al['type']:<8} "
                f"duration={dur}s  max={al['max_pct']:.4f}%  "
                f"min={al['min_pct']:.4f}%  "
                f"range={al['max_pct'] - al['min_pct']:.4f}%"
            )

    if new_records:
        records.extend(new_records)
        _save_json(path, records)


def get_active_alerts():
    """Returns (active_list, unstable_list) — unstable = alive > 15 minutes."""
    now      = time.time()
    active   = []
    unstable = []
    for key, al in _active_alerts.items():
        if al.get("ended"):
            continue
        age = now - al["start_ts"]
        data = {
            **al,
            "age_s":     round(age),
            "range_pct": round(al["max_pct"] - al["min_pct"], 5),
        }
        if age > 900:   # 15 minutes → unstable tab
            unstable.append(data)
        else:
            active.append(data)
    return active, unstable


def load_range(days: int) -> list:
    all_records = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        d    = today - timedelta(days=i)
        path = os.path.join(DATA_DIR, f"history_{d}.json")
        all_records.extend(_load_json(path))
    return all_records


def compute_analytics(records: list, coin_filter: str = None, type_filter: str = None) -> dict:
    """
    total_potential_pct = sum of potential_pct for records where potential_pct >= 0.2%
    (gross, before fees ~0.05-0.1% per side)
    """
    if coin_filter:
        records = [r for r in records if r.get("symbol") == coin_filter]
    if type_filter and type_filter in ("spread", "funding"):
        records = [r for r in records if r.get("type") == type_filter]

    if not records:
        return {
            "total_alerts": 0, "total_potential_pct": 0, "avg_potential_pct": 0,
            "qualified_alerts": 0,
            "by_symbol": {}, "by_type": {"spread": 0, "funding": 0}, "by_trust": {},
            "by_exchange_pair": {}, "timeline": [], "best_opportunity": None, "records": [],
        }

    total = len(records)

    qualified       = [r for r in records if (r.get("potential_pct") or 0) >= MIN_GROSS]
    total_potential = sum(r.get("potential_pct", 0) or 0 for r in qualified)

    by_symbol: dict = {}
    for r in records:
        sym = r["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = {"count": 0, "total_pct": 0, "best_pct": 0, "qualified": 0}
        p = r.get("potential_pct", 0) or 0
        by_symbol[sym]["count"] += 1
        if p >= MIN_GROSS:
            by_symbol[sym]["total_pct"] = round(by_symbol[sym]["total_pct"] + p, 5)
            by_symbol[sym]["qualified"] += 1
        by_symbol[sym]["best_pct"] = round(max(by_symbol[sym]["best_pct"], p), 5)

    by_type = {"spread": 0, "funding": 0}
    for r in records:
        by_type[r.get("type", "spread")] += 1

    by_pair: dict = {}
    for r in records:
        key = (
            f"{r.get('buy_exchange','?')} → {r.get('sell_exchange','?')}"
            if r["type"] == "spread"
            else f"{r.get('long_exchange','?')} / {r.get('short_exchange','?')}"
        )
        if key not in by_pair:
            by_pair[key] = {"count": 0, "total_pct": 0}
        by_pair[key]["count"] += 1
        p = r.get("potential_pct", 0) or 0
        if p >= MIN_GROSS:
            by_pair[key]["total_pct"] = round(by_pair[key]["total_pct"] + p, 5)

    by_trust = {"high": 0, "medium": 0, "low": 0}
    for r in records:
        t = r.get("trust_level", "medium")
        by_trust[t] = by_trust.get(t, 0) + 1

    buckets: dict = {}
    for r in records:
        hour = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:00")
        if hour not in buckets:
            buckets[hour] = {"time": hour, "count": 0, "potential": 0}
        buckets[hour]["count"] += 1
        p = r.get("potential_pct", 0) or 0
        if p >= MIN_GROSS:
            buckets[hour]["potential"] = round(buckets[hour]["potential"] + p, 5)

    timeline = sorted(buckets.values(), key=lambda x: x["time"])
    best     = max(records, key=lambda r: r.get("potential_pct", 0) or 0)

    return {
        "total_alerts":        total,
        "qualified_alerts":    len(qualified),
        "total_potential_pct": round(total_potential, 4),
        "avg_potential_pct":   round(total_potential / len(qualified), 4) if qualified else 0,
        "min_gross_threshold": MIN_GROSS,
        "by_symbol":           dict(sorted(by_symbol.items(), key=lambda x: x[1]["total_pct"], reverse=True)),
        "by_type":             by_type,
        "by_trust":            by_trust,
        "by_exchange_pair":    dict(sorted(by_pair.items(), key=lambda x: x[1]["count"], reverse=True)),
        "timeline":            timeline,
        "best_opportunity":    best,
        "records":             sorted(records, key=lambda r: r["ts"], reverse=True),
    }
