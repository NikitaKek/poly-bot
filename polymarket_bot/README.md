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

Logs are written to:

```text
logs/bot.log
```

## What Each Module Does

- `models.py`: dataclasses and enums for tokens, sides, orders, fills, and market snapshots.
- `config.py`: conservative paper-trading defaults and risk limits.
- `market_data.py`: mock market data generator for YES/NO tokens where `YES + NO ~= 1`.
- `paper_exchange.py`: paper limit-order execution against the current best bid/ask.
- `order_manager.py`: places, cancels, stores, and updates paper orders.
- `position_manager.py`: tracks cash, spot YES/NO inventory, average cost, fills, realized PnL, unrealized PnL, and equity.
- `risk_manager.py`: validates price, size, cash, spot inventory, open-order exposure, and inventory imbalance limits before orders are accepted.
- `logger.py`: logs to console and `logs/bot.log`.
- `main.py`: runs the paper-trading loop and places simple test quotes.

## Paper Fill Logic

A paper order fills when it crosses the current top of book:

- `BUY` fills when `order.price >= current_best_ask`
- `SELL` fills when `order.price <= current_best_bid`

The current implementation supports simplified partial fills by limiting the
fill size to available top-of-book size. If the top size is larger than the
remaining order size, the order is fully filled.

## Spot Accounting

This MVP uses a simple spot-only accounting model:

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

## Tests

Run the unit tests:

```bash
python -m unittest discover -s tests -v
```

## Example Output

```text
2026-05-07 12:00:00 | INFO | polymarket_bot | Starting Polymarket BTCUSDT 15m paper bot
2026-05-07 12:00:00 | INFO | polymarket_bot | Market snapshot | YES 0.487/0.509 size 9.46/19.73 | NO 0.480/0.506 size 22.84/6.74
2026-05-07 12:00:00 | INFO | polymarket_bot | Iteration 1 | YES 0.487/0.509 | NO 0.480/0.506
2026-05-07 12:00:00 | INFO | polymarket_bot | Positions | starting_cash=1000.00 cash=1000.00 yes_position=0.0000 no_position=0.0000 yes_avg_cost=0.0000 no_avg_cost=0.0000 imbalance=0.0000 realized_pnl=0.00 unrealized_pnl=0.00 equity=1000.00
2026-05-07 12:00:00 | INFO | polymarket_bot | Orders | open_orders=0 rejected_orders=0
2026-05-07 12:00:00 | INFO | polymarket_bot | Placed paper order | order=... BUY YES size=5.0000 price=0.477
2026-05-07 12:00:00 | WARNING | polymarket_bot | Risk rejection | naked short blocked: SELL 5.0000 YES requested with available spot inventory 0.0000
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
