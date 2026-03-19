# ARB SCANNER — Perpetual Futures Arbitrage Monitor

Monitors **Binance, Bybit, OKX, MEXC, Gate.io, KuCoin, Bitget, CoinEx, Bitmart** for:
- Price spreads between identical perpetual futures contracts across exchanges
- Funding rate differences (delta-neutral arbitrage opportunities)
- Long-lived or structurally anomalous pairs (Unstable / Locked tracking)
- Manual pair management with Exclude, Ban, and Restore controls

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. (Optional) Enable full AI assistant
```bash
export ANTHROPIC_API_KEY=your_key_here
```
Without this, the AI assistant runs in keyword-only fallback mode. With it, you get a full LLM with live web search, market context, and conversational memory.

### 3. Run the server
```bash
python server.py
```

### 4. Open the dashboard
```
http://localhost:5000
```

---

## How it works

| Component | Description |
|---|---|
| `scanner.py` | Async fetcher — hits all 9 exchange APIs in parallel |
| `server.py` | Flask server with background scan loop + REST API endpoints |
| `history.py` | Alert logging, deduplication (10-min cooldown), session tracking |
| `static/index.html` | Dashboard — auto-refreshes every 200ms |

### Scan cycle
- Every **2 seconds**, `scanner.py` fetches prices + funding rates for 45 symbols × 9 exchanges = 405 API calls (async, ~2–4s total)
- Results cached and served to the dashboard

---

## Dashboard tabs

### Price Spreads
Compares the last price of the same perpetual contract across exchanges.
- **Spread %** = `(max_price − min_price) / min_price × 100`
- **Mid Price $** = `(max_price + min_price) / 2` — used as reference consolidation price
- Alert threshold: **≥ 0.15%**
- **⊗ button** (red circle, leftmost column) — manually excludes a pair from this view; pair moves to the **Unstable** tab labelled "Manual"
- Exchange filter is 3-state: click once = include only (green), again = exclude (red), again = neutral

### Funding Rates
Shows the current funding rate for each symbol on each exchange.
- Green = negative rate (longs get paid), Red = positive rate (shorts get paid)
- Toggle individual exchanges on/off with the exchange chips

### ⚡ Opportunities
Pairs the exchange with the **highest funding rate** (SHORT there) vs the one with the **lowest** (LONG there).
- Shows estimated per-cycle (8h) gain and annualised yield
- Alert threshold: diff ≥ 0.03% per cycle

### Unstable
Pairs that have been in the main view for **>15 minutes** (system), or were **manually excluded** via the ⊗ button.
- Each card shows whether removal was **Manual** (purple badge) or **System** (grey badge)
- **↩ Restore** — moves the pair back to the main Price Spreads view
- **⊗ Ban** — moves the pair to the Ban List permanently (hidden from all views)
- **Locked** (orange badge) = spread >2% persisting >5 minutes — excluded from Total Gross calculations

### Ban List *(new)*
Permanently hidden pairs. Useful for pairs with known data issues, token migrations, or fake arbitrage.
- Shows the spread % at time of ban, mid price, trust level, and ban timestamp
- **↩ Unban to Unstable** — removes from ban list and moves back to Unstable for review

### Analytics
Historical alert records with filtering by coin, type, and UTC time range.
- Total gross = sum of `potential_pct` for alerts ≥ 0.2% (before fees ~0.05–0.1% per side)
- Includes per-coin breakdown, hourly timeline, and individual alert records

### AI Assistant *(upgraded)*
Full conversational LLM assistant with:
- **Live scanner context** — current top spreads, funding diffs, and analytics summary injected automatically
- **Web search** — can look up current crypto news, market conditions, funding rate context
- **Conversation memory** — maintains history across messages in the same session
- **Clear button** (✕) — resets conversation history
- Mode shown in header: `AI + Web` (full mode) or `Basic` (fallback without API key)

### Exchange Health
Live API health checks run every 60 seconds per exchange. Shows trust level, operational status, order book probes, and direct links to futures trading and status pages.

---

## Pair lifecycle

```
Main view (Price Spreads)
    │
    ├─ Auto: alive >15min ──────────────────► Unstable tab (System badge)
    │
    ├─ Manual: click ⊗ button ─────────────► Unstable tab (Manual badge)
    │                                              │
    │                                    ┌─────────┴──────────┐
    │                                    ↓                    ↓
    │                             ↩ Restore               ⊗ Ban
    │                                    │                    │
    └────────────────────────────────────┘               Ban List tab
                                                              │
                                                      ↩ Unban to Unstable
```

---

## Mid Price (Consolidation)

The **Mid Price** column shows `(max_price + min_price) / 2` — the midpoint between the highest and lowest quoted price across exchanges. This is the reference price for sizing both legs of a spread arbitrage trade.

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
MIN_INTERVAL = 2.0         # Seconds between scans
HEALTH_REFRESH_SEC = 60    # Exchange health check interval
```

---

## Notes
- No API keys required for scanning — all public endpoints
- `ANTHROPIC_API_KEY` env var enables full AI assistant with web search (optional)
- KuCoin futures uses `USDTM` contracts (e.g. `BTCUSDTM`)
- Gate.io uses underscore format (e.g. `BTC_USDT`)
- Funding rates shown as % per 8h cycle; annualised = rate × 1095
- Alert deduplication: 10-minute cooldown after an alert ends before the same pair fires again
- History stored daily as JSON in `data/` directory; text log in `alert_logs.txt`
