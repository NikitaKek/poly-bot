"""Entry point for the safe paper-trading MVP bot."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterable
from typing import Protocol

from .config import DEFAULT_CONFIG, BotConfig
from .logger import setup_logger
from .market_data import MockMarketDataGenerator
from .models import MarketSnapshot, Order, Side, Token
from .order_manager import OrderManager
from .paper_exchange import PaperExchange
from .position_manager import PositionManager
from .polymarket_market_data import (
    MarketDataError,
    MarketDiscoveryClient,
    PolymarketMarketDataClient,
    PolymarketOrderBookClient,
)
from .risk_manager import RiskLimits, RiskManager


class MarketDataProvider(Protocol):
    """Market data provider interface used by the paper bot loop."""

    def get_snapshot(self) -> MarketSnapshot | None:
        """Return the latest market snapshot, or None when data is incomplete."""


def build_components(config: BotConfig) -> tuple[
    logging.Logger,
    MarketDataProvider,
    OrderManager,
    PositionManager,
    RiskManager,
]:
    """Construct the bot's paper-trading components."""

    logger = setup_logger(log_file=config.log_file)
    market_data = _create_market_data_provider(config=config, logger=logger)
    paper_exchange = PaperExchange(logger=logger)
    position_manager = PositionManager(logger=logger, initial_cash=config.initial_cash)
    risk_manager = RiskManager(
        limits=RiskLimits(
            max_position_per_token=config.max_position_per_token,
            max_inventory_imbalance=config.max_inventory_imbalance,
            max_order_size=config.max_order_size,
            min_price=config.min_price,
            max_price=config.max_price,
        ),
        position_manager=position_manager,
        logger=logger,
    )
    order_manager = OrderManager(
        paper_exchange=paper_exchange,
        logger=logger,
        risk_manager=risk_manager,
    )
    return logger, market_data, order_manager, position_manager, risk_manager


def _create_market_data_provider(config: BotConfig, logger: logging.Logger) -> MarketDataProvider:
    """Create the configured market data provider."""

    if config.market_data_mode == "mock":
        logger.info("Using mock market data mode")
        return MockMarketDataGenerator(logger=logger, seed=config.random_seed)

    if config.market_data_mode == "polymarket":
        logger.info(
            "Using public Polymarket market data mode | gamma_base_url=%s clob_base_url=%s query=%s",
            config.gamma_base_url,
            config.clob_base_url,
            config.btc_market_query,
        )
        discovery_client = MarketDiscoveryClient(
            logger=logger,
            gamma_base_url=config.gamma_base_url,
            btc_market_query=config.btc_market_query,
            timeout_seconds=config.http_timeout_seconds,
        )
        orderbook_client = PolymarketOrderBookClient(
            logger=logger,
            clob_base_url=config.clob_base_url,
            timeout_seconds=config.http_timeout_seconds,
        )
        return PolymarketMarketDataClient(
            discovery_client=discovery_client,
            orderbook_client=orderbook_client,
            market_refresh_seconds=config.market_refresh_seconds,
            logger=logger,
        )

    raise ValueError(f"unsupported market_data_mode={config.market_data_mode!r}")


def run_bot(config: BotConfig) -> None:
    """Run the paper-trading loop."""

    logger, market_data, order_manager, position_manager, risk_manager = build_components(config)
    logger.info("Starting Polymarket BTCUSDT 15m paper bot | config=%s", config)

    iteration = 0
    try:
        while config.max_iterations is None or iteration < config.max_iterations:
            iteration += 1
            try:
                snapshot = market_data.get_snapshot()
            except MarketDataError as exc:
                logger.warning("Market data unavailable; skipping iteration %d | %s", iteration, exc)
                snapshot = None

            if snapshot is None:
                logger.warning("Market snapshot unavailable; skipping iteration %d", iteration)
                if config.max_iterations is None or iteration < config.max_iterations:
                    time.sleep(config.market_update_interval_seconds)
                continue

            fills = order_manager.update_orders_from_market(snapshot)
            position_manager.apply_fills(fills)
            position_manager.update_unrealized_pnl(snapshot)
            cancel_all_open_orders(order_manager)
            _place_test_orders(
                snapshot=snapshot,
                order_manager=order_manager,
                position_manager=position_manager,
                config=config,
            )
            _log_status(
                logger,
                iteration,
                snapshot,
                order_manager.get_open_orders(),
                position_manager,
                risk_manager,
            )

            if config.max_iterations is None or iteration < config.max_iterations:
                time.sleep(config.market_update_interval_seconds)
    except KeyboardInterrupt:
        logger.info("Paper bot stopped by user")
    except Exception:
        logger.exception("Fatal error in paper bot loop")
        raise
    finally:
        logger.info("Final positions | %s", position_manager.summary())
        logger.info(
            "All orders=%d fills=%d rejected_orders=%d",
            len(order_manager.get_all_orders()),
            len(position_manager.fills_history),
            risk_manager.rejected_orders,
        )


def cancel_all_open_orders(order_manager: OrderManager) -> int:
    """Cancel all currently open or partially filled orders."""

    cancelled_count = 0
    for order in order_manager.get_open_orders():
        if order_manager.cancel_order(order.order_id):
            cancelled_count += 1
    return cancelled_count


def _log_status(
    logger: logging.Logger,
    iteration: int,
    snapshot: MarketSnapshot,
    open_orders: Iterable[Order],
    position_manager: PositionManager,
    risk_manager: RiskManager,
) -> None:
    open_orders_list = list(open_orders)
    logger.info(
        "Iteration %d | YES %.3f/%.3f | NO %.3f/%.3f",
        iteration,
        snapshot.yes.best_bid,
        snapshot.yes.best_ask,
        snapshot.no.best_bid,
        snapshot.no.best_ask,
    )
    logger.info("Positions | %s", position_manager.summary())
    logger.info(
        "Orders | open_orders=%d rejected_orders=%d",
        len(open_orders_list),
        risk_manager.rejected_orders,
    )
    for order in open_orders_list:
        logger.info(
            "Open order | order=%s %s %s size=%.4f filled=%.4f remaining=%.4f price=%.3f status=%s",
            order.order_id,
            order.side,
            order.token,
            order.size,
            order.filled_size,
            order.remaining_size,
            order.price,
            order.status,
        )


def _place_test_orders(
    snapshot: MarketSnapshot,
    order_manager: OrderManager,
    position_manager: PositionManager,
    config: BotConfig,
) -> None:
    """Place simple passive test quotes around YES and NO books.

    BUY quotes are always eligible subject to risk checks. SELL quotes are only
    proposed when spot inventory can cover the full test order size, which keeps
    normal logs free from predictable naked-short rejections.
    """

    size = config.default_order_size
    proposals = [
        (Token.YES, Side.BUY, snapshot.yes.best_bid - config.strategy_quote_offset),
        (Token.NO, Side.BUY, snapshot.no.best_bid - config.strategy_quote_offset),
    ]

    if position_manager.yes_position >= size:
        proposals.append((Token.YES, Side.SELL, snapshot.yes.best_ask + config.strategy_quote_offset))

    if position_manager.no_position >= size:
        proposals.append((Token.NO, Side.SELL, snapshot.no.best_ask + config.strategy_quote_offset))

    for token, side, raw_price in proposals:
        price = min(max(raw_price, config.min_price), config.max_price)
        order_manager.place_limit_order(token=token, side=side, price=price, size=size)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description="Safe Polymarket paper-trading MVP bot.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of loop iterations to run. Use 0 for an infinite loop.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_CONFIG.market_update_interval_seconds,
        help="Seconds to sleep between market snapshots.",
    )
    parser.add_argument(
        "--order-size",
        type=float,
        default=DEFAULT_CONFIG.default_order_size,
        help="Paper order size for each test quote.",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=DEFAULT_CONFIG.initial_cash,
        help="Starting paper cash balance.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_CONFIG.random_seed,
        help="Random seed for mock market data. Use any integer for reproducible runs.",
    )
    parser.add_argument(
        "--market-data-mode",
        choices=("mock", "polymarket"),
        default=DEFAULT_CONFIG.market_data_mode,
        help="Market data source. Trading remains paper-only in both modes.",
    )
    parser.add_argument(
        "--gamma-base-url",
        default=DEFAULT_CONFIG.gamma_base_url,
        help="Public Polymarket Gamma API base URL.",
    )
    parser.add_argument(
        "--clob-base-url",
        default=DEFAULT_CONFIG.clob_base_url,
        help="Public Polymarket CLOB API base URL.",
    )
    parser.add_argument(
        "--btc-market-query",
        default=DEFAULT_CONFIG.btc_market_query,
        help="Text used to select an active BTC 15-minute market by question/slug.",
    )
    parser.add_argument(
        "--market-refresh-seconds",
        type=float,
        default=DEFAULT_CONFIG.market_refresh_seconds,
        help="Seconds between Gamma market rediscovery attempts in Polymarket mode.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI wrapper for the paper bot."""

    args = parse_args()
    config = BotConfig(
        initial_cash=args.initial_cash,
        market_update_interval_seconds=args.interval,
        default_order_size=args.order_size,
        max_iterations=None if args.iterations == 0 else args.iterations,
        random_seed=args.seed,
        market_data_mode=args.market_data_mode,
        gamma_base_url=args.gamma_base_url,
        clob_base_url=args.clob_base_url,
        btc_market_query=args.btc_market_query,
        market_refresh_seconds=args.market_refresh_seconds,
    )
    run_bot(config)


if __name__ == "__main__":
    main()
