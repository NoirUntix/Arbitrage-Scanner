"""
Arbitrage Scanner Web Server  —  python server.py  →  http://localhost:5000
"""

import asyncio, json, threading, time, logging, urllib.request
from flask import Flask, jsonify, send_from_directory, request
from scanner import scan_all, EXCHANGE_META, WITHDRAWAL_STATUS
from history import record_alerts, load_range, compute_analytics

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

app          = Flask(__name__, static_folder="static")
_cache       = {}
_last_scan   = 0.0
_scanning    = False
_scan_count  = 0
MIN_INTERVAL = 2.0


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
            log.info(f"Scan #{_scan_count} in {elapsed}s — {len(data)} coins")
            try: record_alerts(data)
            except Exception as he: log.warning(f"History: {he}")
        except Exception as e:
            log.error(f"Scan error: {e}")
        finally:
            _scanning = False
        wait = max(0, MIN_INTERVAL - (time.time() - t0))
        if wait: time.sleep(wait)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    return jsonify({"cache": _cache, "scanning": _scanning,
                    "scan_interval": MIN_INTERVAL, "last_scan": _last_scan, "scan_count": _scan_count})

@app.route("/api/exchanges")
def api_exchanges():
    return jsonify({ex: {**meta, "withdrawal": WITHDRAWAL_STATUS.get(ex, {"status":"unknown","note":"—"})}
                    for ex, meta in EXCHANGE_META.items()})

@app.route("/api/analytics")
def api_analytics():
    period      = request.args.get("period", "day")
    coin        = request.args.get("coin", None)  or None
    type_filter = request.args.get("type", None)  or None
    days        = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    records     = load_range(days)
    analytics   = compute_analytics(records, coin_filter=coin, type_filter=type_filter)
    return jsonify({"period": period, "days": days, "coin_filter": coin, **analytics})

@app.route("/api/news")
def api_news():
    headlines = []
    try:
        req = urllib.request.Request(
            "https://cryptopanic.com/api/v1/posts/?auth_token=public&kind=news&filter=important&currencies=BTC,ETH,SOL",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        for item in data.get("results", [])[:20]:
            headlines.append({"title": item.get("title",""), "url": item.get("url",""),
                               "source": item.get("source",{}).get("title",""),
                               "published": item.get("published_at",""),
                               "currencies": [c["code"] for c in item.get("currencies",[])],
                               "kind": item.get("kind","news")})
    except Exception as e:
        log.warning(f"News: {e}")
    static = [
        {"title":"CoinDesk — Arbitrage & Derivatives",   "url":"https://www.coindesk.com/search?s=arbitrage",        "source":"CoinDesk",   "kind":"search"},
        {"title":"The Block — Perpetual Futures",         "url":"https://www.theblock.co/search#q=perpetual+futures", "source":"The Block",  "kind":"search"},
        {"title":"Decrypt — Funding Rate News",           "url":"https://decrypt.co/search?q=funding+rate",           "source":"Decrypt",    "kind":"search"},
        {"title":"CryptoPanic — Live Feed",               "url":"https://cryptopanic.com",                            "source":"CryptoPanic","kind":"tool"},
        {"title":"Coinglass — Funding Rates",             "url":"https://www.coinglass.com/FundingRate",              "source":"Coinglass",  "kind":"tool"},
        {"title":"Coinglass — Liquidation Heatmap",       "url":"https://www.coinglass.com/LiquidationData",          "source":"Coinglass",  "kind":"tool"},
    ]
    return jsonify({"headlines": headlines, "static_links": static})


@app.route("/api/ai_chat", methods=["POST"])
def api_ai_chat():
    """
    Simple rule-based AI assistant for analytics questions.
    Reads current analytics data + history to answer questions.
    """
    body    = request.get_json(force=True) or {}
    message = body.get("message", "").strip().lower()
    period  = body.get("period", "day")
    coin    = body.get("coin", None) or None

    days    = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    records = load_range(days)
    an      = compute_analytics(records, coin_filter=coin)

    def fmt_pct(v): return f"+{v:.4f}%"
    def fmt_ts(ts):
        return __import__('datetime').datetime.fromtimestamp(ts, tz=__import__('datetime').timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    reply = None

    # ── Pattern matching ──────────────────────────────────────────────────────

    if any(k in message for k in ["best", "top", "biggest", "highest", "maximum"]):
        if an["best_opportunity"]:
            b = an["best_opportunity"]
            pair = f"{b.get('buy_exchange','?')} → {b.get('sell_exchange','?')}" if b["type"]=="spread" else f"{b.get('long_exchange','?')} / {b.get('short_exchange','?')}"
            reply = (f"Best opportunity ({period}): **{b['symbol']}** {b['type'].upper()}\n"
                     f"• Gross: {fmt_pct(b.get('potential_pct',0))}\n"
                     f"• Pair: {pair}\n"
                     f"• Trust: {b.get('trust_level','?')}\n"
                     f"• Time: {fmt_ts(b['ts'])}")
        else:
            reply = f"No opportunities recorded for {period} yet."

    elif any(k in message for k in ["how many", "count", "total alert", "number of"]):
        reply = (f"**{period.capitalize()}** stats:\n"
                 f"• Total alerts: {an['total_alerts']}\n"
                 f"• Qualified (≥0.2%): {an['qualified_alerts']}\n"
                 f"• Spread alerts: {an['by_type'].get('spread',0)}\n"
                 f"• Funding alerts: {an['by_type'].get('funding',0)}")

    elif any(k in message for k in ["earn", "profit", "gross", "total", "potential", "made"]):
        reply = (f"**Gross potential** ({period}):\n"
                 f"• Total qualified alerts (≥0.2%): {an['qualified_alerts']} of {an['total_alerts']}\n"
                 f"• Total gross if all executed: {fmt_pct(an['total_potential_pct'])}\n"
                 f"• Average per qualified alert: {fmt_pct(an['avg_potential_pct'])}\n"
                 f"⚠ Gross before fees. Est. fees ~0.05-0.10% per side.")

    elif any(k in message for k in ["which coin", "best coin", "top coin", "most active"]):
        if an["by_symbol"]:
            top = list(an["by_symbol"].items())[:3]
            lines = [f"**Top coins** ({period}):"]
            for sym, v in top:
                lines.append(f"• {sym}: {v['qualified']} qualified alerts, gross {fmt_pct(v['total_pct'])}")
            reply = "\n".join(lines)
        else:
            reply = "No coin data yet."

    elif any(k in message for k in ["exchange", "pair", "which exchange"]):
        if an["by_exchange_pair"]:
            top = list(an["by_exchange_pair"].items())[:3]
            lines = [f"**Top exchange pairs** ({period}):"]
            for pair, v in top:
                lines.append(f"• {pair}: {v['count']} alerts")
            reply = "\n".join(lines)
        else:
            reply = "No exchange pair data yet."

    elif any(k in message for k in ["trust", "safe", "risky", "reliable"]):
        bt = an["by_trust"]
        reply = (f"**Trust breakdown** ({period}):\n"
                 f"• 🟢 High trust: {bt.get('high',0)} alerts\n"
                 f"• 🟡 Medium trust: {bt.get('medium',0)} alerts\n"
                 f"• 🔴 Low trust: {bt.get('low',0)} alerts\n"
                 f"Recommendation: Focus on high-trust pairs (Binance, Bybit, OKX) for actual execution.")

    elif any(k in message for k in ["spread", "price spread", "price diff"]):
        spread_recs = [r for r in an["records"] if r["type"]=="spread"]
        if spread_recs:
            best_s = max(spread_recs, key=lambda r: r.get("potential_pct",0))
            reply = (f"**Spread alerts** ({period}): {len(spread_recs)}\n"
                     f"• Best: {best_s['symbol']} {fmt_pct(best_s.get('potential_pct',0))}\n"
                     f"  {best_s.get('buy_exchange','?')} → {best_s.get('sell_exchange','?')}\n"
                     f"• Avg spread: {fmt_pct(sum(r.get('potential_pct',0) for r in spread_recs)/len(spread_recs))}")
        else:
            reply = f"No spread alerts recorded for {period}."

    elif any(k in message for k in ["funding", "rate"]):
        fund_recs = [r for r in an["records"] if r["type"]=="funding"]
        if fund_recs:
            best_f = max(fund_recs, key=lambda r: r.get("potential_pct",0))
            reply = (f"**Funding alerts** ({period}): {len(fund_recs)}\n"
                     f"• Best: {best_f['symbol']} {fmt_pct(best_f.get('potential_pct',0))}/8h cycle\n"
                     f"  SHORT {best_f.get('short_exchange','?')}  LONG {best_f.get('long_exchange','?')}\n"
                     f"• Annual est: {fmt_pct(best_f.get('annual_diff_pct',0))}")
        else:
            reply = f"No funding alerts recorded for {period}."

    elif any(k in message for k in ["last", "recent", "latest"]):
        recs = an["records"][:3]
        if recs:
            lines = [f"**Last {len(recs)} alerts** ({period}):"]
            for r in recs:
                pair = f"{r.get('buy_exchange','?')}→{r.get('sell_exchange','?')}" if r["type"]=="spread" else f"{r.get('long_exchange','?')}/{r.get('short_exchange','?')}"
                lines.append(f"• [{fmt_ts(r['ts'])}] {r['symbol']} {r['type']} {fmt_pct(r.get('potential_pct',0))} — {pair}")
            reply = "\n".join(lines)
        else:
            reply = "No recent alerts."

    elif any(k in message for k in ["recommend", "should i", "advice", "tip", "suggest"]):
        bt = an["by_trust"]
        high_pct = bt.get("high",0) / max(an["total_alerts"],1) * 100
        best_coin = list(an["by_symbol"].keys())[0] if an["by_symbol"] else "N/A"
        reply = (f"**Recommendations** based on {period} data:\n"
                 f"• {high_pct:.0f}% of alerts involve high-trust exchanges — good signal\n"
                 f"• Most active coin: {best_coin}\n"
                 f"• Use 'Stable' filter to only trade pairs visible for >15s\n"
                 f"• For funding arb: enter before funding settlement (00:00, 08:00, 16:00 UTC)\n"
                 f"• For spread arb: requires automated bot — gaps close in <1s typically\n"
                 f"• Always verify withdrawal status before executing cross-exchange arb")

    elif any(k in message for k in ["fee", "cost", "net profit"]):
        qualified = an["qualified_alerts"]
        gross     = an["total_potential_pct"]
        est_fees  = qualified * 0.10  # ~0.1% round trip per trade
        net       = round(gross - est_fees, 4)
        reply = (f"**Fee estimate** ({period}):\n"
                 f"• Gross (≥0.2% alerts): {fmt_pct(gross)}\n"
                 f"• Est. fees ({qualified} trades × ~0.10%): -{est_fees:.3f}%\n"
                 f"• Est. net: {'+' if net>0 else ''}{net:.4f}%\n"
                 f"⚠ Actual fees vary by exchange. Binance/Bybit: ~0.02-0.05% per side.")

    elif any(k in message for k in ["hello", "hi", "help", "what can", "what do"]):
        reply = ("**ARB Scanner AI Assistant**\n\n"
                 "I can answer questions about your analytics data. Try:\n"
                 "• \"What's the best opportunity today?\"\n"
                 "• \"How many alerts this week?\"\n"
                 "• \"How much could I have earned this month?\"\n"
                 "• \"Which coin is most active?\"\n"
                 "• \"Which exchange pairs appear most?\"\n"
                 "• \"Show me last 3 alerts\"\n"
                 "• \"Are my alerts mostly high trust?\"\n"
                 "• \"What are the fees?\"\n"
                 "• \"Recommendations?\"\n\n"
                 "Use the period buttons (Today/Week/Month) and coin filter to narrow context.")
    else:
        # Fallback — show summary
        reply = (f"**Summary** ({period}{' · '+coin if coin else ''}):\n"
                 f"• Total alerts: {an['total_alerts']}\n"
                 f"• Qualified (≥0.2%): {an['qualified_alerts']}\n"
                 f"• Total gross: {fmt_pct(an['total_potential_pct'])}\n"
                 f"• Best: {an['best_opportunity']['symbol'] if an.get('best_opportunity') else 'N/A'}\n\n"
                 f"Try asking: 'best opportunity', 'how much earned', 'which coin', 'recommendations'")

    return jsonify({"reply": reply, "period": period, "coin": coin})


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    t = threading.Thread(target=background_scanner, daemon=True)
    t.start()
    print("\n🚀  Arbitrage Scanner → http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
