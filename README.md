# ARB SCANNER — Perpetual Futures Arbitrage Monitor

Monitors **Bybit, OKX, MEXC, Gate.io, KuCoin** for:
- Price spreads between identical perpetual futures contracts
- Funding rate differences (arbitrage opportunities)

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the server
```bash
python server.py
```

### 3. Open the dashboard
```
http://localhost:5000
```

---

## How it works

| Component | Description |
|---|---|
| `scanner.py` | Async fetcher — hits all 5 exchange APIs in parallel |
| `server.py` | Flask server with background scan loop + `/api/data` endpoint |
| `static/index.html` | Dashboard — auto-refreshes every 15s |

### Scan cycle
- Every **15 seconds**, `scanner.py` fetches prices + funding rates for 15 symbols × 5 exchanges = 75 API calls (async, ~2–4s total)
- Results cached and served to the dashboard

---

## Dashboard tabs

### Price Spreads
Compares the last price of the same perpetual contract across exchanges.
- **Spread %** = (max_price - min_price) / min_price × 100
- Alert threshold: **≥ 0.15%**
- Filter by minimum spread, or show alerts only

### Funding Rates
Shows the current funding rate for each symbol on each exchange.
- Green = negative rate (longs get paid)
- Red = positive rate (shorts get paid)

### ⚡ Opportunities
Pairs the exchange with the **highest funding rate** (SHORT there) vs the one with the **lowest** (LONG there).
- Shows estimated per-cycle (8h) gain and annualised yield
- Alert threshold: diff ≥ 0.03% per cycle

---

## Customisation

Edit `scanner.py` to change:
```python
SYMBOLS = [...]            # Add/remove coins
ALERT_SPREAD_PCT = 0.15    # Spread alert threshold
ALERT_FUNDING_DIFF = 0.03  # Funding alert threshold
```

Edit `server.py` to change:
```python
SCAN_INTERVAL = 15         # Seconds between scans
```

---

## Notes
- No API keys required — all public endpoints
- KuCoin futures uses `USDTM` contracts (e.g. `BTCUSDTM`)
- Gate.io uses underscore format (e.g. `BTC_USDT`)
- Funding rates shown as % per 8h cycle
- Annualised = rate × (365 × 24 / 8) = rate × 1095
