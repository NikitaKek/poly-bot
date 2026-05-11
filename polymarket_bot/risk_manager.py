"""Pre-trade risk checks for paper orders."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from .logger import StructuredEventLogger, log_context
from .models import MarketSnapshot, Order, Side, Token
from .position_manager import PositionManager


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Basic pre-trade limits used by the MVP."""

    max_position_per_token: float
    max_inventory_imbalance: float
    max_order_size: float
    min_price: float
    max_price: float


@dataclass(slots=True)
class RiskManager:
    """Validate orders before they enter the paper order book."""

    limits: RiskLimits
    position_manager: PositionManager
    logger: logging.Logger
    event_logger: StructuredEventLogger | None = None
    rejected_orders: int = 0

    def validate_order(
        self,
        market_snapshot: MarketSnapshot,
        outcome: Token,
        side: Side,
        price: float,
        size: float,
        open_orders: Iterable[Order] | None = None,
        iteration: int | None = None,
    ) -> tuple[bool, str]:
        """Return whether a proposed spot order passes all configured risk checks.

        SELL orders are only allowed against already-owned inventory. Existing
        open SELL orders reserve inventory, so a second SELL cannot reuse the
        same shares.
        """

        if size <= 0:
            return self._reject("order size must be positive", market_snapshot, outcome, side, price, size, iteration)

        if size > self.limits.max_order_size:
            return self._reject(
                f"order size {size:.4f} exceeds max_order_size {self.limits.max_order_size:.4f}",
                market_snapshot,
                outcome,
                side,
                price,
                size,
                iteration,
            )

        if price < self.limits.min_price or price > self.limits.max_price:
            return self._reject(
                f"price {price:.3f} outside [{self.limits.min_price:.3f}, {self.limits.max_price:.3f}]",
                market_snapshot,
                outcome,
                side,
                price,
                size,
                iteration,
            )

        open_orders_list = [
            order
            for order in list(open_orders or [])
            if order.condition_id == market_snapshot.condition_id
        ]

        if side == Side.SELL:
            available_position = self._available_to_sell(
                condition_id=market_snapshot.condition_id,
                outcome=outcome,
                token_id=market_snapshot.token_id(outcome),
                open_orders=open_orders_list,
            )
            if size > available_position:
                return self._reject(
                    f"naked short blocked: SELL {size:.4f} {outcome} requested for "
                    f"condition_id={market_snapshot.condition_id} token_id={market_snapshot.token_id(outcome)} "
                    f"with available spot inventory {available_position:.4f}",
                    market_snapshot,
                    outcome,
                    side,
                    price,
                    size,
                    iteration,
                )
        else:
            projected_long_position = (
                self.position_manager.get_position(market_snapshot.condition_id, outcome)
                + self._open_buy_size(
                    condition_id=market_snapshot.condition_id,
                    outcome=outcome,
                    token_id=market_snapshot.token_id(outcome),
                    open_orders=open_orders_list,
                )
                + size
            )
            if projected_long_position > self.limits.max_position_per_token:
                return self._reject(
                    f"projected {outcome} long position {projected_long_position:.4f} exceeds "
                    f"max_position_per_token {self.limits.max_position_per_token:.4f}",
                    market_snapshot,
                    outcome,
                    side,
                    price,
                    size,
                    iteration,
                )

            required_cash = price * size
            reserved_cash = self._reserved_buy_cash(open_orders_list)
            if required_cash + reserved_cash > self.position_manager.cash:
                return self._reject(
                    f"insufficient paper cash: required {required_cash + reserved_cash:.2f}, "
                    f"available {self.position_manager.cash:.2f}",
                    market_snapshot,
                    outcome,
                    side,
                    price,
                    size,
                    iteration,
                )

        market_position = self.position_manager.ensure_market(market_snapshot)
        proposed_imbalance_effect = self._inventory_imbalance_effect(outcome, side, size)
        positive_imbalance = market_position.inventory_imbalance + max(proposed_imbalance_effect, 0.0)
        negative_imbalance = market_position.inventory_imbalance + min(proposed_imbalance_effect, 0.0)

        for order in open_orders_list:
            effect = self._inventory_imbalance_effect(
                order.outcome,
                order.side,
                order.remaining_size,
            )
            positive_imbalance += max(effect, 0.0)
            negative_imbalance += min(effect, 0.0)

        if positive_imbalance > self.limits.max_inventory_imbalance:
            return self._reject(
                f"worst-case positive inventory imbalance {positive_imbalance:.4f} exceeds "
                f"max_inventory_imbalance {self.limits.max_inventory_imbalance:.4f}",
                market_snapshot,
                outcome,
                side,
                price,
                size,
                iteration,
            )

        if negative_imbalance < -self.limits.max_inventory_imbalance:
            return self._reject(
                f"worst-case negative inventory imbalance {negative_imbalance:.4f} exceeds "
                f"max_inventory_imbalance {self.limits.max_inventory_imbalance:.4f}",
                market_snapshot,
                outcome,
                side,
                price,
                size,
                iteration,
            )

        return True, "accepted"

    def _available_to_sell(
        self,
        condition_id: str,
        outcome: Token,
        token_id: str,
        open_orders: Iterable[Order],
    ) -> float:
        reserved_sell_size = sum(
            order.remaining_size
            for order in open_orders
            if (
                order.condition_id == condition_id
                and order.outcome == outcome
                and order.token_id == token_id
                and order.side == Side.SELL
            )
        )
        return max(self.position_manager.get_position(condition_id, outcome) - reserved_sell_size, 0.0)

    @staticmethod
    def _open_buy_size(
        condition_id: str,
        outcome: Token,
        token_id: str,
        open_orders: Iterable[Order],
    ) -> float:
        return sum(
            order.remaining_size
            for order in open_orders
            if (
                order.condition_id == condition_id
                and order.outcome == outcome
                and order.token_id == token_id
                and order.side == Side.BUY
            )
        )

    @staticmethod
    def _inventory_imbalance_effect(token: Token, side: Side, size: float) -> float:
        signed_size = size if side == Side.BUY else -size
        return signed_size if token == Token.YES else -signed_size

    @staticmethod
    def _reserved_buy_cash(open_orders: Iterable[Order]) -> float:
        return sum(
            order.price * order.remaining_size
            for order in open_orders
            if order.side == Side.BUY
        )

    def _reject(
        self,
        reason: str,
        market_snapshot: MarketSnapshot,
        outcome: Token,
        side: Side,
        price: float,
        size: float,
        iteration: int | None = None,
    ) -> tuple[bool, str]:
        self.rejected_orders += 1
        market_position = self.position_manager.ensure_market(market_snapshot)
        with log_context(
            event="risk_rejection",
            iteration=iteration,
            market_slug=market_snapshot.market_slug,
            condition_id=market_snapshot.condition_id,
        ):
            self.logger.warning("Risk rejection | %s", reason)
        if self.event_logger is not None:
            self.event_logger.emit(
                "risk_rejection",
                level="WARNING",
                iteration=iteration,
                market_slug=market_snapshot.market_slug,
                condition_id=market_snapshot.condition_id,
                reason=reason,
                token=outcome,
                outcome=outcome,
                side=side,
                price=price,
                size=size,
                current_position=self.position_manager.get_position(
                    market_snapshot.condition_id,
                    outcome,
                ),
                current_cash=self.position_manager.cash,
                inventory_imbalance=market_position.inventory_imbalance,
                max_inventory_imbalance=self.limits.max_inventory_imbalance,
                max_position_per_token=self.limits.max_position_per_token,
            )
        return False, reason
