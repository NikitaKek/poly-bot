"""Typed domain models used across the paper-trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4


class Token(StrEnum):
    """Binary market outcome token."""

    YES = "YES"
    NO = "NO"


class Side(StrEnum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    """Order lifecycle status."""

    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


def utc_now() -> datetime:
    """Return timezone-aware current UTC time."""

    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TokenMarketSnapshot:
    """Top-of-book snapshot for one outcome token."""

    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    last_trade_price: float
    timestamp: datetime = field(default_factory=utc_now)

    @property
    def midpoint(self) -> float:
        """Return the top-of-book midpoint."""

        return (self.best_bid + self.best_ask) / 2.0


@dataclass(slots=True)
class MarketSnapshot:
    """Market snapshot containing YES and NO token books."""

    yes: TokenMarketSnapshot
    no: TokenMarketSnapshot
    timestamp: datetime = field(default_factory=utc_now)

    def token_snapshot(self, token: Token) -> TokenMarketSnapshot:
        """Return the snapshot for a specific token."""

        return self.yes if token == Token.YES else self.no


@dataclass(slots=True)
class Order:
    """Paper limit order tracked by the order manager."""

    token: Token
    side: Side
    price: float
    size: float
    order_id: str = field(default_factory=lambda: str(uuid4()))
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def remaining_size(self) -> float:
        """Return the unfilled quantity."""

        return max(self.size - self.filled_size, 0.0)

    def apply_fill(self, filled_size: float) -> None:
        """Apply a fill to the order and update its lifecycle status."""

        if filled_size <= 0:
            return

        self.filled_size = min(self.size, self.filled_size + filled_size)
        self.updated_at = utc_now()

        if self.remaining_size <= 1e-12:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIAL

    def cancel(self) -> None:
        """Cancel the order if it is still active."""

        if self.status in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
            return
        self.status = OrderStatus.CANCELLED
        self.updated_at = utc_now()


@dataclass(frozen=True, slots=True)
class Fill:
    """Execution report emitted by the paper exchange."""

    order_id: str
    token: Token
    side: Side
    price: float
    size: float
    timestamp: datetime = field(default_factory=utc_now)

    @property
    def notional(self) -> float:
        """Return fill notional in quote currency."""

        return self.price * self.size
