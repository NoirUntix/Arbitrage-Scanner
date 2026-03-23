# Arbitrage Scanner — Full Issues & Improvements List

---

## BUGS

**Problem 1 — No "Restore to Main View" button in Unstable tab**
The Unstable tab only shows a "Ban permanently" button. Once a pair is manually excluded or auto-moved, there is no way to bring it back to the main spreads view. The server-side `mark_pair_status()` uses a one-way priority system (`normal < unstable < banned`) that never allows downgrading, and no `/api/action/restore` endpoint exists.
Fix: Add `restore_pair_to_normal(symbol, min_ex, max_ex)` function in `history.py` that deletes the record from `_pair_status` and removes the `coin_id` from `_excluded_ids`. Add `/api/action/restore` POST endpoint in `server.py`. Add "↩ Restore to Main" button inside `buildCard()` in `index.html` that clears the key from `manuallyExcluded`, `serverUnstableKeys`, `autoUnstableKeys`, adds it to `pinnedKeys`, and calls the restore endpoint.

---

**Problem 2 — System auto-moves pairs permanently despite user intent**
`_auto_mark_unstable_if_needed()` in `server.py` runs after every scan and permanently marks any pair with spread > 1% as unstable via `mark_pair_status()`. Because status is never downgraded, if a pair momentarily spikes above the threshold (common during volatile market events), it gets permanently removed from the main view even if the spike was temporary. There is no whitelist or pinned-pair protection.
Fix: Introduce `pinned_pairs` in `config.json` — a set of pair keys immune to auto-unstable. Add `cfg.is_pinned(key)` check at the top of `_auto_mark_unstable_if_needed()`. Add pin/unpin buttons in the UI and `/api/action/pin` / `/api/action/unpin` endpoints. This is Lifecycle State 4 (Pinned) alongside Normal / Unstable / Banned.

---

**Problem 3 — Frontend `rebuildAutoUnstable()` hides pinned pairs from main view without server consent**
In `index.html`, `rebuildAutoUnstable()` is called on every render and adds any spread > 1% pair to `autoUnstableKeys`, hiding it from the main view. It only checks `bannedKeys.has(k)` but not `pinnedKeys.has(k)`, so a pair the user deliberately pinned still gets hidden locally on the next scan.
Fix: Add `if (!bannedKeys.has(k) && !pinnedKeys.has(k))` condition inside `rebuildAutoUnstable()`. The `pinnedKeys` set is populated when the user clicks "Pin to Main" or when the `/api/pair_statuses` poll returns a pinned record.

---

**Problem 4 — Pairs that disappear from scan data do not appear in Unstable tab**
`renderUnstable()` iterates only over `allData` (the live scan cache). If a pair was manually excluded but the coin is no longer being actively quoted by an exchange (exchange down, contract delisted), it silently vanishes from the Unstable tab too. There is no persistent local record of manually excluded pairs that survived across scans.
Fix: Maintain a `persistentUnstable` Map in `index.html` that stores the last known metadata (symbol, exchanges, spread, removal type) for every excluded pair. Populate it when `manuallyExclude()` or `serverUnstableKeys` is updated. `renderUnstable()` merges `persistentUnstable` entries with the live `allData` entries, so disappeared pairs still show.

---

**Problem 5 — Race condition on `_active_alerts` between scanner thread and Flask thread**
`_active_alerts` and `_dedup` dicts in `history.py` are written by the background scanner thread inside `record_alerts()` and read by Flask request handlers (e.g. `/api/active_alerts`, `/api/ai_chat`). Python's GIL prevents hard corruption but does not protect against inconsistent mid-iteration state across dict resizing or compound operations.
Fix: Add `_alerts_lock = threading.RLock()` in `history.py`. Wrap all reads and writes to `_active_alerts` and `_dedup` inside `with _alerts_lock:` blocks in `record_alerts()`, `get_active_alerts()`, and any other function that accesses them.

---

**Problem 6 — Docstring in `history.py` contradicts actual logic**
The module docstring says `qualified = 0.20% ≤ pct < 0.80%` but the actual code has `MIN_GROSS = 0.80` and `MAX_GROSS = 100.0`, meaning qualified is `pct >= 0.80%` with no effective upper cap. The README is correct, the docstring is wrong. This would mislead anyone (including the bot) reading the code to understand the qualification threshold.
Fix: Update the docstring to `qualified = gross% >= MIN_GROSS (default 0.80%)`. Also remove the confusing `MAX_GROSS = 100.0` line and replace it with a comment explaining it is intentionally uncapped.

---

**Problem 7 — `log_manager.py` is dead code — full duplicate of `history.py`**
`log_manager.py` implements its own complete versions of coin ID management, banned/unstable pair tracking, analytics exclusions, and log reading — all of which are already implemented in `history.py`. It is never imported anywhere in `server.py`, `scanner.py`, or any other module. It creates confusion about which module is authoritative and wastes maintenance bandwidth.
Fix: Delete `log_manager.py` entirely. If any utility functions in it are not covered by `history.py`, port those specific functions over. Document in README that `history.py` is the single source of truth for all state management.

---

**Problem 8 — Bitget API v1 is deprecated**
`fetch_bitget()` in `scanner.py` calls `https://api.bitget.com/api/mix/v1/market/ticker` and `api/mix/v1/market/current-fundRate`. Bitget deprecated the v1 Mix API in mid-2024. These endpoints may return errors or stale/incorrect data silently (the fetcher catches all exceptions and returns `None` price, hiding the breakage).
Fix: Update to Bitget v2 endpoints: `GET /api/v2/mix/market/ticker?symbol={sym}USDT&productType=USDT-FUTURES` for price and `GET /api/v2/mix/market/current-fund-rate?symbol={sym}USDT&productType=USDT-FUTURES` for funding rate. Parse `data[0].lastPr` for price and `data[0].fundingRate` for the rate.

---

## ARCHITECTURE & PERFORMANCE

**Problem 9 — 200ms HTTP polling creates unnecessary server load**
The frontend polls `/api/data` every 200ms regardless of whether any data changed. With 9 exchanges × 45 symbols × 2 API calls each, the server is already doing ~810 async calls per 2-second scan cycle. Adding constant 200ms HTTP polling on top means ~300 extra requests per minute that mostly receive identical cached data.
Fix: Replace polling with WebSocket (use `flask-sock` library). Server pushes a diff message only when `_cache` is updated after each scan. Client receives the push, updates state, and re-renders. Fallback: add ETag/Last-Modified headers to `/api/data` so clients receive HTTP 304 Not Modified when data has not changed, reducing payload to ~150 bytes.

---

**Problem 10 — Coin ID file written to disk on every new pair**
`get_or_create_coin_id()` calls `_save_coin_ids()` (a full JSON file write) every time a new coin ID is created. During startup when many new pairs appear simultaneously, this can trigger dozens of sequential disk writes per second.
Fix: Debounce the save with a 500ms timer. Set a `_coin_id_dirty` flag on every new ID, start a `threading.Timer(0.5, _flush_coin_ids)` if one isn't already running. The timer writes to disk once after activity settles.

---

**Problem 11 — No net-profit calculation — gross % is misleading**
All analytics, alert records, and opportunity cards show only gross spread %. The dashboard does not subtract exchange maker fees, which vary per exchange (MEXC 0%, Binance 0.02%, Bitmart 0.04%). A MEXC↔Bitmart spread of 0.20% gross actually has round-trip fees of ~0.08%, netting only 0.12%. Users (and future bot) cannot make correct trade decisions from gross alone.
Fix: Add `fees` section to `config.json` with per-exchange maker fee percentages. Add `compute_net_spread(gross, long_ex, short_ex, fees)` function: `net = gross - 2*(fee_long + fee_short)`. Store `net_pct` on every alert record. Show net alongside gross in all UI cards and analytics tables.

---

**Problem 12 — No composite opportunity score**
Currently all opportunities are sorted only by raw spread % or funding diff %. A 0.8% spread on a Bitmart/MEXC pair with stale prices and low liquidity ranks higher than a 0.6% spread on Binance/KuCoin with normal health and confirmed depth. The bot cannot use a single reliable signal for automated entry.
Fix: Implement `compute_score(net_pct, type, trust, health_a, health_b, alive_s, any_stale) → int 0–100`. Components: net value 0–40 pts, trust level 0–25 pts, exchange health 0–20 pts, duration stability 0–15 pts, stale price penalty −15 pts. Store score on every alert record. Expose score in `/api/opportunities` for bot consumption.

---

**Problem 13 — Stale price detection is missing**
Some exchanges cache API responses and return the same price for 10+ consecutive scans. The scanner treats this as a valid live price and generates spread alerts between a stale price and a real one. This produces phantom opportunities that don't exist in the real order book.
Fix: Add `_price_streak` dict in `scanner.py` tracking `(exchange, symbol) → [last_price, consecutive_same_count]`. After each fetch, if price equals previous price for >= `stale_streak` scans (default 5, configurable), set `is_stale=True` on the `PerpData` object. Propagate `any_stale` into analysis and alert records. Apply −15 point score penalty and show `[STALE]` badge in UI.

---

**Problem 14 — No funding rate settlement countdown**
Funding is paid every 8 hours (00:00, 08:00, 16:00 UTC). Opportunity cards show the funding rate but not how much time is left until the next settlement. Opening a delta-neutral position 2 minutes before settlement means you receive (or pay) the full 8h funding immediately, but then need to hold through two more periods to break even on fees. Without a countdown, users cannot time entries correctly.
Fix: Compute `next_funding_ts` from UTC time: `next = ceil(utc_hour / 8) * 8 * 3600`. Calculate `seconds_until = next_funding_ts - now`. Display `Next funding: 2h 14m` on every Opportunity card. For bot: expose `funding_countdown_s` in `/api/opportunities` JSON.

---

**Problem 15 — STACK opportunities (spread + funding simultaneously) are invisible**
When a coin has both a spread alert on one exchange pair and a funding rate differential on another (or the same) pair, these appear as separate items in separate tabs. There is no combined view or flag indicating that both types of opportunity exist for the same symbol at the same time, which is the highest-value setup.
Fix: Add `is_stack: bool` to the analysis output in `scanner.py` — true when `spread_pct >= alert_spread_pct AND any(funding_opp.alert for funding_opp in funding_opportunities)`. Add `[STACK]` badge in the spread table row and log entry. Add a "STACK" filter chip to the Opportunities tab. Bot endpoint includes `is_stack` field.

---

## FEATURES

**Problem 16 — All thresholds are hardcoded constants requiring server restart to change**
`ALERT_SPREAD_PCT = 0.15`, `UNSTABLE_SPREAD_THRESHOLD = 1.0`, and `DEDUP_TTL = 600` are hardcoded in `scanner.py`, `server.py`, and `history.py`. Tuning these for different market conditions requires editing source files and restarting the server, which interrupts the scan session and loses in-memory alert state.
Fix: Move all thresholds to `data/config.json` loaded via a `config.py` singleton. Add `/api/config` GET and PATCH endpoints. Add a "Settings" section in the UI showing all tunable values as editable inputs with live-save. All modules read thresholds via `cfg.get("thresholds.X")` on each use, not at import time.

---

**Problem 17 — No per-exchange fee configuration**
Exchange maker fees are not tracked anywhere. Round-trip fees are approximated in the README as "~0.10%" but this is wrong for MEXC (0% maker), Binance (0.02% × 2 sides × 2 legs = 0.08%), and Bitmart (0.04% × 4 = 0.16%). Using a flat approximation causes systematic error in net-profit calculations.
Fix: Add `"fees"` section to `config.json` with default per-exchange maker fee percentages. Expose fee editor in Settings UI. Use actual fees in `compute_net_spread()` and `compute_net_funding()`. Show fee breakdown tooltip on hover over net % values.

---

**Problem 18 — Bulk actions require clicking each pair individually**
During high-volatility periods, 20–50 pairs can appear simultaneously with structurally invalid spreads (e.g. CoinEx consistently pricing all altcoins 0.5% above other exchanges). There is no way to bulk-exclude or bulk-ban all pairs involving a specific exchange or matching a spread pattern. Each must be clicked individually.
Fix: Add checkboxes to the spread table rows. Add a "Select all matching" option for the current filter. Add bulk action toolbar: "Exclude selected", "Ban selected", "Pin selected". Implement `POST /api/action/bulk` endpoint accepting `{action, keys[]}`.

---

**Problem 19 — No spread rate-of-change indicator**
The spread table shows the current spread % and whether it triggered an alert, but not whether the spread is growing, shrinking, or stable. A spread that went from 0.3% → 0.7% in the last 5 scans is a very different trading signal from one that went from 0.7% → 0.3%.
Fix: Track `spread_history[key]` in the frontend as a circular buffer of the last 10 spread values per pair. Compute `delta = current - avg_of_last_5`. Show `↑ +0.04%` or `↓ −0.02%` indicator next to the spread value. Use green for growing (entry signal) and red for shrinking (opportunity closing).

---

**Problem 20 — No sparkline showing spread history over time**
The alert duration badge shows how long a pair has been alive but gives no visual information about spread behavior over that time. A pair alive for 90 seconds with a stable 0.5% spread is very different from one that oscillated between 0.1% and 0.9%.
Fix: Store `spread_history[key]` as a time-series array `[{ts, pct}]` per pair in the frontend. Render a 60×20px canvas sparkline in the "Alive" column of the spread table, showing the spread trajectory. Color-code: flat = green, growing = yellow, spiky = orange.

---

**Problem 21 — Symbol list requires code edit and server restart**
The `SYMBOLS` list in `scanner.py` is a hardcoded Python array. Adding a new token (e.g. a newly listed coin) requires editing source code, committing, and restarting the server. The bot cannot add/remove symbols dynamically.
Fix: Move symbols to `config.json["symbols"]`. Load via `cfg.get_symbols()` in `scanner.py` on every scan start so changes take effect without restart. Add symbol management UI in a "Settings" tab: checkboxes for existing symbols, text input for adding custom ones, validation against exchange support. Expose `GET /api/config/symbols` and `PATCH /api/config/symbols` for bot use.

---

**Problem 22 — No "Pinned" status — pairs are either in main view or permanently removed**
The current lifecycle is `normal → unstable → banned` with no way back to main view. A user who wants to permanently keep a high-spread pair visible (e.g. a structural MEXC/Binance basis they are actively trading) has no way to do so — the auto-unstable system will always try to remove it.
Fix: Add a fourth lifecycle state `pinned`. Pinned pairs are immune to auto-unstable and are always shown at the top of the spread table with a 📌 icon. Store pinned keys in `config.json["pinned_pairs"]`. Add `/api/action/pin` and `/api/action/unpin` endpoints. Pinned pairs are not excluded from analytics.

---

**Problem 23 — No minimum alive-duration filter for the spread table**
The spread table shows pairs that appeared in the most recent scan, including ones that fired for 3 seconds and are likely noise. Traders need a way to filter out noise and show only pairs that have been consistently alive for at least N seconds, indicating a real and persistent opportunity rather than a transient tick.
Fix: Add a "Min alive" slider in the spread table toolbar (0s to 120s, step 5s). On each render, filter out rows where `aliveMs(key) < minAlive * 1000`. This requires no server changes — purely frontend filtering using the existing `stableMap`.

---

**Problem 24 — No CSV / JSON export for analytics records**
All analytics data is only visible in the browser table. There is no way to export alert records for external analysis (e.g. in Excel, Python, or to feed into bot backtesting). The bot also has no way to retrieve historical performance data in bulk.
Fix: Add "Export CSV" and "Export JSON" buttons to the Analytics tab. Client-side: use the current filtered `anData.records` array, serialize to CSV (Date, AlertID, Symbol, Type, Pair, Gross%, Net%, Score, Trust) or raw JSON, and trigger a browser `<a download>` link. Also expose `GET /api/analytics/export?period=day&format=csv` for bot access.

---

**Problem 25 — No per-symbol unstable threshold override**
The global `unstable_spread_pct = 1.0%` threshold is applied identically to all 45 symbols. DYDX regularly produces 1.2% spreads that are real and tradeable, while BTC producing a 0.3% spread would indicate a serious market dislocation. A single threshold creates either too many false-positive removals (for volatile altcoins) or misses real structural pairs (for major coins).
Fix: Add `"unstable_overrides": {"DYDX": 2.0, "BTC": 0.3}` to `config.json`. In `_auto_mark_unstable_if_needed()` in `server.py`, check `cfg.get(f"unstable_overrides.{sym}", cfg.get("thresholds.unstable_spread_pct", 1.0))` as the threshold for each symbol. Expose per-symbol overrides in the Settings UI.

---

## BOT-READINESS

**Problem 26 — `/api/data` is not suitable for bot consumption**
The main data endpoint returns the entire raw scan cache including all exchange raw data, analysis objects, and timestamps — hundreds of KB of unfiltered JSON. A bot querying this every second would need to parse and filter this entire payload on every request, which is slow, fragile, and tightly coupled to the internal data structure.
Fix: Add a dedicated `GET /api/opportunities` endpoint that returns only actionable items as clean, stable JSON. Each item includes: `alert_id`, `coin_id`, `symbol`, `type`, `score`, `gross_pct`, `net_pct`, `buy_exchange`, `sell_exchange`, `spread_pct` or `funding_diff`, `trust`, `alive_s`, `funding_countdown_s`, `any_stale`, `is_stack`, `exchange_health`. Supports query params `?min_score=65&type=spread&min_net=0.3`.

---

**Problem 27 — No outcome tracking — realized P&L is never measured**
The scanner tracks potential gross and net %. But when the bot actually executes a trade, there is no mechanism to record what price was actually achieved, what slippage occurred, and what the realized P&L was. Without this, it is impossible to know whether the opportunities the scanner identifies are actually profitable in practice.
Fix: Add `POST /api/outcome` endpoint. Bot posts `{alert_id, entry_buy_price, entry_sell_price, exit_buy_price, exit_sell_price, volume_usdt, realized_pnl_pct}`. Store in `data/outcomes_YYYY-MM-DD.json`. Show in Analytics tab as "Realized P&L" column next to "Gross %" and "Net %". This creates a ground-truth feedback loop for threshold calibration.

---

**Problem 28 — No order book depth validation before signaling**
The scanner compares last-trade prices between exchanges but does not check order book depth. A spread of 0.6% on a pair where the best ask has only $200 available is worthless — it will close as soon as $200 of buying pressure hits it. The scanner currently cannot distinguish between a $200 opportunity and a $50,000 one.
Fix: Add optional depth fetching in `scanner.py` per exchange for top-5 bid/ask levels. Compute `available_usdt = sum(qty * price for level in top5_asks)`. Add `depth_score` component to composite score: 0 pts for <$500, 5 pts for $500–2000, 10 pts for $2000–10000, 15 pts for >$10000. This is gated behind a config flag `depth_check_enabled: false` by default since it doubles API call count.

---

**Problem 29 — No webhook / push notification system for alerts**
The bot has to poll `/api/opportunities` constantly to catch new alerts. If the scan interval is 2 seconds and the average spread alert lives 15 seconds, the bot has at most a 7-scan window. There is no push mechanism, so the bot must poll aggressively or risk missing short-lived opportunities.
Fix: Add `POST /api/webhooks/subscribe` — register a URL, min_score threshold, and alert types. When `record_alerts()` creates a new alert that meets subscriber criteria, fire a background `POST` to the registered URL with the full opportunity JSON payload. Use a `threading.Thread(daemon=True)` per webhook call to avoid blocking the scan loop.

---

**Problem 30 — Exchange latency is not measured or tracked**
Exchange health checks run every 60 seconds and only test reachability. But in practice, one exchange might respond in 80ms and another in 900ms per scan. For a bot executing two legs simultaneously, the slower exchange is the binding constraint — if it times out, only one leg executes, creating an unhedged directional position.
Fix: In each `fetch_*()` function in `scanner.py`, record `t0 = time.time()` before the request and compute `latency_ms = (time.time() - t0) * 1000` after. Store a rolling window of last 20 latency values per exchange in a shared dict. Expose as `avg_latency_ms` and `p95_latency_ms` per exchange in `/api/exchanges`. Bot uses this to determine execution order — execute the slower exchange leg first.
