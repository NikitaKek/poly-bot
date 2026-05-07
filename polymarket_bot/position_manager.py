"""Position and PnL tracking for paper fills."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import Fill, MarketSnapshot, Side, Token

EPSILON = 1e-9


class PositionAccountingError(RuntimeError):
    """Raised when a fill would violate spot inventory accounting."""


@dataclass(slots=True)
class PositionManager:
    """Track cash, spot inventory, average cost, fills, and PnL."""

    logger: logging.Logger
    initial_cash: float = 1_000.0
    yes_position: float = 0.0
    no_position: float = 0.0
    yes_avg_cost: float = 0.0
    no_avg_cost: float = 0.0
    cash: float = field(init=False)
    cash_spent: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    equity: float = field(init=False)
    fills_history: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash
        self.equity = self.initial_cash
        self._assert_non_negative_positions()

    @property
    def inventory_imbalance(self) -> float:
        """Return YES inventory minus NO inventory."""

        return self.yes_position - self.no_position

    def get_position(self, token: Token) -> float:
        """Return current position for a token."""

        return self.yes_position if token == Token.YES else self.no_position

    def get_avg_cost(self, token: Token) -> float:
        """Return average inventory cost for a token."""

        return self.yes_avg_cost if token == Token.YES else self.no_avg_cost

    def apply_fill(self, fill: Fill) -> None:
        """Apply a fill to cash, spot inventory, average cost, and realized PnL."""

        notional = fill.notional

        if fill.size <= 0:
            raise PositionAccountingError(f"fill size must be positive, got {fill.size}")

        if fill.side == Side.BUY:
            self._apply_buy(fill)
        else:
            self._apply_sell(fill)

        self._assert_non_negative_positions()
        self.fills_history.append(fill)
        self.logger.info(
            "Position fill applied | %s %s size=%.4f price=%.3f cash=%.2f "
            "yes_position=%.4f no_position=%.4f yes_avg_cost=%.4f no_avg_cost=%.4f realized_pnl=%.2f",
            fill.side,
            fill.token,
            fill.size,
            fill.price,
            self.cash,
            self.yes_position,
            self.no_position,
            self.yes_avg_cost,
            self.no_avg_cost,
            self.realized_pnl,
        )

    def apply_fills(self, fills: list[Fill]) -> None:
        """Apply multiple fills in order."""

        for fill in fills:
            self.apply_fill(fill)

    def update_unrealized_pnl(self, market_snapshot: MarketSnapshot) -> float:
        """Mark open inventory to current midpoints and update unrealized PnL/equity."""

        return self.update_marks(
            yes_mark_price=market_snapshot.yes.midpoint,
            no_mark_price=market_snapshot.no.midpoint,
        )

    def update_marks(self, yes_mark_price: float, no_mark_price: float) -> float:
        """Update mark-to-market PnL and equity from explicit token marks."""

        self.unrealized_pnl = (
            self.yes_position * (yes_mark_price - self.yes_avg_cost)
            + self.no_position * (no_mark_price - self.no_avg_cost)
        )
        self.equity = self.cash + self.yes_position * yes_mark_price + self.no_position * no_mark_price
        return self.unrealized_pnl

    def summary(self) -> str:
        """Return a compact human-readable position summary."""

        return (
            f"starting_cash={self.initial_cash:.2f} cash={self.cash:.2f} "
            f"yes_position={self.yes_position:.4f} no_position={self.no_position:.4f} "
            f"yes_avg_cost={self.yes_avg_cost:.4f} no_avg_cost={self.no_avg_cost:.4f} "
            f"imbalance={self.inventory_imbalance:.4f} "
            f"realized_pnl={self.realized_pnl:.2f} unrealized_pnl={self.unrealized_pnl:.2f} "
            f"equity={self.equity:.2f}"
        )

    def _apply_buy(self, fill: Fill) -> None:
        current_position = self.get_position(fill.token)
        current_avg_cost = self.get_avg_cost(fill.token)
        new_position = current_position + fill.size
        new_avg_cost = (
            (current_position * current_avg_cost + fill.notional) / new_position
            if new_position > EPSILON
            else 0.0
        )

        self._set_position(fill.token, new_position)
        self._set_avg_cost(fill.token, new_avg_cost)
        self.cash -= fill.notional
        self.cash_spent += fill.notional

    def _apply_sell(self, fill: Fill) -> None:
        current_position = self.get_position(fill.token)
        if current_position + EPSILON < fill.size:
            reason = (
                f"critical accounting error: attempted SELL {fill.size:.8f} {fill.token} "
                f"with position {current_position:.8f}"
            )
            self.logger.critical(reason)
            raise PositionAccountingError(reason)

        avg_cost = self.get_avg_cost(fill.token)
        new_position = max(current_position - fill.size, 0.0)
        self._set_position(fill.token, new_position)
        if new_position <= EPSILON:
            self._set_avg_cost(fill.token, 0.0)

        self.cash += fill.notional
        self.realized_pnl += (fill.price - avg_cost) * fill.size

    def _set_position(self, token: Token, value: float) -> None:
        if token == Token.YES:
            self.yes_position = value
        else:
            self.no_position = value

    def _set_avg_cost(self, token: Token, value: float) -> None:
        if token == Token.YES:
            self.yes_avg_cost = value
        else:
            self.no_avg_cost = value

    def _assert_non_negative_positions(self) -> None:
        if self.yes_position < -EPSILON or self.no_position < -EPSILON:
            reason = (
                f"critical accounting error: negative position detected "
                f"yes_position={self.yes_position:.8f} no_position={self.no_position:.8f}"
            )
            self.logger.critical(reason)
            raise PositionAccountingError(reason)
