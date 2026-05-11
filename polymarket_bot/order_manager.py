"""Order management for paper limit orders."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .logger import StructuredEventLogger, log_context, order_event_fields
from .models import Fill, MarketSnapshot, Order, OrderStatus, Side, Token
from .paper_exchange import PaperExchange

if TYPE_CHECKING:
    from .risk_manager import RiskManager


@dataclass(slots=True)
class OrderManager:
    """Track order state and update orders from paper exchange fills."""

    paper_exchange: PaperExchange
    logger: logging.Logger
    risk_manager: RiskManager | None = None
    event_logger: StructuredEventLogger | None = None
    orders: dict[str, Order] = field(default_factory=dict)

    def place_limit_order(
        self,
        market_snapshot: MarketSnapshot,
        outcome: Token,
        side: Side,
        price: float,
        size: float,
        iteration: int | None = None,
        reason: str = "test_quote",
    ) -> Order | None:
        """Create and store a new paper limit order if risk checks pass."""

        if self.risk_manager is not None:
            accepted, rejection_reason = self.risk_manager.validate_order(
                market_snapshot=market_snapshot,
                outcome=outcome,
                side=side,
                price=price,
                size=size,
                open_orders=self.get_open_orders(),
                iteration=iteration,
            )
            if not accepted:
                with log_context(
                    event="risk_rejection",
                    iteration=iteration,
                    market_slug=market_snapshot.market_slug,
                    condition_id=market_snapshot.condition_id,
                ):
                    self.logger.info(
                        "Order rejected by risk | condition_id=%s market_slug=%s %s %s "
                        "token_id=%s size=%.4f price=%.3f reason=%s",
                        market_snapshot.condition_id,
                        market_snapshot.market_slug,
                        side,
                        outcome,
                        market_snapshot.token_id(outcome),
                        size,
                        price,
                        rejection_reason,
                    )
                return None

        order = Order(
            condition_id=market_snapshot.condition_id,
            market_slug=market_snapshot.market_slug,
            token_id=market_snapshot.token_id(outcome),
            outcome=outcome,
            side=side,
            price=round(price, 3),
            size=round(size, 8),
        )
        self.orders[order.order_id] = order
        with log_context(
            event="order_placed",
            iteration=iteration,
            market_slug=order.market_slug,
            condition_id=order.condition_id,
        ):
            self.logger.info(
                "Placed paper order | order=%s condition_id=%s market_slug=%s %s %s "
                "token_id=%s size=%.4f price=%.3f",
                order.order_id,
                order.condition_id,
                order.market_slug,
                order.side,
                order.outcome,
                order.token_id,
                order.size,
                order.price,
            )
        if self.event_logger is not None:
            self.event_logger.emit(
                "order_placed",
                iteration=iteration,
                **order_event_fields(order, reason=reason),
            )
        return order

    def cancel_order(
        self,
        order_id: str,
        cancel_reason: str = "manual",
        iteration: int | None = None,
    ) -> bool:
        """Cancel an active order by id. Returns True when cancellation succeeds."""

        order = self.orders.get(order_id)
        if order is None:
            self.logger.warning("Cancel rejected | unknown order_id=%s", order_id)
            return False

        if order.status in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
            self.logger.warning(
                "Cancel rejected | order=%s status=%s",
                order.order_id,
                order.status,
            )
            return False

        order.cancel()
        with log_context(
            event="order_cancelled",
            iteration=iteration,
            market_slug=order.market_slug,
            condition_id=order.condition_id,
        ):
            self.logger.info(
                "Cancelled order | order=%s reason=%s remaining=%.4f",
                order.order_id,
                cancel_reason,
                order.remaining_size,
            )
        if self.event_logger is not None:
            self.event_logger.emit(
                "order_cancelled",
                iteration=iteration,
                **order_event_fields(order, cancel_reason=cancel_reason),
            )
        return True

    def get_open_orders(self) -> list[Order]:
        """Return all currently active orders."""

        return [
            order
            for order in self.orders.values()
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}
        ]

    def get_open_orders_for_condition(self, condition_id: str) -> list[Order]:
        """Return active orders for one condition."""

        return [order for order in self.get_open_orders() if order.condition_id == condition_id]

    def get_all_orders(self) -> list[Order]:
        """Return every order known to the manager."""

        return list(self.orders.values())

    def update_orders_from_market(
        self,
        market_snapshot: MarketSnapshot,
        iteration: int | None = None,
    ) -> list[Fill]:
        """Match open orders against the latest market snapshot."""

        fills: list[Fill] = []
        for order in self.get_open_orders():
            try:
                fill = self.paper_exchange.match_order(order, market_snapshot, iteration=iteration)
            except Exception:
                with log_context(
                    event="error",
                    iteration=iteration,
                    market_slug=market_snapshot.market_slug,
                    condition_id=market_snapshot.condition_id,
                ):
                    self.logger.exception("Error while matching order | order=%s", order.order_id)
                if self.event_logger is not None:
                    self.event_logger.emit(
                        "error",
                        level="ERROR",
                        iteration=iteration,
                        market_slug=market_snapshot.market_slug,
                        condition_id=market_snapshot.condition_id,
                        order_id=order.order_id,
                        error="error while matching order",
                    )
                continue

            if fill is None:
                continue

            order.apply_fill(fill.size)
            fills.append(fill)
            with log_context(
                event="fill",
                iteration=iteration,
                market_slug=order.market_slug,
                condition_id=order.condition_id,
            ):
                self.logger.info(
                    "Order updated | order=%s condition_id=%s status=%s filled=%.4f remaining=%.4f",
                    order.order_id,
                    order.condition_id,
                    order.status,
                    order.filled_size,
                    order.remaining_size,
                )

        return fills
