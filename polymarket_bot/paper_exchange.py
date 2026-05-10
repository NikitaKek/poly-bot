"""Paper exchange that simulates limit-order fills from top-of-book data."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import Fill, MarketSnapshot, Order, OrderStatus, Side


@dataclass(slots=True)
class PaperExchange:
    """Simulate execution of active limit orders against market snapshots."""

    logger: logging.Logger
    allow_partial_fills: bool = True

    def match_order(self, order: Order, market_snapshot: MarketSnapshot) -> Fill | None:
        """Return a fill if the order crosses the current top of book.

        BUY limit orders execute when their limit price is at or above the
        current best ask. SELL limit orders execute when their limit price is at
        or below the current best bid.
        """

        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
            return None

        token_book = market_snapshot.token_snapshot(order.token)
        remaining = order.remaining_size
        if remaining <= 0:
            return None

        if order.side == Side.BUY:
            if order.price < token_book.best_ask:
                return None
            liquidity = token_book.ask_size
        else:
            if order.price > token_book.best_bid:
                return None
            liquidity = token_book.bid_size

        fill_size = min(remaining, liquidity) if self.allow_partial_fills else remaining
        if fill_size <= 0:
            return None

        fill = Fill(
            order_id=order.order_id,
            token=order.token,
            side=order.side,
            price=order.price,
            size=round(fill_size, 8),
        )
        self.logger.info(
            "Paper fill | order=%s %s %s size=%.4f price=%.3f notional=%.3f",
            fill.order_id,
            fill.side,
            fill.token,
            fill.size,
            fill.price,
            fill.notional,
        )
        return fill
