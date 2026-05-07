"""Application configuration for the paper-trading bot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Runtime configuration for the MVP bot.

    The defaults are intentionally conservative and paper-only. They are useful
    for local smoke tests and can later be mapped to environment variables when
    real market data and order routing are introduced.
    """

    initial_cash: float = 1_000.0
    market_update_interval_seconds: float = 2.0
    default_order_size: float = 5.0
    strategy_quote_offset: float = 0.01
    max_iterations: int | None = None

    max_position_per_token: float = 100.0
    max_inventory_imbalance: float = 50.0
    max_order_size: float = 10.0
    min_price: float = 0.01
    max_price: float = 0.99

    log_file: str = "logs/bot.log"
    random_seed: int | None = 42


DEFAULT_CONFIG = BotConfig()
