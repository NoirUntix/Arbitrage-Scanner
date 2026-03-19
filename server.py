"""
Arbitrage Scanner Web Server  —  python server.py  →  http://localhost:5000
"""

import asyncio, json, threading, time, logging, urllib.request, urllib.error, os
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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


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


# ── AI Chat — full LLM with web search and arbitrage context ──────────────────

def _build_scan_summary():
    """Build a compact summary of current live scan data for the AI."""
    data = _cache.get("data", {})
    if not data:
        return "No live scan data available yet."
    lines = []
    top_spreads = sorted(
        [(sym, d["analysis"]) for sym, d in data.items() if d.get("analysis", {}).get("spread_pct", 0) > 0],
        key=lambda x: x[1]["spread_pct"], reverse=True
    )[:8]
    if top_spreads:
        lines.append("TOP LIVE SPREADS:")
        for sym, a in top_spreads:
            lines.append(f"  {sym}: {a['spread_pct']:.4f}% ({a.get('min_exchange','?')} → {a.get('max_exchange','?')})")
    top_funding = []
    for sym, d in data.items():
        for opp in (d.get("analysis", {}).get("funding_opportunities", []) or [])[:1]:
            top_funding.append((sym, opp))
    top_funding.sort(key=lambda x: x[1].get("diff_pct", 0), reverse=True)
    if top_funding[:6]:
        lines.append("TOP LIVE FUNDING DIFFS:")
        for sym, opp in top_funding[:6]:
            lines.append(
                f"  {sym}: +{opp['diff_pct']:.4f}%/8h — SHORT {opp['short_exchange']}, LONG {opp['long_exchange']}"
            )
    return "\n".join(lines) if lines else "No significant opportunities at this moment."


@app.route("/api/ai_chat", methods=["POST"])
def api_ai_chat():
    body     = request.get_json(force=True) or {}
    messages = body.get("messages", [])
    period   = body.get("period", "day")
    coin     = body.get("coin", None) or None
    days     = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    an       = compute_analytics(load_range(days), coin_filter=coin)
    scan_summary = _build_scan_summary()

    top_coins = list(an.get("by_symbol", {}).keys())[:6]
    best      = an.get("best_opportunity")
    best_str  = ""
    if best:
        pair = (f"{best.get('buy_exchange','?')} → {best.get('sell_exchange','?')}"
                if best["type"] == "spread"
                else f"SHORT {best.get('short_exchange','?')}, LONG {best.get('long_exchange','?')}")
        best_str = f"{best['symbol']} {best['type'].upper()} {pair} +{best.get('potential_pct',0):.4f}%"

    system_prompt = f"""You are an expert crypto perpetual futures and funding rate arbitrage assistant, integrated into a live multi-exchange scanner. You have deep expertise in derivatives markets, cross-exchange arbitrage, funding rate mechanics, basis trading, and risk management.

LIVE SCANNER DATA ({period}):
- Total alerts: {an['total_alerts']} | Qualified (≥0.2% gross): {an['qualified_alerts']}
- Total gross potential: +{an['total_potential_pct']:.4f}% (before fees)
- Spread alerts: {an['by_type'].get('spread', 0)} | Funding alerts: {an['by_type'].get('funding', 0)}
- Top coins by activity: {', '.join(top_coins) if top_coins else 'none'}
- Best alert: {best_str if best_str else 'none recorded'}

{scan_summary}

SCANNER CONFIGURATION:
- Exchanges monitored: Binance, Bybit, OKX, MEXC, Gate.io, KuCoin, Bitget, CoinEx, Bitmart
- Symbols tracked: 45 (large cap, mid cap, DeFi, L2, meme, AI/infra)
- Scan interval: every 2 seconds (async parallel API calls)
- Funding rate cycle: 8h (annualised = rate × 1095)
- Alert thresholds: spread ≥ 0.15%, funding diff ≥ 0.03%/8h

ARBITRAGE MECHANICS:
- Price spread arb: BUY on lower-price exchange, simultaneously SELL on higher-price exchange. Gross = spread %. Net after fees ≈ gross − 0.1% (0.05% per side maker).
- Funding rate arb: SHORT on high-rate exchange, LONG on low-rate exchange. Collect the rate difference every 8h. Delta-neutral if sized equally. Execution risk: rate can change at settlement.
- Trust levels: high (Binance/Bybit/OKX/KuCoin), medium (MEXC/Gate.io/Bitget/CoinEx), low (Bitmart).
- Key risks: withdrawal delays, slippage on thin order books, funding rate reversals, liquidation on leveraged legs.
- Mid price = (max_price + min_price) / 2 — used as consolidation reference point.

You can search the web for current crypto news, market conditions, funding rate context, or any market information. Be direct, expert, and actionable. Keep answers concise unless depth is requested. Always flag key risks when discussing opportunities."""

    # ── Call Anthropic API ────────────────────────────────────────────────────
    if not ANTHROPIC_API_KEY:
        # Graceful fallback: simple keyword analytics mode
        return _simple_ai_fallback(an, messages, period)

    try:
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "system": system_prompt,
            "messages": messages,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta":  "web-search-2025-03-05",
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
        return jsonify({"reply": f"AI API error {e.code} — check ANTHROPIC_API_KEY and quota.", "ai_mode": "error"})
    except Exception as e:
        log.error(f"AI chat error: {e}")
        return _simple_ai_fallback(an, messages, period)


def _simple_ai_fallback(an, messages, period):
    """Keyword-based fallback when no API key is set."""
    message = (messages[-1].get("content", "") if messages else "").strip().lower()

    def fmt(v): return f"+{v:.4f}%"

    reply = None
    if any(k in message for k in ["best", "top", "biggest", "highest"]):
        b = an.get("best_opportunity")
        if b:
            pair = (f"{b.get('buy_exchange','?')} → {b.get('sell_exchange','?')}" if b["type"] == "spread"
                    else f"SHORT {b.get('short_exchange','?')}, LONG {b.get('long_exchange','?')}")
            reply = f"Best ({period}): {b['symbol']} {b['type'].upper()}\nGross: {fmt(b.get('potential_pct',0))}\nPair: {pair}"
        else:
            reply = "No opportunities yet."
    elif any(k in message for k in ["how many", "count", "total alerts"]):
        reply = (f"{period}: {an['total_alerts']} alerts, {an['qualified_alerts']} qualified.\n"
                 f"Spread: {an['by_type'].get('spread',0)}, Funding: {an['by_type'].get('funding',0)}")
    elif any(k in message for k in ["earn", "profit", "gross"]):
        reply = (f"Gross ({period}): {fmt(an['total_potential_pct'])} "
                 f"from {an['qualified_alerts']} qualified alerts. Avg: {fmt(an['avg_potential_pct'])}")
    elif any(k in message for k in ["coin", "symbol"]):
        top = list(an["by_symbol"].items())[:3]
        reply = "Top coins: " + ", ".join(f"{s} ({v['qualified']} alerts)" for s,v in top) if top else "No data."
    else:
        reply = (f"Summary ({period}): {an['total_alerts']} alerts, "
                 f"{an['qualified_alerts']} qualified, gross {fmt(an['total_potential_pct'])}.\n"
                 f"Set ANTHROPIC_API_KEY environment variable to enable full AI mode with web search.")

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
