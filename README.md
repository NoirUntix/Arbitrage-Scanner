# ARB SCANNER — Perpetual Futures Arbitrage Monitor

Monitors **Binance, Bybit, OKX, MEXC, Gate.io, KuCoin, Bitget, CoinEx, Bitmart** for:
- Price spreads between identical perpetual futures contracts across exchanges
- Funding rate differences (delta-neutral arbitrage opportunities)
- Long-lived or structurally anomalous pairs with automatic unstable detection
- Full pair lifecycle management: Exclude → Unstable → Ban, with persistent IDs and audit logs

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
Without this, the AI assistant runs in keyword-only fallback mode. With it, you get a full LLM with live web search, access to all log files, conversational memory, and arbitrage expertise.

### 3. Run the server
```bash
python server.py
```

### 4. Open the dashboard
```
http://localhost:5000
```

---

## File structure

| File | Description |
|---|---|
| `scanner.py` | Async fetcher — hits all 9 exchange APIs in parallel |
| `server.py` | Flask server — scan loop, REST API, auto-unstable detection, AI endpoint |
| `history.py` | Alert logger — IDs, deduplication, pair status, analytics, log file access |
| `log_manager.py` | Standalone log/state utility (scripting and direct access) |
| `static/index.html` | Dashboard — 200ms polling, all UI logic |
| `data/history_YYYY-MM-DD.json` | Daily alert records |
| `data/coin_ids.json` | Permanent COIN-XXXXX IDs per symbol+exchange pair |
| `data/pair_status.json` | Persistent unstable/banned status for all marked pairs |
| `data/analytics_excluded.json` | Coins manually excluded from analytics only |
| `alert_logs.txt` | Full alert session log (NEW/ENDED with alert IDs + coin IDs) |
| `banned_coins.log` | Audit trail of all ban events |
| `unstable_coins.log` | Audit trail of all unstable events (system and manual) |
| `client_actions.log` | Every user action — tab switches, excludes, bans, analytics changes |

---

## Scan cycle

Every **2 seconds**, `scanner.py` fires **405 async HTTP calls** in parallel (45 symbols × 9 exchanges). Each call has a 4s timeout; failures return `None` and never block other calls. Results are cached in memory and served to the frontend. After each scan the server also runs `_auto_mark_unstable_if_needed()` which checks every pair against the unstable threshold.

---

## Dashboard tabs

### Price Spreads
Compares the last price of the same perpetual contract across all exchanges.

- **Spread %** = `(max_price − min_price) / min_price × 100`
- **Mid Price $** = `(max_price + min_price) / 2` — reference price for sizing both legs of a trade
- Spread alert threshold: **≥ 0.15%**
- **⊗ button** (red circle, leftmost column) — manually excludes a pair. It immediately disappears from this view, moves to the Unstable tab with a **Manual Removal** badge, is recorded server-side, and all its historical alerts are excluded from analytics. This action is **permanent** — there is no restore to main view.
- Exchange filter is **3-state**: click once = include only (green), again = exclude (red), again = neutral
- **Freeze** — locks the current snapshot; the server keeps scanning but the table stops updating until unfrozen

### Funding Rates
Current funding rate for each symbol on each exchange in a matrix view.

- Green = negative rate (longs receive payment), Red = positive rate (shorts receive payment)
- Toggle individual exchanges on/off with the exchange chips at the top

### ⚡ Opportunities
Funding rate arbitrage pairs sorted by diff (highest first).

- SHORT the exchange with the highest rate, LONG the exchange with the lowest
- Shows per-cycle (8h) diff and annualised estimate
- Alert threshold: diff ≥ 0.03%/8h
- **Freeze** — locks the current card grid independently of the Spreads freeze

### Unstable
Pairs that have been moved out of the main view — either by the system or manually. **Pairs stay here permanently; there is no restore to main view.**

Two removal types are shown:
- **Manual Removal** (purple badge) — you clicked the ⊗ button
- **System Removal** (red badge) — the system moved the pair automatically because its spread exceeded **1.0%**

All pairs in this tab are excluded from analytics calculations. The only action available is **⊗ Ban permanently**, which moves the pair to the Ban List.

**Locked** (amber badge) = spread >2% persisting >5 minutes. Shown inside the Unstable tab.

### Ban List
Permanently hidden pairs. Banned pairs are invisible in all live views and all their historical alerts are excluded from analytics by coin ID — even alerts that fired before the ban.

- Shows spread at time of ban, mid price, trust level, and exact UTC timestamp
- **↩ Unban to Unstable** — removes from ban list, moves back to Unstable tab (not to main view)
- Coin IDs make exclusion retroactive: if ALT-00042 fired for COIN-00007 before you banned it, that alert is excluded from Total Gross

### Analytics
Historical alert records with full filtering.

- **Time period**: Today / This Week / This Month
- **Coin filter**: dropdown of all coins seen this period
- **Type filter**: All / Spread / Funding
- **UTC time range**: filter records to a specific time window within the day
- **By Coin table**: shows total alerts, qualified count, total gross, and best alert per coin. Each row has:
  - **× button** (red) — exclude this coin from analytics only (hides from stats, doesn't affect live view)
  - **↩ button** (green) — re-include a previously excluded coin
  - **Status badge** — `Banned`, `Manual Removal`, or `System Removal` on affected rows; dimmed opacity
- **Alert Records**: every recorded alert with its ID, coin, type, pair, gross %, and trust level. Excluded rows are dimmed and tagged with their removal reason.

**Qualified alerts** = alerts where `potential_pct ≥ 0.80%`. Only qualified alerts count toward **Total Gross**. Fees are approximately 0.10% round-trip (0.05% per side maker), so net ≈ gross − 0.10%.

Stats update automatically whenever you exclude/include a coin or the analytics endpoint is refreshed.

### Exchange Health
Live API health checks per exchange, refreshed every **60 seconds** in a background thread.

- Checks: API ping, futures ticker, BTC order book bid (liquidity probe)
- Shows: trust level, operational status, last check time, notes
- Direct links to the exchange's futures trading page and status/support page

### AI Arbitrage Assistant
A full conversational LLM (Claude Sonnet via Anthropic API) with:

- **Live scanner context** — top spreads, top funding diffs, and analytics summary injected automatically into every message
- **Log file access** — the AI receives the last 30–50 lines of `alert_logs.txt`, `banned_coins.log`, `unstable_coins.log`, and `client_actions.log` on every call
- **Banned/unstable state** — full list of current banned and unstable pairs with coin IDs injected
- **Web search** — can search for current crypto news, market conditions, exchange events in real time
- **Conversation memory** — full message history sent on each turn, so context is maintained throughout the session
- **× Clear** — resets conversation history
- Mode indicator: `AI + Web` (full mode with API key) or `Basic` (keyword fallback without key)

Without `ANTHROPIC_API_KEY`, the assistant answers basic keyword queries about counts, gross, and top opportunities using local data only.

---

## Pair lifecycle

```
Main view (Price Spreads)
    │
    ├─ System: spread > 1.0% ────────────────► Unstable tab [System Removal — red]
    │
    └─ Manual: click ⊗ button ───────────────► Unstable tab [Manual Removal — purple]
                                                      │
                                               ⊗ Ban permanently
                                                      │
                                               Ban List tab
                                                      │
                                          ↩ Unban to Unstable
                                          (never back to main view)
```

Status is **one-way and never downgraded**: normal → unstable → banned. Once a pair is banned, calling unstable on it has no effect.

---

## ID system

Every pair gets a **permanent Coin ID** (e.g. `COIN-00007`) assigned on first sighting, stored in `data/coin_ids.json`. This ID never changes even if the pair disappears and reappears.

Every alert gets a **unique Alert ID** (e.g. `ALT-00042`) assigned when it first fires. Both IDs are stamped on every log line and every JSON record.

When you ban a pair, the server excludes **all historical alert records matching that coin ID** from analytics — including alerts that fired before the ban happened. This prevents past alerts from inflating your gross stats after you've identified a pair as problematic.

---

## Alert deduplication

When an alert fires it enters an **active** state. While active, each scan updates `max_pct`, `min_pct`, and `current_pct` without creating new records. When the pair disappears from the scan for more than **8 seconds** (grace period), it is marked ended and a **10-minute cooldown** starts. No new alert record for the same pair can be written during the cooldown.

Pairs marked as unstable or banned are skipped entirely in `record_alerts()` — no new records are written for them at all.

---

## Analytics qualification

| Threshold | Value | Notes |
|---|---|---|
| Minimum gross (qualified) | **0.80%** | Alerts below this don't count toward Total Gross |
| Approx. round-trip fees | ~0.10% | 0.05% maker per side |
| Approx. net (qualified) | gross − 0.10% | Per qualified alert |
| Annualised funding | rate × 1095 | 365 days × 3 cycles/day |

---

## Log files

| File | What it records |
|---|---|
| `alert_logs.txt` | Every NEW and ENDED alert with `[ALT-XXXXX]` and `[COIN-XXXXX]` stamps, plus session start/end |
| `banned_coins.log` | Every ban event: coin ID, symbol, pair, spread at ban, who banned it, timestamp |
| `unstable_coins.log` | Every unstable event: coin ID, symbol, pair, spread, removal type (system/manual), timestamp |
| `client_actions.log` | Tab switches, manual excludes, bans, analytics exclusions — every user action timestamped |

All logs are append-only. The AI assistant reads the last 30–50 lines of each on every message.

---

## REST API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/data` | Latest scan cache (prices, spreads, funding rates) |
| GET | `/api/exchanges` | Exchange metadata + live health status |
| GET | `/api/analytics?period=day&coin=BTC&type=spread` | Analytics for the period |
| GET | `/api/pair_statuses` | All unstable/banned pairs with coin IDs and timestamps |
| GET | `/api/coin_ids` | Full coin ID registry |
| GET | `/api/logs/<type>?lines=100` | Read log tail (`alerts`, `banned`, `unstable`, `client`) |
| POST | `/api/action/unstable` | Mark a pair as unstable (`{symbol, min_ex, max_ex, spread_pct, by}`) |
| POST | `/api/action/ban` | Mark a pair as banned (`{symbol, min_ex, max_ex, spread_pct, by}`) |
| POST | `/api/action/exclude_coin` | Exclude/include a coin from analytics (`{symbol, action}`) |
| POST | `/api/action/log` | Write a client action to `client_actions.log` (`{action, target, details}`) |
| POST | `/api/ai_chat` | Send a message to the AI assistant (`{messages, period, coin}`) |
| GET | `/api/news` | CryptoPanic headlines + static resource links |
| GET | `/api/active_alerts` | Currently active and unstable alerts from the live session |

---

## Customisation

Edit `scanner.py`:
```python
SYMBOLS = [...]              # Add/remove coins (currently 45)
ALERT_SPREAD_PCT  = 0.15     # Spread alert threshold
ALERT_FUNDING_DIFF = 0.03    # Funding rate diff alert threshold
```

Edit `server.py`:
```python
MIN_INTERVAL             = 2.0   # Seconds between scans
HEALTH_REFRESH_SEC       = 60    # Exchange health check interval
UNSTABLE_SPREAD_THRESHOLD = 1.0  # Spread % that triggers auto system-unstable
```

Edit `history.py`:
```python
DEDUP_TTL  = 600   # Cooldown (seconds) after alert ends before it can fire again
GRACE_TTL  = 8     # Seconds a pair can disappear before its alert is marked ended
MIN_GROSS  = 0.80  # Minimum gross % for an alert to count as qualified
```

---

## Exchange trust levels

| Trust | Exchanges | Implication |
|---|---|---|
| **High** | Binance, Bybit, OKX, KuCoin | Top liquidity, reliable withdrawals |
| **Medium** | MEXC, Gate.io, Bitget, CoinEx | Adequate liquidity, verify withdrawals per coin |
| **Low** | Bitmart | Known withdrawal delays, thin books — high risk for arb |

A pair's trust level is always the **worst** of the two legs. A Binance/Bitmart spread is **low** trust.

---

## Notes

- No API keys required for scanning — all public endpoints
- `ANTHROPIC_API_KEY` env var enables full AI mode (optional but recommended)
- KuCoin futures uses `USDTM` contracts (e.g. `BTCUSDTM`)
- Gate.io uses underscore format (e.g. `BTC_USDT`)
- Funding rates displayed as % per 8h cycle
- All timestamps are UTC throughout
- `data/` directory and all log files are created automatically on first run
