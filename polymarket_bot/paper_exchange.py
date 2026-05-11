"""Paper exchange that simulates limit-order fills from top-of-book data."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Fill, MarketSnapshot, Order, OrderStatus, Side
from .logger import StructuredEventLogger, fill_event_fields, log_context


@dataclass(slots=True)
class PaperExchange:
    """Simulate execution of active limit orders against market snapshots."""

    logger: logging.Logger
    event_logger: StructuredEventLogger | None = None
    allow_partial_fills: bool = True

    def match_order(
        self,
        order: Order,
        market_snapshot: MarketSnapshot,
        iteration: int | None = None,
    ) -> Fill | None:
        """Return a fill if the order crosses the current top of book.

        BUY limit orders execute when their limit price is at or above the
        current best ask. SELL limit orders execute when their limit price is at
        or below the current best bid.
        """

        if order.status not in {OrderStatus.OPEN, OrderStatus.PARTIAL}:
            return None
        if order.condition_id != market_snapshot.condition_id:
            return None
        if order.token_id != market_snapshot.token_id(order.outcome):
            return None

        token_book = market_snapshot.token_snapshot(order.outcome)
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
            condition_id=order.condition_id,
            market_slug=order.market_slug,
            token_id=order.token_id,
            outcome=order.outcome,
            side=order.side,
            price=order.price,
            size=round(fill_size, 8),
        )
        seconds_to_expiry = _seconds_to_expiry(market_snapshot)
        with log_context(
            event="fill",
            iteration=iteration,
            market_slug=fill.market_slug,
            condition_id=fill.condition_id,
        ):
            self.logger.info(
                "Paper fill | order=%s condition_id=%s %s %s token_id=%s "
                "size=%.4f price=%.3f notional=%.3f",
                fill.order_id,
                fill.condition_id,
                fill.side,
                fill.outcome,
                fill.token_id,
                fill.size,
                fill.price,
                fill.notional,
            )
        if self.event_logger is not None:
            self.event_logger.emit(
                "fill",
                iteration=iteration,
                **fill_event_fields(fill, time_to_expiry=seconds_to_expiry),
            )
        return fill


def _seconds_to_expiry(snapshot: MarketSnapshot) -> float | None:
    if snapshot.expiry_time is None:
        return None
    expiry_time = snapshot.expiry_time
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)
    return (expiry_time - datetime.now(timezone.utc)).total_seconds()
