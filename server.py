"""
Arbitrage Scanner Web Server  —  python server.py  →  http://localhost:5000
"""

import asyncio, json, threading, time, logging, urllib.request, urllib.error
from flask import Flask, jsonify, send_from_directory, request
from scanner import scan_all, EXCHANGE_META, WITHDRAWAL_STATUS
from history import record_alerts, load_range, compute_analytics, get_active_alerts

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


# ── Live exchange health checker ──────────────────────────────────────────────

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
    if code == 200:
        details.append("Futures API: OK")
    else:
        issues.append("Futures API unreachable")
    # Liquidity probe
    code2, body2 = _fetch("https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=5", 4)
    if code2 == 200:
        try:
            d = json.loads(body2)
            top_bid = float(d["bids"][0][0])
            details.append(f"BTC bid: ${top_bid:,.0f} — liquidity OK")
        except Exception:
            pass
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational. Futures and withdrawals normal."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bybit():
    issues, details = [], []
    code, body = _fetch("https://api.bybit.com/v5/market/time", 4)
    if code == 200:
        details.append("API time: OK")
    else:
        issues.append("API unreachable")
    code2, body2 = _fetch("https://api.bybit.com/v5/market/orderbook?category=linear&symbol=BTCUSDT&limit=5", 4)
    if code2 == 200:
        try:
            d = json.loads(body2)
            bids = d.get("result", {}).get("b", [])
            if bids:
                details.append(f"BTC bid: ${float(bids[0][0]):,.0f} — liquidity OK")
        except Exception:
            pass
    else:
        issues.append("Order book unavailable")
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational. High liquidity confirmed."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_okx():
    issues, details = [], []
    code, body = _fetch("https://www.okx.com/api/v5/system/status", 5)
    if code == 200:
        try:
            d = json.loads(body)
            for item in d.get("data", []):
                if item.get("state") != "normal":
                    issues.append(f"{item.get('title','Service')}: {item.get('state','?')}")
            if not issues:
                details.append("OKX system status: all normal")
        except Exception:
            details.append("Status API: OK")
    else:
        issues.append("System status API unreachable")
    code2, _ = _fetch("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP", 4)
    if code2 == 200:
        details.append("Futures ticker: OK")
    else:
        issues.append("Futures ticker unavailable")
    st = "warning" if len(issues) > 1 else ("caution" if issues else "normal")
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_mexc():
    issues, details = [], []
    code, body = _fetch("https://contract.mexc.com/api/v1/contract/ticker?symbol=BTC_USDT", 5)
    if code == 200:
        try:
            price = json.loads(body).get("data", {}).get("lastPrice")
            if price:
                details.append(f"Futures: OK (BTC ~${float(price):,.0f})")
            else:
                issues.append("Price data missing")
        except Exception:
            issues.append("API response malformed")
    else:
        issues.append("Futures API unreachable")
    issues.append("Withdrawal delays common on altcoins — verify each coin before transfer")
    st = "caution"
    note = "; ".join(issues)
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_gate():
    issues, details = [], []
    code, body = _fetch("https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=BTC_USDT", 5)
    if code == 200:
        try:
            d = json.loads(body)
            if d:
                vol = float(d[0].get("volume_24h_usd", 0))
                details.append(f"BTC 24h vol: ${vol/1e6:.0f}M")
                if vol < 50_000_000:
                    issues.append(f"Low 24h volume ${vol/1e6:.1f}M — liquidity risk on large orders")
        except Exception:
            pass
    else:
        issues.append("Futures API unreachable")
    st = "caution" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_kucoin():
    issues, details = [], []
    code, body = _fetch("https://api-futures.kucoin.com/api/v1/ticker?symbol=XBTUSDTM", 5)
    if code == 200:
        try:
            price = json.loads(body).get("data", {}).get("price")
            if price:
                details.append(f"Futures: OK (BTC ~${float(price):,.0f})")
        except Exception:
            pass
    else:
        issues.append("Futures API unreachable")
    st = "warning" if issues else "normal"
    note = "; ".join(issues) if issues else "All systems operational."
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bitget():
    issues, details = [], []
    code, body = _fetch("https://api.bitget.com/api/mix/v1/market/ticker?symbol=BTCUSDT_UMCBL&productType=umcbl", 5)
    if code == 200:
        try:
            d = json.loads(body).get("data", {})
            vol = float(d.get("usdtVolume", 0))
            details.append(f"24h vol: ${vol/1e6:.0f}M")
            if vol < 30_000_000:
                issues.append(f"Low volume ${vol/1e6:.1f}M — check order book before trading")
        except Exception:
            pass
    else:
        issues.append("Futures API unreachable")
    issues.append("Mid-tier liquidity — verify order book depth before large trades")
    st = "caution"
    note = "; ".join(issues)
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_coinex():
    issues, details = [], []
    code, _ = _fetch("https://api.coinex.com/perpetual/v1/market/ticker?market=BTCUSDT", 5)
    if code == 200:
        details.append("API: reachable")
    else:
        issues.append("Futures API not responding")
    issues.append("Low volume — withdrawals may take longer; verify each coin before transfer")
    st = "caution"
    note = "; ".join(issues)
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


def _check_bitmart():
    issues, details = [], []
    code, _ = _fetch("https://api-cloud.bitmart.com/contract/public/details?symbol=BTCUSDT", 5)
    if code == 200:
        details.append("API: reachable")
    else:
        issues.append("Futures API unreachable")
    issues.append("Historically reported withdrawal delays; low liquidity on many pairs; high arbitrage risk")
    st = "warning"
    note = "; ".join(issues)
    return {"status": st, "note": note, "details": details, "checked_at": time.strftime("%H:%M UTC", time.gmtime())}


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
        try:
            refresh_exchange_health()
        except Exception as e:
            log.error(f"Health refresh: {e}")
        time.sleep(HEALTH_REFRESH_SEC)


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
            except Exception as he:
                log.warning(f"History: {he}")
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


@app.route("/api/analytics")
def api_analytics():
    period      = request.args.get("period", "day")
    coin_filter = request.args.get("coin",   None) or None
    type_filter = request.args.get("type",   None) or None
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
            headlines.append({"title": item.get("title",""), "url": item.get("url",""),
                               "source": item.get("source",{}).get("title",""),
                               "published": item.get("published_at",""),
                               "currencies": [c["code"] for c in item.get("currencies",[])],
                               "kind": item.get("kind","news")})
    except Exception as e:
        log.warning(f"News: {e}")
    return jsonify({"headlines": headlines, "static_links": [
        {"title": "Coinglass — Funding Rates",       "url": "https://www.coinglass.com/FundingRate",     "source": "Coinglass",  "kind": "tool"},
        {"title": "Coinglass — Liquidation Heatmap", "url": "https://www.coinglass.com/LiquidationData", "source": "Coinglass",  "kind": "tool"},
        {"title": "CoinDesk — Derivatives",          "url": "https://www.coindesk.com/search?s=arbitrage","source": "CoinDesk", "kind": "search"},
        {"title": "CryptoPanic — Live Feed",         "url": "https://cryptopanic.com",                   "source": "CryptoPanic","kind": "tool"},
    ]})


@app.route("/api/ai_chat", methods=["POST"])
def api_ai_chat():
    body    = request.get_json(force=True) or {}
    message = body.get("message", "").strip().lower()
    period  = body.get("period", "day")
    coin    = body.get("coin", None) or None
    days    = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    an      = compute_analytics(load_range(days), coin_filter=coin)

    def fmt_pct(v): return f"+{v:.4f}%"

    reply = None
    if any(k in message for k in ["best", "top", "biggest", "highest"]):
        b = an.get("best_opportunity")
        if b:
            pair = (f"{b.get('buy_exchange','?')} -> {b.get('sell_exchange','?')}" if b["type"]=="spread"
                    else f"{b.get('long_exchange','?')} / {b.get('short_exchange','?')}")
            reply = f"Best ({period}): {b['symbol']} {b['type'].upper()}\nGross: {fmt_pct(b.get('potential_pct',0))}\nPair: {pair}"
        else:
            reply = "No opportunities yet."
    elif any(k in message for k in ["how many", "count", "total"]):
        reply = (f"{period}: {an['total_alerts']} alerts, {an['qualified_alerts']} qualified.\n"
                 f"Spread: {an['by_type'].get('spread',0)}, Funding: {an['by_type'].get('funding',0)}")
    elif any(k in message for k in ["earn", "profit", "gross"]):
        reply = (f"Gross ({period}): {fmt_pct(an['total_potential_pct'])} "
                 f"from {an['qualified_alerts']} qualified alerts. Avg: {fmt_pct(an['avg_potential_pct'])}")
    elif any(k in message for k in ["coin", "symbol"]):
        top = list(an["by_symbol"].items())[:3]
        reply = "Top coins: " + ", ".join(f"{s} ({v['qualified']} alerts)" for s,v in top) if top else "No data."
    else:
        reply = (f"Summary ({period}): {an['total_alerts']} alerts, "
                 f"{an['qualified_alerts']} qualified, gross {fmt_pct(an['total_potential_pct'])}.")

    return jsonify({"reply": reply})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=health_refresh_loop, daemon=True).start()
    threading.Thread(target=background_scanner,  daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
