"""
Crypto Perpetual Futures Arbitrage Scanner
Exchanges: Binance, Bybit, OKX, MEXC, Gate.io, KuCoin, Bitget, CoinEx, Bitmart
45 symbols tracked
"""

import asyncio
import aiohttp
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

SYMBOLS = [
    # Large cap
    "BTC", "ETH", "BNB", "SOL", "XRP",
    # Mid cap
    "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "MATIC", "LTC", "BCH", "ATOM", "UNI",
    "NEAR", "FTM", "INJ", "SEI", "TIA",
    # DeFi
    "AAVE", "CRV", "MKR", "SNX", "LDO",
    "GRT", "DYDX", "1INCH", "IMX", "RPL",
    # L2 & New
    "ARB", "OP", "SUI", "APT", "STX",
    # Meme & trending
    "SHIB", "PEPE", "WLD", "BLUR", "JTO",
    # AI & infra
    "FET", "RNDR", "PYTH", "ORDI", "STRK",
]

ALERT_SPREAD_PCT  = 0.15
ALERT_FUNDING_DIFF = 0.03

EXCHANGE_META = {
    "Binance": {"futures_url": "https://www.binance.com/en/futures/{symbol}USDT",          "withdraw_url": "https://www.binance.com/en/support/announcement/c-48",   "trust": "high"},
    "Bybit":   {"futures_url": "https://www.bybit.com/trade/usdt/{symbol}USDT",             "withdraw_url": "https://status.bybit.com",                               "trust": "high"},
    "OKX":     {"futures_url": "https://www.okx.com/trade-swap/{symbol}-usdt-swap",         "withdraw_url": "https://www.okx.com/support/hc/en-us",                   "trust": "high"},
    "MEXC":    {"futures_url": "https://futures.mexc.com/exchange/{symbol}_USDT",           "withdraw_url": "https://support.mexc.com",                               "trust": "medium"},
    "Gate.io": {"futures_url": "https://www.gate.io/futures/USDT/{symbol}_USDT",            "withdraw_url": "https://www.gate.io/support",                            "trust": "medium"},
    "KuCoin":  {"futures_url": "https://futures.kucoin.com/trade/{symbol}USDTM",            "withdraw_url": "https://www.kucoin.com/support",                         "trust": "high"},
    "Bitget":  {"futures_url": "https://www.bitget.com/futures/usdt/{symbol}USDT",          "withdraw_url": "https://www.bitget.com/support",                         "trust": "medium"},
    "CoinEx":  {"futures_url": "https://www.coinex.com/futures/{symbol}USDT",               "withdraw_url": "https://www.coinex.com/support",                         "trust": "medium"},
    "Bitmart": {"futures_url": "https://futures.bitmart.com/en?symbol={symbol}USDT",        "withdraw_url": "https://support.bitmart.com",                            "trust": "low"},
}

WITHDRAWAL_STATUS = {
    "Binance": {"status": "normal",  "note": "All systems operational"},
    "Bybit":   {"status": "normal",  "note": "All systems operational"},
    "OKX":     {"status": "normal",  "note": "All systems operational"},
    "MEXC":    {"status": "caution", "note": "Some coins may have delayed withdrawals — verify before trading"},
    "Gate.io": {"status": "normal",  "note": "All systems operational"},
    "KuCoin":  {"status": "normal",  "note": "All systems operational"},
    "Bitget":  {"status": "caution", "note": "Lower liquidity on some pairs — check order book depth"},
    "CoinEx":  {"status": "caution", "note": "Lower volume exchange — verify withdrawal availability per coin"},
    "Bitmart": {"status": "warning", "note": "Known withdrawal delays reported — high risk for arbitrage"},
}


@dataclass
class PerpData:
    exchange: str
    symbol:   str
    price:    Optional[float]
    funding_rate: Optional[float]
    funding_interval_h: int  = 8
    timestamp:  float        = 0.0
    trust:      str          = "medium"


# ─── Exchange fetchers (all return PerpData, never raise) ─────────────────────

async def fetch_binance(s, sym):
    t = sym + "USDT"
    try:
        async with s.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        price = float(d["price"])
        async with s.get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={t}&limit=1", timeout=aiohttp.ClientTimeout(total=4)) as r:
            df = await r.json()
        fr = float(df[0]["fundingRate"]) * 100
        return PerpData("Binance", sym, price, fr, 8, time.time(), "high")
    except Exception as e:
        log.debug(f"Binance {sym}: {e}")
        return PerpData("Binance", sym, None, None, 8, 0, "high")


async def fetch_bybit(s, sym):
    t = sym + "USDT"
    try:
        async with s.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        item = d["result"]["list"][0]
        return PerpData("Bybit", sym, float(item["lastPrice"]), float(item["fundingRate"]) * 100, 8, time.time(), "high")
    except Exception as e:
        log.debug(f"Bybit {sym}: {e}")
        return PerpData("Bybit", sym, None, None, 8, 0, "high")


async def fetch_okx(s, sym):
    t = sym + "-USDT-SWAP"
    try:
        async with s.get(f"https://www.okx.com/api/v5/market/ticker?instId={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        price = float(d["data"][0]["last"])
        async with s.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            df = await r.json()
        fr = float(df["data"][0]["fundingRate"]) * 100
        return PerpData("OKX", sym, price, fr, 8, time.time(), "high")
    except Exception as e:
        log.debug(f"OKX {sym}: {e}")
        return PerpData("OKX", sym, None, None, 8, 0, "high")


async def fetch_mexc(s, sym):
    t = sym + "_USDT"
    try:
        async with s.get(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        data = d["data"]
        return PerpData("MEXC", sym, float(data["lastPrice"]), float(data["fundingRate"]) * 100, 8, time.time(), "medium")
    except Exception as e:
        log.debug(f"MEXC {sym}: {e}")
        return PerpData("MEXC", sym, None, None, 8, 0, "medium")


async def fetch_gate(s, sym):
    t = sym + "_USDT"
    try:
        async with s.get(f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        price = float(d[0]["last"])
        async with s.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            dc = await r.json()
        fr = float(dc["funding_rate"]) * 100
        return PerpData("Gate.io", sym, price, fr, 8, time.time(), "medium")
    except Exception as e:
        log.debug(f"Gate.io {sym}: {e}")
        return PerpData("Gate.io", sym, None, None, 8, 0, "medium")


async def fetch_kucoin(s, sym):
    t = sym + "USDTM"
    try:
        async with s.get(f"https://api-futures.kucoin.com/api/v1/ticker?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        price = float(d["data"]["price"])
        async with s.get(f"https://api-futures.kucoin.com/api/v1/funding-rate/{t}/current", timeout=aiohttp.ClientTimeout(total=4)) as r:
            df = await r.json()
        fr = float(df["data"]["value"]) * 100
        return PerpData("KuCoin", sym, price, fr, 8, time.time(), "high")
    except Exception as e:
        log.debug(f"KuCoin {sym}: {e}")
        return PerpData("KuCoin", sym, None, None, 8, 0, "high")


async def fetch_bitget(s, sym):
    t = sym + "USDT_UMCBL"
    try:
        async with s.get(f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        price = float(d["data"]["last"])
        async with s.get(f"https://api.bitget.com/api/mix/v1/market/current-fundRate?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            df = await r.json()
        fr = float(df["data"]["fundingRate"]) * 100
        return PerpData("Bitget", sym, price, fr, 8, time.time(), "medium")
    except Exception as e:
        log.debug(f"Bitget {sym}: {e}")
        return PerpData("Bitget", sym, None, None, 8, 0, "medium")


async def fetch_coinex(s, sym):
    t = sym + "USDT"
    try:
        async with s.get(f"https://api.coinex.com/v2/futures/ticker?market={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        item = d["data"][0]
        price = float(item["last"])
        fr = float(item.get("funding_rate_last", 0)) * 100
        return PerpData("CoinEx", sym, price, fr, 8, time.time(), "medium")
    except Exception as e:
        log.debug(f"CoinEx {sym}: {e}")
        return PerpData("CoinEx", sym, None, None, 8, 0, "medium")


async def fetch_bitmart(s, sym):
    t = sym + "USDT"
    try:
        async with s.get(f"https://api-cloud-v2.bitmart.com/contract/public/ticker?symbol={t}", timeout=aiohttp.ClientTimeout(total=4)) as r:
            d = await r.json()
        item = d["data"]["tickers"][0]
        price = float(item["last_price"])
        fr = float(item.get("funding_rate", 0)) * 100
        return PerpData("Bitmart", sym, price, fr, 8, time.time(), "low")
    except Exception as e:
        log.debug(f"Bitmart {sym}: {e}")
        return PerpData("Bitmart", sym, None, None, 8, 0, "low")


FETCHERS = [fetch_binance, fetch_bybit, fetch_okx, fetch_mexc,
            fetch_gate, fetch_kucoin, fetch_bitget, fetch_coinex, fetch_bitmart]


# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyze(results: list) -> dict:
    valid = [r for r in results if r.price is not None]
    if len(valid) < 2:
        return {}

    prices   = {r.exchange: r.price        for r in valid}
    fundings = {r.exchange: r.funding_rate  for r in valid if r.funding_rate is not None}
    trust    = {r.exchange: r.trust         for r in valid}

    max_ex = max(prices, key=prices.get)
    min_ex = min(prices, key=prices.get)
    spread_pct = ((prices[max_ex] - prices[min_ex]) / prices[min_ex]) * 100 if prices[min_ex] else 0
    spread_risk = trust.get(max_ex, "medium") == "low" or trust.get(min_ex, "medium") == "low"

    funding_opps = []
    fx = list(fundings.items())
    for i in range(len(fx)):
        for j in range(i + 1, len(fx)):
            ex_a, fr_a = fx[i]; ex_b, fr_b = fx[j]
            diff = abs(fr_a - fr_b)
            if diff > 0:
                short_ex = ex_a if fr_a > fr_b else ex_b
                long_ex  = ex_b if fr_a > fr_b else ex_a
                t_s = trust.get(short_ex, "medium")
                t_l = trust.get(long_ex, "medium")
                risk = "low" if "low" in [t_s, t_l] else "medium" if "medium" in [t_s, t_l] else "high"
                funding_opps.append({
                    "short_exchange":   short_ex,
                    "long_exchange":    long_ex,
                    "diff_pct":         round(diff, 5),
                    "annual_diff_pct":  round(diff * (365 * 24 / 8), 2),
                    "alert":            diff >= ALERT_FUNDING_DIFF,
                    "trust_level":      risk,
                    "short_trust":      t_s,
                    "long_trust":       t_l,
                })
    funding_opps.sort(key=lambda x: x["diff_pct"], reverse=True)

    # All per-exchange prices for detail view
    price_list = [{"exchange": ex, "price": p, "trust": trust.get(ex, "medium")}
                  for ex, p in sorted(prices.items(), key=lambda x: x[1], reverse=True)]

    return {
        "spread_pct":            round(spread_pct, 4),
        "spread_alert":          spread_pct >= ALERT_SPREAD_PCT,
        "spread_risk":           spread_risk,
        "max_exchange":          max_ex,
        "min_exchange":          min_ex,
        "max_price":             prices[max_ex],
        "min_price":             prices[min_ex],
        "funding_opportunities": funding_opps[:6],
        "prices":                prices,
        "price_list":            price_list,
        "funding_rates":         fundings,
        "trust_map":             trust,
    }


async def scan_all() -> dict:
    connector = aiohttp.TCPConnector(limit=200, ssl=False, ttl_dns_cache=300)
    timeout   = aiohttp.ClientTimeout(total=5, connect=3)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks      = [f(session, sym) for sym in SYMBOLS for f in FETCHERS]
        all_results = await asyncio.gather(*tasks, return_exceptions=False)

    grouped: dict = {}
    for r in all_results:
        grouped.setdefault(r.symbol, []).append(r)

    output: dict = {}
    for sym, results in grouped.items():
        analysis = analyze(results)
        if analysis:
            output[sym] = {
                "symbol":     sym,
                "analysis":   analysis,
                "raw":        [asdict(r) for r in results],
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
    return output


if __name__ == "__main__":
    data = asyncio.run(scan_all())
    print(json.dumps(data, indent=2))
