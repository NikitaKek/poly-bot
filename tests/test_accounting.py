"""Tests for spot-only position accounting and pre-trade risk checks."""

from __future__ import annotations

import logging
import unittest
from datetime import timedelta

from polymarket_bot.config import BotConfig
from polymarket_bot.main import (
    _place_test_orders,
    _new_positions_blocked,
    _seconds_to_expiry,
    cancel_all_open_orders,
    cancel_open_orders_not_for_condition,
)
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


def sample_snapshot(
    condition_id: str = "condition-a",
    market_slug: str = "market-a",
    yes_token_id: str = "yes-a",
    no_token_id: str = "no-a",
    expires_in_seconds: float | None = None,
) -> MarketSnapshot:
    expiry_time = None
    if expires_in_seconds is not None:
        from polymarket_bot.models import utc_now

        expiry_time = utc_now() + timedelta(seconds=expires_in_seconds)

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
        condition_id=condition_id,
        market_slug=market_slug,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        expiry_time=expiry_time,
    )


def sample_fill(
    outcome: Token,
    side: Side,
    price: float,
    size: float,
    condition_id: str = "condition-a",
    market_slug: str = "market-a",
    token_id: str | None = None,
    order_id: str = "fill",
) -> Fill:
    return Fill(
        order_id=order_id,
        condition_id=condition_id,
        market_slug=market_slug,
        token_id=token_id or ("yes-a" if outcome == Token.YES else "no-a"),
        outcome=outcome,
        side=side,
        price=price,
        size=size,
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
        order = self.order_manager.place_limit_order(
            sample_snapshot(), Token.YES, Side.SELL, price=0.45, size=1.0
        )

        self.assertIsNone(order)
        self.assertEqual(self.order_manager.get_open_orders(), [])
        self.assertEqual(self.risk_manager.rejected_orders, 1)

    def test_cannot_sell_no_when_position_is_zero(self) -> None:
        order = self.order_manager.place_limit_order(
            sample_snapshot(), Token.NO, Side.SELL, price=0.55, size=1.0
        )

        self.assertIsNone(order)
        self.assertEqual(self.order_manager.get_open_orders(), [])
        self.assertEqual(self.risk_manager.rejected_orders, 1)

    def test_buy_then_sell_realized_pnl(self) -> None:
        self.position_manager.apply_fill(
            sample_fill(Token.YES, Side.BUY, price=0.40, size=100.0, order_id="buy")
        )
        self.position_manager.apply_fill(
            sample_fill(Token.YES, Side.SELL, price=0.45, size=100.0, order_id="sell")
        )

        self.assertAlmostEqual(self.position_manager.realized_pnl, 5.0)
        self.assertAlmostEqual(self.position_manager.get_position("condition-a", Token.YES), 0.0)
        self.assertAlmostEqual(self.position_manager.cash, 1_005.0)

    def test_buy_makes_position_positive(self) -> None:
        self.position_manager.apply_fill(
            sample_fill(Token.YES, Side.BUY, price=0.40, size=10.0, order_id="buy")
        )

        market_position = self.position_manager.get_market_position("condition-a")
        self.assertGreater(market_position.yes_position, 0.0)
        self.assertAlmostEqual(market_position.yes_avg_cost, 0.40)

    def test_sell_decreases_position(self) -> None:
        self.position_manager.apply_fill(
            sample_fill(Token.NO, Side.BUY, price=0.60, size=10.0, order_id="buy")
        )
        self.position_manager.apply_fill(
            sample_fill(Token.NO, Side.SELL, price=0.65, size=4.0, order_id="sell")
        )

        market_position = self.position_manager.get_market_position("condition-a")
        self.assertAlmostEqual(market_position.no_position, 6.0)
        self.assertAlmostEqual(market_position.no_avg_cost, 0.60)
        self.assertAlmostEqual(self.position_manager.realized_pnl, 0.20)

    def test_position_never_becomes_negative(self) -> None:
        with self.assertRaises(PositionAccountingError):
            self.position_manager.apply_fill(
                sample_fill(Token.YES, Side.SELL, price=0.45, size=1.0, order_id="bad-sell")
            )

        market_position = self.position_manager.get_market_position("condition-a")
        self.assertGreaterEqual(market_position.yes_position, 0.0)
        self.assertGreaterEqual(market_position.no_position, 0.0)

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
            sample_fill(Token.YES, Side.BUY, price=0.40, size=5.0, order_id="buy")
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
        snapshot = sample_snapshot()
        open_order = self.order_manager.place_limit_order(snapshot, Token.YES, Side.BUY, price=0.39, size=5.0)
        partial_order = self.order_manager.place_limit_order(snapshot, Token.NO, Side.BUY, price=0.57, size=5.0)
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
        snapshot = sample_snapshot()
        order = Order(
            condition_id=snapshot.condition_id,
            market_slug=snapshot.market_slug,
            token_id=snapshot.yes_token_id,
            outcome=Token.YES,
            side=Side.BUY,
            price=0.50,
            size=5.0,
        )

        fill = exchange.match_order(order, snapshot)

        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.price, 0.50)

    def test_paper_exchange_sell_fill_uses_order_price(self) -> None:
        exchange = PaperExchange(logger=self.logger)
        snapshot = sample_snapshot()
        order = Order(
            condition_id=snapshot.condition_id,
            market_slug=snapshot.market_slug,
            token_id=snapshot.yes_token_id,
            outcome=Token.YES,
            side=Side.SELL,
            price=0.35,
            size=5.0,
        )

        fill = exchange.match_order(order, snapshot)

        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.price, 0.35)

    def test_yes_one_market_is_not_yes_other_market(self) -> None:
        self.position_manager.apply_fill(
            sample_fill(Token.YES, Side.BUY, price=0.40, size=5.0, condition_id="condition-a")
        )

        self.assertAlmostEqual(self.position_manager.get_position("condition-a", Token.YES), 5.0)
        self.assertAlmostEqual(self.position_manager.get_position("condition-b", Token.YES), 0.0)

    def test_old_order_cannot_fill_on_new_market_snapshot(self) -> None:
        exchange = PaperExchange(logger=self.logger)
        old_snapshot = sample_snapshot("condition-a", "market-a", "yes-a", "no-a")
        new_snapshot = sample_snapshot("condition-b", "market-b", "yes-b", "no-b")
        order = Order(
            condition_id=old_snapshot.condition_id,
            market_slug=old_snapshot.market_slug,
            token_id=old_snapshot.yes_token_id,
            outcome=Token.YES,
            side=Side.BUY,
            price=0.50,
            size=5.0,
        )

        fill = exchange.match_order(order, new_snapshot)

        self.assertIsNone(fill)

    def test_market_change_cancels_old_market_open_orders(self) -> None:
        old_snapshot = sample_snapshot("condition-a", "market-a", "yes-a", "no-a")
        new_snapshot = sample_snapshot("condition-b", "market-b", "yes-b", "no-b")
        old_order = self.order_manager.place_limit_order(old_snapshot, Token.YES, Side.BUY, 0.39, 5.0)
        new_order = self.order_manager.place_limit_order(new_snapshot, Token.YES, Side.BUY, 0.39, 5.0)

        cancelled = cancel_open_orders_not_for_condition(self.order_manager, new_snapshot.condition_id)

        self.assertEqual(cancelled, 1)
        self.assertEqual(old_order.status, OrderStatus.CANCELLED)
        self.assertEqual(new_order.status, OrderStatus.OPEN)

    def test_positions_do_not_transfer_between_markets(self) -> None:
        self.position_manager.apply_fill(
            sample_fill(Token.YES, Side.BUY, price=0.40, size=5.0, condition_id="condition-a")
        )
        self.position_manager.ensure_market(sample_snapshot("condition-b", "market-b", "yes-b", "no-b"))

        market_a = self.position_manager.get_market_position("condition-a")
        market_b = self.position_manager.get_market_position("condition-b")
        self.assertEqual(market_a.yes_position, 5.0)
        self.assertEqual(market_b.yes_position, 0.0)

    def test_forced_exit_blocks_new_orders_near_expiry(self) -> None:
        snapshot = sample_snapshot(expires_in_seconds=30.0)

        self.assertIsNotNone(_seconds_to_expiry(snapshot))
        self.assertTrue(_new_positions_blocked(snapshot))


if __name__ == "__main__":
    unittest.main()
