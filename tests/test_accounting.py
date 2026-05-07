"""Tests for spot-only position accounting and pre-trade risk checks."""

from __future__ import annotations

import logging
import unittest

from polymarket_bot.models import Fill, Side, Token
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


if __name__ == "__main__":
    unittest.main()
