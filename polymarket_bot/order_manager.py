"""Order management for paper limit orders."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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
    orders: dict[str, Order] = field(default_factory=dict)

    def place_limit_order(self, token: Token, side: Side, price: float, size: float) -> Order | None:
        """Create and store a new paper limit order if risk checks pass."""

        if self.risk_manager is not None:
            accepted, reason = self.risk_manager.validate_order(
                token=token,
                side=side,
                price=price,
                size=size,
                open_orders=self.get_open_orders(),
            )
            if not accepted:
                self.logger.info(
                    "Order rejected by risk | %s %s size=%.4f price=%.3f reason=%s",
                    side,
                    token,
                    size,
                    price,
                    reason,
                )
                return None

        order = Order(token=token, side=side, price=round(price, 3), size=round(size, 8))
        self.orders[order.order_id] = order
        self.logger.info(
            "Placed paper order | order=%s %s %s size=%.4f price=%.3f",
            order.order_id,
            order.side,
            order.token,
            order.size,
            order.price,
        )
        return order

    def cancel_order(self, order_id: str) -> bool:
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
        self.logger.info("Cancelled order | order=%s", order.order_id)
        return True

    def get_open_orders(self) -> list[Order]:
        """Return all currently active orders."""

        return [
            order
            for order in self.orders.values()
            if order.status in {OrderStatus.OPEN, OrderStatus.PARTIAL}
        ]

    def get_all_orders(self) -> list[Order]:
        """Return every order known to the manager."""

        return list(self.orders.values())

    def update_orders_from_market(self, market_snapshot: MarketSnapshot) -> list[Fill]:
        """Match open orders against the latest market snapshot."""

        fills: list[Fill] = []
        for order in self.get_open_orders():
            try:
                fill = self.paper_exchange.match_order(order, market_snapshot)
            except Exception:
                self.logger.exception("Error while matching order | order=%s", order.order_id)
                continue

            if fill is None:
                continue

            order.apply_fill(fill.size)
            fills.append(fill)
            self.logger.info(
                "Order updated | order=%s status=%s filled=%.4f remaining=%.4f",
                order.order_id,
                order.status,
                order.filled_size,
                order.remaining_size,
            )

        return fills
