# Polymarket BTCUSDT 15m Paper-Trading MVP

Safe Python MVP for Polymarket-style 15-minute BTCUSDT prediction markets.

This project does **not** send real orders and does **not** require API keys. It
builds the local infrastructure needed for market data snapshots, order
management, position tracking, risk checks, logging, and paper trading.

## Project Structure

```text
polymarket_bot/
  main.py
  config.py
  market_data.py
  polymarket_market_data.py
  order_manager.py
  position_manager.py
  risk_manager.py
  logger.py
  models.py
  paper_exchange.py
  requirements.txt
  README.md
```

## Requirements

- Python 3.11+
- No external packages are required for the MVP

## Installation

From the repository root:

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r polymarket_bot/requirements.txt
```

On macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r polymarket_bot/requirements.txt
```

## Run

Run a short paper-trading session:

```bash
python -m polymarket_bot.main --iterations 5 --interval 1
```

Run continuously until `Ctrl+C`:

```bash
python -m polymarket_bot.main --iterations 0
```

Run with public Polymarket market data while keeping paper-only execution:

```bash
python -m polymarket_bot.main --market-data-mode polymarket --iterations 0
```

Each run creates a dedicated paper-trading session directory:

```text
logs/sessions/<session_id>/
```

The `session_id` format is:

```text
YYYYMMDD_HHMMSS_<market_data_mode>_paper
```

## What Each Module Does

- `models.py`: dataclasses and enums for tokens, sides, orders, fills, and market snapshots.
- `config.py`: conservative paper-trading defaults and risk limits.
- `market_data.py`: mock market data generator for YES/NO tokens where `YES + NO ~= 1`.
- `polymarket_market_data.py`: public Gamma market discovery and public CLOB order book snapshots.
- `paper_exchange.py`: paper limit-order execution against the current best bid/ask.
- `order_manager.py`: places, cancels, stores, and updates paper orders.
- `position_manager.py`: tracks cash plus market-specific spot YES/NO inventory, average cost, fills, realized PnL, unrealized PnL, unresolved inventory, and equity.
- `risk_manager.py`: validates price, size, cash, spot inventory, open-order exposure, and inventory imbalance limits before orders are accepted.
- `logger.py`: session-aware human logs, structured JSONL events, CSV exports, config snapshots, and session summaries.
- `main.py`: runs the paper-trading loop and places simple test quotes.

## Session Logs

Every run writes artifacts under:

```text
logs/sessions/<session_id>/
  bot.log
  events.jsonl
  summary.json
  config.json
  markets/
    <market_slug>.jsonl
  csv/
    snapshots.csv
    orders.csv
    fills.csv
    positions.csv
    risk_rejections.csv
```

`bot.log` remains human-readable and includes session, event, iteration, market
slug, and condition id context:

```text
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=snapshot | iteration=1 | market=mock-btc-15m | condition=mock-btc-15m-condition | Snapshot | YES 0.489/0.504 | NO 0.496/0.511
```

`events.jsonl` contains one valid JSON object per line for analysis:

```json
{"event":"snapshot","session_id":"20260511_140045_mock_paper","iteration":1,"market_slug":"mock-btc-15m","condition_id":"mock-btc-15m-condition","yes_bid":0.489,"yes_ask":0.504,"no_bid":0.496,"no_ask":0.511}
{"event":"order_placed","session_id":"20260511_140045_mock_paper","order_id":"...","outcome":"YES","side":"BUY","price":0.479,"size":5.0,"reason":"test_quote"}
```

Structured event types include `session_started`, `session_finished`,
`market_selected`, `market_rollover`, `snapshot`, `strategy_decision`,
`order_placed`, `order_cancelled`, `fill`, `position`, `risk_rejection`,
`market_data_unavailable`, `forced_exit`, and `error`.

Events that include `market_slug` are also appended to
`markets/<market_slug>.jsonl`, which makes it easy to inspect one 15-minute
market without filtering the full session file.

To inspect fills with Python, optionally using pandas:

```python
import pandas as pd

fills = pd.read_csv("logs/sessions/20260511_140045_mock_paper/csv/fills.csv")
fills["pnl_hint"] = fills["notional"]
print(fills[["ts", "market_slug", "outcome", "side", "price", "size", "notional"]])
```

If you want to avoid pandas:

```python
import csv

with open("logs/sessions/20260511_140045_mock_paper/csv/fills.csv", newline="", encoding="utf-8") as file:
    for row in csv.DictReader(file):
        print(row["ts"], row["market_slug"], row["outcome"], row["side"], row["price"], row["size"])
```

`summary.json` is written at the end of the run and contains ending cash,
ending equity, realized/unrealized PnL, max/min equity, max drawdown, total
orders, total fills, total risk rejections, markets seen/traded, and final
market-specific positions.

## Market Data Modes

The bot supports two market data modes:

- `mock`: local random YES/NO snapshots. This is the default.
- `polymarket`: public read-only Polymarket data from Gamma and CLOB.

Polymarket mode uses:

- Gamma base URL: `https://gamma-api.polymarket.com`
- CLOB base URL: `https://clob.polymarket.com`
- Gamma filters: `active=true`, `closed=false`

It first generates timestamp-based BTC 15-minute slugs such as
`btc-updown-15m-1778410800`, fetches them through Gamma event/market slug
endpoints, then falls back to scanning active Gamma market `question`/`slug`
values for BTC/Bitcoin and 15m/15-minute signals. `clobTokenIds` are read as
`[YES token id, NO token id]`. No private key, API key, order signing, or
authenticated endpoint is used.

## Paper Fill Logic

A paper order fills when it crosses the current top of book:

- `BUY` fills when `order.price >= current_best_ask`
- `SELL` fills when `order.price <= current_best_bid`

Paper fills are intentionally conservative: execution price is the order's
limit price, not the currently available best bid or ask.

The current implementation supports simplified partial fills by limiting the
fill size to available top-of-book size. If the top size is larger than the
remaining order size, the order is fully filled.

Before placing a fresh set of test quotes, the bot cancels all stale open or
partially filled orders. The test strategy always considers BUY quotes, but it
only proposes SELL quotes when current spot inventory can cover the full order
size.

## Spot Accounting

This MVP uses a simple spot-only accounting model:

- YES/NO inventory is scoped to a specific `condition_id` and `token_id`.
- Inventory from one 15-minute market is never reused in the next 15-minute market.
- Naked short selling is blocked.
- A `SELL` order is accepted only when available inventory for that token is at
  least the order size.
- Open `SELL` orders reserve inventory, so the same shares cannot be sold twice.
- `avg_cost` is tracked separately for YES and NO.
- Realized PnL is recognized only when owned inventory is sold:

```text
realized_pnl += (sell_price - avg_cost[token]) * filled_size
```

Equity is marked as:

```text
equity = cash + yes_position * yes_mark_price + no_position * no_mark_price
```

If a fill would make YES or NO inventory negative, the bot logs a critical error
and raises an accounting exception.

When the selected 15-minute market changes, open orders from the previous
`condition_id` are cancelled. If a market is near expiry, forced-exit guardrails
apply:

- Less than 90 seconds to expiry: do not open new positions.
- Less than 60 seconds to expiry: cancel open orders and try to close current
  market inventory in paper mode.
- If paper exit cannot close all inventory, the market ledger is archived with
  unresolved inventory.

## Tests

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

## Example Output

```text
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=session_started | iteration=- | market=- | condition=- | Starting Polymarket BTCUSDT 15m paper bot
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=market_selected | iteration=1 | market=mock-btc-15m | condition=mock-btc-15m-condition | Selected market | question=Mock BTC 15-minute prediction market slug=mock-btc-15m
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=snapshot | iteration=1 | market=mock-btc-15m | condition=mock-btc-15m-condition | Snapshot | YES 0.489/0.504 | NO 0.496/0.511
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=order_placed | iteration=1 | market=mock-btc-15m | condition=mock-btc-15m-condition | Placed paper order | order=... BUY YES token_id=mock-btc-15m-yes size=5.0000 price=0.479
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=position | iteration=1 | market=mock-btc-15m | condition=mock-btc-15m-condition | Portfolio | starting_cash=1000.00 cash=1000.00 markets=1 realized_pnl=0.00 unrealized_pnl=0.00 total_equity=1000.00
2026-05-11 14:00:45 | INFO | session=20260511_140045_mock_paper | event=session_finished | iteration=- | market=- | condition=- | All orders=2 fills=0 rejected_orders=0 summary=logs\sessions\20260511_140045_mock_paper\summary.json
```

## Next Steps for Real Polymarket Connectivity

Keep real trading behind an explicit separate adapter and preserve paper mode as
the default.

Suggested next steps:

1. Add a `PolymarketMarketDataClient` that subscribes to official WebSocket
   feeds and converts updates into `MarketSnapshot`.
2. Add market discovery for active 15-minute BTCUSDT events and token IDs.
3. Introduce persistent storage for orders, fills, and snapshots.
4. Add a real exchange adapter with explicit feature flags, dry-run protection,
   API-key loading, and strong pre-trade checks.
5. Expand risk controls with outstanding-order exposure, daily loss limits,
   self-trade prevention, and market state validation.
6. Build strategy modules separately from infrastructure so paper and live
   execution can share the same interface.

Real order routing should only be added after paper trading, replay tests, and
kill-switch behavior are verified.
