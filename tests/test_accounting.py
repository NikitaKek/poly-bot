"""Tests for spot-only position accounting and pre-trade risk checks."""

from __future__ import annotations

import logging
import unittest

from polymarket_bot.config import BotConfig
from polymarket_bot.main import _place_test_orders, cancel_all_open_orders
from polymarket_bot.models import (
    Fill,
    MarketSnapshot,
    Order,
    OrderStatus,
    Side,
    Token,
    TokenMarketSnapshot,
)
from polymarket_bot.order_manager import OrderManager
from polymarket_bot.paper_exchange import PaperExchange
from polymarket_bot.position_manager import PositionAccountingError, PositionManager
from polymarket_bot.risk_manager import RiskLimits, RiskManager


def test_logger() -> logging.Logger:
    logger = logging.getLogger("polymarket_bot_tests")
    logger.addHandler(logging.NullHandler())
    return logger


def risk_limits() -> RiskLimits:
    return RiskLimits(
        max_position_per_token=1_000.0,
        max_inventory_imbalance=1_000.0,
        max_order_size=1_000.0,
        min_price=0.01,
        max_price=0.99,
    )


def sample_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        yes=TokenMarketSnapshot(
            best_bid=0.40,
            best_ask=0.42,
            bid_size=100.0,
            ask_size=100.0,
            last_trade_price=0.41,
        ),
        no=TokenMarketSnapshot(
            best_bid=0.58,
            best_ask=0.60,
            bid_size=100.0,
            ask_size=100.0,
            last_trade_price=0.59,
        ),
    )


class SpotAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = test_logger()
        self.position_manager = PositionManager(logger=self.logger, initial_cash=1_000.0)
        self.risk_manager = RiskManager(
            limits=risk_limits(),
            position_manager=self.position_manager,
            logger=self.logger,
        )
        self.order_manager = OrderManager(
            paper_exchange=PaperExchange(logger=self.logger),
            logger=self.logger,
            risk_manager=self.risk_manager,
        )

    def test_cannot_sell_yes_when_position_is_zero(self) -> None:
        order = self.order_manager.place_limit_order(Token.YES, Side.SELL, price=0.45, size=1.0)

        self.assertIsNone(order)
        self.assertEqual(self.order_manager.get_open_orders(), [])
        self.assertEqual(self.risk_manager.rejected_orders, 1)

    def test_cannot_sell_no_when_position_is_zero(self) -> None:
        order = self.order_manager.place_limit_order(Token.NO, Side.SELL, price=0.55, size=1.0)

        self.assertIsNone(order)
        self.assertEqual(self.order_manager.get_open_orders(), [])
        self.assertEqual(self.risk_manager.rejected_orders, 1)

    def test_buy_then_sell_realized_pnl(self) -> None:
        self.position_manager.apply_fill(
            Fill(order_id="buy", token=Token.YES, side=Side.BUY, price=0.40, size=100.0)
        )
        self.position_manager.apply_fill(
            Fill(order_id="sell", token=Token.YES, side=Side.SELL, price=0.45, size=100.0)
        )

        self.assertAlmostEqual(self.position_manager.realized_pnl, 5.0)
        self.assertAlmostEqual(self.position_manager.yes_position, 0.0)
        self.assertAlmostEqual(self.position_manager.cash, 1_005.0)

    def test_buy_makes_position_positive(self) -> None:
        self.position_manager.apply_fill(
            Fill(order_id="buy", token=Token.YES, side=Side.BUY, price=0.40, size=10.0)
        )

        self.assertGreater(self.position_manager.yes_position, 0.0)
        self.assertAlmostEqual(self.position_manager.yes_avg_cost, 0.40)

    def test_sell_decreases_position(self) -> None:
        self.position_manager.apply_fill(
            Fill(order_id="buy", token=Token.NO, side=Side.BUY, price=0.60, size=10.0)
        )
        self.position_manager.apply_fill(
            Fill(order_id="sell", token=Token.NO, side=Side.SELL, price=0.65, size=4.0)
        )

        self.assertAlmostEqual(self.position_manager.no_position, 6.0)
        self.assertAlmostEqual(self.position_manager.no_avg_cost, 0.60)
        self.assertAlmostEqual(self.position_manager.realized_pnl, 0.20)

    def test_position_never_becomes_negative(self) -> None:
        with self.assertRaises(PositionAccountingError):
            self.position_manager.apply_fill(
                Fill(order_id="bad-sell", token=Token.YES, side=Side.SELL, price=0.45, size=1.0)
            )

        self.assertGreaterEqual(self.position_manager.yes_position, 0.0)
        self.assertGreaterEqual(self.position_manager.no_position, 0.0)

    def test_strategy_does_not_place_sell_yes_when_yes_position_is_zero(self) -> None:
        _place_test_orders(
            snapshot=sample_snapshot(),
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            config=BotConfig(default_order_size=5.0),
        )

        open_orders = self.order_manager.get_open_orders()
        self.assertFalse(any(order.token == Token.YES and order.side == Side.SELL for order in open_orders))

    def test_strategy_does_not_place_sell_no_when_no_position_is_zero(self) -> None:
        _place_test_orders(
            snapshot=sample_snapshot(),
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            config=BotConfig(default_order_size=5.0),
        )

        open_orders = self.order_manager.get_open_orders()
        self.assertFalse(any(order.token == Token.NO and order.side == Side.SELL for order in open_orders))

    def test_strategy_can_place_sell_yes_after_buy(self) -> None:
        self.position_manager.apply_fill(
            Fill(order_id="buy", token=Token.YES, side=Side.BUY, price=0.40, size=5.0)
        )

        _place_test_orders(
            snapshot=sample_snapshot(),
            order_manager=self.order_manager,
            position_manager=self.position_manager,
            config=BotConfig(default_order_size=5.0),
        )

        open_orders = self.order_manager.get_open_orders()
        self.assertTrue(any(order.token == Token.YES and order.side == Side.SELL for order in open_orders))

    def test_cancel_all_open_orders_cancels_open_and_partial_orders(self) -> None:
        open_order = self.order_manager.place_limit_order(Token.YES, Side.BUY, price=0.39, size=5.0)
        partial_order = self.order_manager.place_limit_order(Token.NO, Side.BUY, price=0.57, size=5.0)
        self.assertIsNotNone(open_order)
        self.assertIsNotNone(partial_order)
        partial_order.apply_fill(2.0)

        cancelled_count = cancel_all_open_orders(self.order_manager)

        self.assertEqual(cancelled_count, 2)
        self.assertEqual(open_order.status, OrderStatus.CANCELLED)
        self.assertEqual(partial_order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order_manager.get_open_orders(), [])

    def test_paper_exchange_buy_fill_uses_order_price(self) -> None:
        exchange = PaperExchange(logger=self.logger)
        order = Order(token=Token.YES, side=Side.BUY, price=0.50, size=5.0)

        fill = exchange.match_order(order, sample_snapshot())

        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.price, 0.50)

    def test_paper_exchange_sell_fill_uses_order_price(self) -> None:
        exchange = PaperExchange(logger=self.logger)
        order = Order(token=Token.YES, side=Side.SELL, price=0.35, size=5.0)

        fill = exchange.match_order(order, sample_snapshot())

        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.price, 0.35)


if __name__ == "__main__":
    unittest.main()
