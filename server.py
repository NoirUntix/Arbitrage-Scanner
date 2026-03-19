"""
Arbitrage Scanner Web Server  —  python server.py  →  http://localhost:5000
"""

import asyncio, json, threading, time, logging, urllib.request, urllib.error, os
from flask import Flask, jsonify, send_from_directory, request
from scanner import scan_all, EXCHANGE_META, WITHDRAWAL_STATUS
from history import (
    record_alerts, load_range, compute_analytics, get_active_alerts,
    mark_pair_status, get_pair_statuses,
    exclude_analytics_symbol, unexclude_analytics_symbol, get_analytics_excluded,
    log_client_action, get_log_tail, get_all_logs_summary,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app          = Flask(__name__, static_folder="static")
_cache       = {}
_last_scan   = 0.0
_scanning    = False
_scan_count  = 0
MIN_INTERVAL = 2.0

_exchange_health    = {}
_exchange_health_ts = 0
HEALTH_REFRESH_SEC  = 60

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
UNSTABLE_SPREAD_THRESHOLD = 1.0   # spread % that triggers system auto-unstable


# ── Health helpers ────────────────────────────────────────────────────────────

def _fetch(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def _check_binance():
    issues, details = [], []
    code, _ = _fetch("https://fapi.binance.com/fapi/v1/ping", 3)
    if code == 200: details.append("Futures API: OK")
    else: issues.append("Futures API unreachable")
    code2, body2 = _fetch("https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=5", 4)
    if code2 == 200:
        try:
            top_bid = float(json.loads(body2)["bids"][0][0])
            details.append(f"BTC bid: ${top_bid:,.0f} — liquidity OK")
        except Exception: pass
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bybit():
    issues, details = [], []
    code, _ = _fetch("https://api.bybit.com/v5/market/time", 4)
    if code == 200: details.append("API time: OK")
    else: issues.append("API unreachable")
    code2, body2 = _fetch("https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=5", 4)
    if code2 == 200:
        try:
            bids = json.loads(body2).get("result", {}).get("b", [])
            if bids: details.append(f"BTC bid: ${float(bids[0][0]):,.0f} — liquidity OK")
        except Exception: pass
    else: issues.append("Order book unavailable")
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational. High liquidity confirmed."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_okx():
    issues, details = [], []
    code, body = _fetch("https://www.okx.com/api/v5/system/status", 5)
    if code == 200:
        try:
            for item in json.loads(body).get("data", []):
                if item.get("state") != "normal":
                    issues.append(f"{item.get('title','Service')}: {item.get('state')}")
            if not issues: details.append("OKX system status: all normal")
        except Exception: details.append("Status API: OK")
    else: issues.append("System status API unreachable")
    code2, _ = _fetch("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP", 4)
    if code2 == 200: details.append("Futures ticker: OK")
    else: issues.append("Futures ticker unavailable")
    st = "warning" if len(issues) > 1 else ("caution" if issues else "normal")
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_mexc():
    issues, details = [], []
    code, body = _fetch("https://contract.mexc.com/api/v1/contract/ticker?symbol=BTC_USDT", 5)
    if code == 200:
        try:
            price = json.loads(body).get("data", {}).get("lastPrice")
            if price: details.append(f"Futures: OK (BTC ~${float(price):,.0f})")
            else: issues.append("Price data missing")
        except Exception: issues.append("API malformed")
    else: issues.append("Futures API unreachable")
    issues.append("Withdrawal delays common on altcoins")
    return {"status": "caution", "note": "; ".join(issues), "details": details,
            "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_gate():
    issues, details = [], []
    code, body = _fetch("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=BTC_USDT", 5)
    if code == 200:
        try:
            d = json.loads(body)
            if d:
                vol = float(d[0].get("volume_24h_usd", 0))
                details.append(f"BTC 24h vol: ${vol/1e6:.0f}M")
                if vol < 50_000_000: issues.append("Low 24h volume — liquidity risk")
        except Exception: pass
    else: issues.append("Futures API unreachable")
    st = "caution" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_kucoin():
    issues, details = [], []
    code, body = _fetch("https://api-futures.kucoin.com/api/v1/ticker?symbol=XBTUSDTM", 5)
    if code == 200:
        try:
            price = json.loads(body).get("data", {}).get("price")
            if price: details.append(f"Futures: OK (BTC ~${float(price):,.0f})")
        except Exception: pass
    else: issues.append("Futures API unreachable")
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bitget():
    issues, details = [], []
    code, body = _fetch("https://api.bitget.com/api/mix/v1/market/ticker?symbol=BTCUSDT_UMCBL&productType=umcbl", 5)
    if code == 200:
        try:
            vol = float(json.loads(body).get("data", {}).get("usdtVolume", 0))
            details.append(f"24h vol: ${vol/1e6:.0f}M")
            if vol < 30_000_000: issues.append("Low volume — check order book")
        except Exception: pass
    else: issues.append("Futures API unreachable")
    issues.append("Mid-tier liquidity — verify depth before large trades")
    return {"status": "caution", "note": "; ".join(issues), "details": details,
            "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_coinex():
    issues, details = [], []
    code, _ = _fetch("https://api.coinex.com/perpetual/v1/market/ticker?market=BTCUSDT", 5)
    if code == 200: details.append("API: reachable")
    else: issues.append("Futures API not responding")
    issues.append("Low volume — verify withdrawal availability per coin")
    return {"status": "caution", "note": "; ".join(issues), "details": details,
            "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bitmart():
    issues, details = [], []
    code, _ = _fetch("https://api-cloud.bitmart.com/contract/public/details?symbol=BTCUSDT", 5)
    if code == 200: details.append("API: reachable")
    else: issues.append("Futures API unreachable")
    issues.append("Known withdrawal delays; low liquidity; high arbitrage risk")
    return {"status": "warning", "note": "; ".join(issues), "details": details,
            "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


HEALTH_CHECKERS = {
    "Binance": _check_binance, "Bybit": _check_bybit, "OKX": _check_okx,
    "MEXC": _check_mexc, "Gate.io": _check_gate, "KuCoin": _check_kucoin,
    "Bitget": _check_bitget, "CoinEx": _check_coinex, "Bitmart": _check_bitmart,
}


def refresh_exchange_health():
    global _exchange_health, _exchange_health_ts
    result = {}
    for name, checker in HEALTH_CHECKERS.items():
        try:
            result[name] = checker()
        except Exception as e:
            log.warning(f"Health check {name}: {e}")
            result[name] = {"status": "unknown", "note": "Health check failed", "details": [], "checked_at": "—"}
    _exchange_health    = result
    _exchange_health_ts = time.time()
    log.info("Exchange health refreshed")


def health_refresh_loop():
    while True:
        try: refresh_exchange_health()
        except Exception as e: log.error(f"Health refresh: {e}")
        time.sleep(HEALTH_REFRESH_SEC)


# ── Auto-unstable detection ───────────────────────────────────────────────────

def _auto_mark_unstable_if_needed(data: dict):
    """Mark any pair with spread > UNSTABLE_SPREAD_THRESHOLD as system unstable."""
    for sym, d in data.items():
        a  = d.get("analysis", {})
        sp = a.get("spread_pct", 0) or 0
        if sp <= UNSTABLE_SPREAD_THRESHOLD:
            continue
        min_ex = a.get("min_exchange", "")
        max_ex = a.get("max_exchange", "")
        if not min_ex or not max_ex:
            continue
        coin_id, changed = mark_pair_status(
            sym, min_ex, max_ex,
            status="unstable", removal_type="system",
            spread_pct=sp,
            reason=f"auto: spread {sp:.4f}% > {UNSTABLE_SPREAD_THRESHOLD}%",
        )
        if changed:
            log.info(f"Auto-unstable: {sym} {min_ex}/{max_ex} spread={sp:.4f}% [{coin_id}]")


# ── Background scanner ────────────────────────────────────────────────────────

def background_scanner():
    global _cache, _last_scan, _scanning, _scan_count
    while True:
        t0 = time.time()
        try:
            _scanning = True
            data      = asyncio.run(scan_all())
            elapsed   = round(time.time() - t0, 2)
            _cache    = {"data": data, "elapsed": elapsed, "ts": time.time()}
            _last_scan = time.time()
            _scan_count += 1
            log.info(f"Scan #{_scan_count} in {elapsed}s")
            try:
                record_alerts(data)
                _auto_mark_unstable_if_needed(data)
            except Exception as he:
                log.warning(f"Post-scan hooks: {he}")
        except Exception as e:
            log.error(f"Scan error: {e}")
        finally:
            _scanning = False
        wait = max(0, MIN_INTERVAL - (time.time() - t0))
        if wait:
            time.sleep(wait)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/data")
def api_data():
    return jsonify({"cache": _cache, "scan_interval": MIN_INTERVAL,
                    "scan_count": _scan_count, "scanning": _scanning})


@app.route("/api/exchanges")
def api_exchanges():
    result = {}
    for name, meta in EXCHANGE_META.items():
        health = _exchange_health.get(name, {
            "status":     WITHDRAWAL_STATUS.get(name, {}).get("status", "unknown"),
            "note":       WITHDRAWAL_STATUS.get(name, {}).get("note", "Checking..."),
            "details":    [],
            "checked_at": "—",
        })
        result[name] = {**meta, "withdrawal": health}
    return jsonify(result)


@app.route("/api/active_alerts")
def api_active_alerts():
    active, unstable = get_active_alerts()
    return jsonify({"active": active, "unstable": unstable})


@app.route("/api/pair_statuses")
def api_pair_statuses():
    return jsonify(get_pair_statuses())


@app.route("/api/coin_ids")
def api_coin_ids():
    from history import _coin_ids
    return jsonify(_coin_ids)


# ── Pair action endpoints ──────────────────────────────────────────────────────

@app.route("/api/action/unstable", methods=["POST"])
def api_action_unstable():
    b      = request.get_json(force=True) or {}
    sym    = b.get("symbol", "?")
    min_ex = b.get("min_ex", "?")
    max_ex = b.get("max_ex", "?")
    sp     = float(b.get("spread_pct", 0))
    by     = b.get("by", "user")
    reason = b.get("reason", "manual removal" if by == "user" else "system removal")

    coin_id, changed = mark_pair_status(
        sym, min_ex, max_ex, status="unstable",
        removal_type=by, spread_pct=sp, reason=reason,
    )
    if by == "user":
        log_client_action("MANUAL_UNSTABLE", f"{sym} {min_ex}/{max_ex}",
                          f"coin_id={coin_id} spread={sp:.4f}%")
    return jsonify({"ok": True, "coin_id": coin_id, "changed": changed})


@app.route("/api/action/ban", methods=["POST"])
def api_action_ban():
    b      = request.get_json(force=True) or {}
    sym    = b.get("symbol", "?")
    min_ex = b.get("min_ex", "?")
    max_ex = b.get("max_ex", "?")
    sp     = float(b.get("spread_pct", 0))
    by     = b.get("by", "user")

    coin_id, changed = mark_pair_status(
        sym, min_ex, max_ex, status="banned",
        removal_type=by, spread_pct=sp, reason=f"banned by {by}",
    )
    log_client_action("BAN_PAIR", f"{sym} {min_ex}/{max_ex}",
                      f"coin_id={coin_id} spread={sp:.4f}% by={by}")
    return jsonify({"ok": True, "coin_id": coin_id, "changed": changed})


@app.route("/api/action/exclude_coin", methods=["POST"])
def api_action_exclude_coin():
    b      = request.get_json(force=True) or {}
    symbol = b.get("symbol", "")
    action = b.get("action", "exclude")
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"})
    if action == "exclude":
        exclude_analytics_symbol(symbol)
        log_client_action("EXCLUDE_COIN_ANALYTICS", symbol)
    else:
        unexclude_analytics_symbol(symbol)
        log_client_action("INCLUDE_COIN_ANALYTICS", symbol)
    return jsonify({"ok": True, "excluded_coins": get_analytics_excluded()})


@app.route("/api/action/log", methods=["POST"])
def api_action_log():
    b       = request.get_json(force=True) or {}
    action  = b.get("action", "UNKNOWN")
    target  = b.get("target", "—")
    details = b.get("details", "")
    log_client_action(action, target, details)
    return jsonify({"ok": True})


@app.route("/api/logs/<log_type>")
def api_log(log_type):
    lines = int(request.args.get("lines", 100))
    return jsonify({"log_type": log_type, "content": get_log_tail(log_type, lines)})


@app.route("/api/analytics")
def api_analytics():
    period      = request.args.get("period", "day")
    coin_filter = request.args.get("coin", None) or None
    type_filter = request.args.get("type", None) or None
    days        = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    records     = load_range(days)
    return jsonify(compute_analytics(records, coin_filter=coin_filter, type_filter=type_filter))


@app.route("/api/news")
def api_news():
    headlines = []
    try:
        req = urllib.request.Request(
            "https://cryptopanic.com/api/v1/posts/?auth_token=public&kind=news&currencies=BTC,ETH&filter=hot",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = json.loads(resp.read())
        for item in raw.get("results", [])[:10]:
            headlines.append({
                "title":      item.get("title", ""),
                "url":        item.get("url", ""),
                "source":     item.get("source", {}).get("title", ""),
                "published":  item.get("published_at", ""),
                "currencies": [c["code"] for c in item.get("currencies", [])],
                "kind":       item.get("kind", "news"),
            })
    except Exception as e:
        log.warning(f"News: {e}")
    return jsonify({"headlines": headlines, "static_links": [
        {"title": "Coinglass — Funding Rates",       "url": "https://www.coinglass.com/FundingRate",      "source": "Coinglass",  "kind": "tool"},
        {"title": "Coinglass — Liquidation Heatmap", "url": "https://www.coinglass.com/LiquidationData",  "source": "Coinglass",  "kind": "tool"},
        {"title": "CoinDesk — Derivatives",          "url": "https://www.coindesk.com/search?s=arbitrage", "source": "CoinDesk",  "kind": "search"},
        {"title": "CryptoPanic — Live Feed",         "url": "https://cryptopanic.com",                    "source": "CryptoPanic","kind": "tool"},
    ]})


# ── AI Chat ───────────────────────────────────────────────────────────────────

def _build_scan_summary():
    data = _cache.get("data", {})
    if not data:
        return "No live scan data available yet."
    lines = []
    top_spreads = sorted(
        [(sym, d["analysis"]) for sym, d in data.items()
         if d.get("analysis", {}).get("spread_pct", 0) > 0],
        key=lambda x: x[1]["spread_pct"], reverse=True
    )[:8]
    if top_spreads:
        lines.append("TOP LIVE SPREADS:")
        for sym, a in top_spreads:
            lines.append(f"  {sym}: {a['spread_pct']:.4f}% ({a.get('min_exchange')} → {a.get('max_exchange')})")
    top_funding = []
    for sym, d in data.items():
        for opp in (d.get("analysis", {}).get("funding_opportunities", []) or [])[:1]:
            top_funding.append((sym, opp))
    top_funding.sort(key=lambda x: x[1].get("diff_pct", 0), reverse=True)
    if top_funding[:6]:
        lines.append("TOP LIVE FUNDING DIFFS:")
        for sym, opp in top_funding[:6]:
            lines.append(f"  {sym}: +{opp['diff_pct']:.4f}%/8h SHORT {opp['short_exchange']} LONG {opp['long_exchange']}")
    return "\n".join(lines) if lines else "No significant live opportunities at this moment."


@app.route("/api/ai_chat", methods=["POST"])
def api_ai_chat():
    body     = request.get_json(force=True) or {}
    messages = body.get("messages", [])
    period   = body.get("period", "day")
    coin     = body.get("coin", None) or None
    days     = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    an       = compute_analytics(load_range(days), coin_filter=coin)
    scan_sum = _build_scan_summary()
    logs_sum = get_all_logs_summary()
    ps       = get_pair_statuses()
    banned   = [(v.get("coin_id"), v.get("symbol"), v.get("min_exchange"), v.get("max_exchange"),
                 v.get("removed_at")) for v in ps.values() if v.get("status") == "banned"]
    unstable = [(v.get("coin_id"), v.get("symbol"), v.get("min_exchange"), v.get("max_exchange"),
                 v.get("removed_at"), v.get("removal_type")) for v in ps.values() if v.get("status") == "unstable"]

    top_coins = list(an.get("by_symbol", {}).keys())[:6]
    best      = an.get("best_opportunity")
    best_str  = ""
    if best:
        pair     = (f"{best.get('buy_exchange')} → {best.get('sell_exchange')}"
                    if best.get("type") == "spread"
                    else f"SHORT {best.get('short_exchange')} LONG {best.get('long_exchange')}")
        best_str = f"{best.get('symbol')} {best.get('type','').upper()} {pair} +{best.get('potential_pct',0):.4f}%"

    banned_str   = "\n".join(f"  {r[0]} {r[1]} {r[2]}/{r[3]} at={r[4]}" for r in banned[:10]) or "  none"
    unstable_str = "\n".join(f"  {r[0]} {r[1]} {r[2]}/{r[3]} at={r[4]} by={r[5]}" for r in unstable[:10]) or "  none"

    system_prompt = f"""You are an expert crypto perpetual futures and funding rate arbitrage assistant embedded in a live multi-exchange scanner.

LIVE ANALYTICS ({period}):
- Total: {an['total_alerts']} alerts | Active (not excluded): {an.get('active_alerts', '?')} | Excluded: {an.get('excluded_alerts', 0)}
- Qualified (≥0.8%): {an['qualified_alerts']} | Total gross: +{an['total_potential_pct']:.4f}% before fees
- Spread alerts: {an['by_type'].get('spread',0)} | Funding alerts: {an['by_type'].get('funding',0)}
- Top coins: {', '.join(top_coins) or 'none'} | Best: {best_str or 'none'}

{scan_sum}

BANNED PAIRS ({len(banned)}):
{banned_str}

UNSTABLE PAIRS ({len(unstable)}):
{unstable_str}

ANALYTICS EXCLUDED COINS: {', '.join(get_analytics_excluded()) or 'none'}

LOG: alert_logs.txt (recent):
{logs_sum.get('alerts', '(empty)')}

LOG: banned_coins.log (recent):
{logs_sum.get('banned', '(empty)')}

LOG: unstable_coins.log (recent):
{logs_sum.get('unstable', '(empty)')}

LOG: client_actions.log (recent):
{logs_sum.get('client', '(empty)')}

SCANNER MECHANICS:
- Exchanges: Binance Bybit OKX MEXC Gate.io KuCoin Bitget CoinEx Bitmart
- 45 symbols tracked | Scan every 2s | Funding 8h cycles (annualised × 1095)
- Spread alert ≥0.15% | Funding alert ≥0.03%/8h | Unstable auto-trigger: spread > {UNSTABLE_SPREAD_THRESHOLD}%
- Qualified threshold: ≥0.80% gross | Fees ~0.10% round-trip (0.05% per side maker)
- Mid price = (max_price + min_price) / 2 — entry reference for sizing legs
- Pair status: normal → unstable → banned (never downgraded). Unstable pairs excluded from analytics.
- Trust: high=Binance/Bybit/OKX/KuCoin  medium=MEXC/Gate.io/Bitget/CoinEx  low=Bitmart
- Alert IDs: ALT-XXXXX | Coin IDs: COIN-XXXXX (permanent per symbol+exchange pair)

You have web search available. Be direct and actionable. Reference specific IDs, coins, and log entries when relevant."""

    if not ANTHROPIC_API_KEY:
        return _simple_ai_fallback(an, messages, period, ps)

    try:
        payload = {
            "model":      "claude-sonnet-4-20250514",
            "max_tokens": 1200,
            "system":     system_prompt,
            "messages":   messages,
            "tools":      [{"type": "web_search_20250305", "name": "web_search"}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta":    "web-search-2025-03-05",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        reply = " ".join(
            block.get("text", "") for block in result.get("content", [])
            if block.get("type") == "text"
        ).strip()
        return jsonify({"reply": reply or "No response generated.", "ai_mode": "full"})

    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="ignore")
        log.error(f"Anthropic API HTTP {e.code}: {body_err[:200]}")
        return jsonify({"reply": f"AI API error {e.code} — check ANTHROPIC_API_KEY.", "ai_mode": "error"})
    except Exception as e:
        log.error(f"AI chat error: {e}")
        return _simple_ai_fallback(an, messages, period, ps)


def _simple_ai_fallback(an, messages, period, ps=None):
    message = (messages[-1].get("content", "") if messages else "").strip().lower()
    def fmt(v): return f"+{v:.4f}%"
    reply = None
    if any(k in message for k in ["best", "top", "biggest", "highest"]):
        b = an.get("best_opportunity")
        if b:
            pair = (f"{b.get('buy_exchange')} → {b.get('sell_exchange')}" if b.get("type") == "spread"
                    else f"SHORT {b.get('short_exchange')}, LONG {b.get('long_exchange')}")
            reply = f"Best ({period}): {b['symbol']} {b['type'].upper()}\nGross: {fmt(b.get('potential_pct',0))}\nPair: {pair}"
        else:
            reply = "No opportunities yet."
    elif any(k in message for k in ["banned", "unstable", "excluded", "removed"]):
        if ps is None: ps = get_pair_statuses()
        banned   = sum(1 for v in ps.values() if v.get("status") == "banned")
        unstable = sum(1 for v in ps.values() if v.get("status") == "unstable")
        excl     = get_analytics_excluded()
        reply    = f"Banned: {banned} pairs | Unstable: {unstable} pairs | Analytics excluded coins: {', '.join(excl) or 'none'}"
    elif any(k in message for k in ["how many", "count", "total"]):
        reply = (f"{period}: {an['total_alerts']} total, {an['qualified_alerts']} qualified (≥0.8%). "
                 f"Excluded: {an.get('excluded_alerts', 0)}")
    elif any(k in message for k in ["earn", "profit", "gross"]):
        reply = (f"Gross ({period}): {fmt(an['total_potential_pct'])} from {an['qualified_alerts']} "
                 f"qualified alerts. Avg: {fmt(an['avg_potential_pct'])}.")
    else:
        reply = (f"Summary ({period}): {an['total_alerts']} alerts, "
                 f"{an['qualified_alerts']} qualified (≥0.8%), gross {fmt(an['total_potential_pct'])}. "
                 f"Excluded from analytics: {an.get('excluded_alerts', 0)}.\n"
                 f"Set ANTHROPIC_API_KEY for full AI + web search.")
    return jsonify({"reply": reply, "ai_mode": "fallback"})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if ANTHROPIC_API_KEY:
        log.info("Anthropic API key detected — full AI mode enabled")
    else:
        log.warning("ANTHROPIC_API_KEY not set — AI assistant running in fallback mode")
    threading.Thread(target=health_refresh_loop, daemon=True).start()
    threading.Thread(target=background_scanner,  daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
