"""Market-specific position and PnL tracking for paper fills."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .models import Fill, MarketSnapshot, Side, Token

EPSILON = 1e-9


class PositionAccountingError(RuntimeError):
    """Raised when a fill would violate spot inventory accounting."""


@dataclass(slots=True)
class MarketPosition:
    """Spot inventory and PnL for one Polymarket condition."""

    condition_id: str
    market_slug: str
    yes_token_id: str
    no_token_id: str
    expiry_time: datetime | None = None
    yes_position: float = 0.0
    no_position: float = 0.0
    yes_avg_cost: float = 0.0
    no_avg_cost: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    equity: float = 0.0
    cash_flow: float = 0.0
    last_yes_mark_price: float = 0.0
    last_no_mark_price: float = 0.0
    unresolved_inventory: bool = False
    unresolved_reason: str | None = None

    @property
    def inventory_imbalance(self) -> float:
        """Return YES inventory minus NO inventory for this condition."""

        return self.yes_position - self.no_position

    def get_position(self, outcome: Token) -> float:
        """Return current spot position for one outcome."""

        return self.yes_position if outcome == Token.YES else self.no_position

    def get_avg_cost(self, outcome: Token) -> float:
        """Return average cost for one outcome."""

        return self.yes_avg_cost if outcome == Token.YES else self.no_avg_cost

    def set_position(self, outcome: Token, value: float) -> None:
        """Set position for one outcome."""

        if outcome == Token.YES:
            self.yes_position = value
        else:
            self.no_position = value

    def set_avg_cost(self, outcome: Token, value: float) -> None:
        """Set average cost for one outcome."""

        if outcome == Token.YES:
            self.yes_avg_cost = value
        else:
            self.no_avg_cost = value


@dataclass(slots=True)
class PositionManager:
    """Track cash and market-specific spot inventory ledgers."""

    logger: logging.Logger
    initial_cash: float = 1_000.0
    positions_by_condition_id: dict[str, MarketPosition] = field(default_factory=dict)
    cash: float = field(init=False)
    cash_spent: float = 0.0
    fills_history: list[Fill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash

    @property
    def realized_pnl(self) -> float:
        """Return realized PnL across all markets."""

        return sum(position.realized_pnl for position in self.positions_by_condition_id.values())

    @property
    def unrealized_pnl(self) -> float:
        """Return unrealized PnL across all marked markets."""

        return sum(position.unrealized_pnl for position in self.positions_by_condition_id.values())

    @property
    def equity(self) -> float:
        """Return total marked equity across all markets."""

        return self.cash + sum(
            position.yes_position * position.last_yes_mark_price
            + position.no_position * position.last_no_mark_price
            for position in self.positions_by_condition_id.values()
        )

    def ensure_market(self, market_snapshot: MarketSnapshot) -> MarketPosition:
        """Create or update the position ledger for a snapshot's condition."""

        position = self.positions_by_condition_id.get(market_snapshot.condition_id)
        if position is None:
            position = MarketPosition(
                condition_id=market_snapshot.condition_id,
                market_slug=market_snapshot.market_slug,
                yes_token_id=market_snapshot.yes_token_id,
                no_token_id=market_snapshot.no_token_id,
                expiry_time=market_snapshot.expiry_time,
            )
            self.positions_by_condition_id[market_snapshot.condition_id] = position
            self.logger.info(
                "Created market position ledger | condition_id=%s market_slug=%s "
                "yes_token_id=%s no_token_id=%s expiry_time=%s",
                position.condition_id,
                position.market_slug,
                position.yes_token_id,
                position.no_token_id,
                position.expiry_time,
            )
        else:
            position.market_slug = market_snapshot.market_slug
            position.yes_token_id = market_snapshot.yes_token_id
            position.no_token_id = market_snapshot.no_token_id
            position.expiry_time = market_snapshot.expiry_time
        return position

    def get_market_position(self, condition_id: str) -> MarketPosition:
        """Return an existing market position ledger."""

        try:
            return self.positions_by_condition_id[condition_id]
        except KeyError as exc:
            raise PositionAccountingError(f"unknown condition_id={condition_id}") from exc

    def get_position(self, condition_id: str, outcome: Token) -> float:
        """Return current spot position for a specific condition/outcome."""

        position = self.positions_by_condition_id.get(condition_id)
        if position is None:
            return 0.0
        return position.get_position(outcome)

    def get_avg_cost(self, condition_id: str, outcome: Token) -> float:
        """Return average cost for a specific condition/outcome."""

        position = self.positions_by_condition_id.get(condition_id)
        if position is None:
            return 0.0
        return position.get_avg_cost(outcome)

    def apply_fill(self, fill: Fill) -> None:
        """Apply a fill to cash and the matching market-specific ledger."""

        if fill.size <= 0:
            raise PositionAccountingError(f"fill size must be positive, got {fill.size}")

        position = self.positions_by_condition_id.get(fill.condition_id)
        if position is None:
            position = MarketPosition(
                condition_id=fill.condition_id,
                market_slug=fill.market_slug,
                yes_token_id=fill.token_id if fill.outcome == Token.YES else "",
                no_token_id=fill.token_id if fill.outcome == Token.NO else "",
            )
            self.positions_by_condition_id[fill.condition_id] = position

        if fill.outcome == Token.YES and not position.yes_token_id:
            position.yes_token_id = fill.token_id
        if fill.outcome == Token.NO and not position.no_token_id:
            position.no_token_id = fill.token_id

        if fill.side == Side.BUY:
            self._apply_buy(position, fill)
        else:
            self._apply_sell(position, fill)

        self._assert_non_negative_positions(position)
        self.fills_history.append(fill)
        self.logger.info(
            "Position fill applied | condition_id=%s market_slug=%s %s %s token_id=%s "
            "size=%.4f price=%.3f cash=%.2f yes_position=%.4f no_position=%.4f "
            "yes_avg_cost=%.4f no_avg_cost=%.4f realized_pnl=%.2f",
            fill.condition_id,
            fill.market_slug,
            fill.side,
            fill.outcome,
            fill.token_id,
            fill.size,
            fill.price,
            self.cash,
            position.yes_position,
            position.no_position,
            position.yes_avg_cost,
            position.no_avg_cost,
            position.realized_pnl,
        )

    def apply_fills(self, fills: list[Fill]) -> None:
        """Apply multiple fills in order."""

        for fill in fills:
            self.apply_fill(fill)

    def update_unrealized_pnl(self, market_snapshot: MarketSnapshot) -> float:
        """Mark one condition to current midpoints and return its unrealized PnL."""

        position = self.ensure_market(market_snapshot)
        return self.update_market_marks(
            condition_id=market_snapshot.condition_id,
            yes_mark_price=market_snapshot.yes.midpoint,
            no_mark_price=market_snapshot.no.midpoint,
        )

    def update_market_marks(
        self,
        condition_id: str,
        yes_mark_price: float,
        no_mark_price: float,
    ) -> float:
        """Update mark-to-market PnL and market equity for one condition."""

        position = self.get_market_position(condition_id)
        position.last_yes_mark_price = yes_mark_price
        position.last_no_mark_price = no_mark_price
        position.unrealized_pnl = (
            position.yes_position * (yes_mark_price - position.yes_avg_cost)
            + position.no_position * (no_mark_price - position.no_avg_cost)
        )
        position.equity = (
            position.cash_flow
            + position.yes_position * yes_mark_price
            + position.no_position * no_mark_price
        )
        return position.unrealized_pnl

    def archive_unresolved_inventory(self, condition_id: str, reason: str) -> None:
        """Mark a market ledger as carrying unresolved inventory."""

        position = self.get_market_position(condition_id)
        if position.unresolved_inventory:
            return
        if position.yes_position <= EPSILON and position.no_position <= EPSILON:
            return
        position.unresolved_inventory = True
        position.unresolved_reason = reason
        self.logger.warning(
            "Archived unresolved inventory | condition_id=%s market_slug=%s "
            "yes_position=%.4f no_position=%.4f reason=%s",
            position.condition_id,
            position.market_slug,
            position.yes_position,
            position.no_position,
            reason,
        )

    def market_summary(self, condition_id: str) -> str:
        """Return a compact summary for one condition."""

        position = self.get_market_position(condition_id)
        return (
            f"current_market_slug={position.market_slug} condition_id={position.condition_id} "
            f"yes_token_id={position.yes_token_id} no_token_id={position.no_token_id} "
            f"yes_position={position.yes_position:.4f} no_position={position.no_position:.4f} "
            f"yes_avg_cost={position.yes_avg_cost:.4f} no_avg_cost={position.no_avg_cost:.4f} "
            f"imbalance={position.inventory_imbalance:.4f} "
            f"realized_pnl={position.realized_pnl:.2f} unrealized_pnl={position.unrealized_pnl:.2f} "
            f"market_equity={position.equity:.2f} unresolved_inventory={position.unresolved_inventory}"
        )

    def summary(self) -> str:
        """Return a compact portfolio-level summary."""

        return (
            f"starting_cash={self.initial_cash:.2f} cash={self.cash:.2f} "
            f"markets={len(self.positions_by_condition_id)} "
            f"realized_pnl={self.realized_pnl:.2f} unrealized_pnl={self.unrealized_pnl:.2f} "
            f"total_equity={self.equity:.2f}"
        )

    def _apply_buy(self, position: MarketPosition, fill: Fill) -> None:
        current_position = position.get_position(fill.outcome)
        current_avg_cost = position.get_avg_cost(fill.outcome)
        new_position = current_position + fill.size
        new_avg_cost = (
            (current_position * current_avg_cost + fill.notional) / new_position
            if new_position > EPSILON
            else 0.0
        )

        position.set_position(fill.outcome, new_position)
        position.set_avg_cost(fill.outcome, new_avg_cost)
        self.cash -= fill.notional
        self.cash_spent += fill.notional
        position.cash_flow -= fill.notional

    def _apply_sell(self, position: MarketPosition, fill: Fill) -> None:
        current_position = position.get_position(fill.outcome)
        if current_position + EPSILON < fill.size:
            reason = (
                f"critical accounting error: attempted SELL {fill.size:.8f} "
                f"{fill.outcome} for condition_id={fill.condition_id} "
                f"with position {current_position:.8f}"
            )
            self.logger.critical(reason)
            raise PositionAccountingError(reason)

        avg_cost = position.get_avg_cost(fill.outcome)
        new_position = max(current_position - fill.size, 0.0)
        position.set_position(fill.outcome, new_position)
        if new_position <= EPSILON:
            position.set_avg_cost(fill.outcome, 0.0)

        self.cash += fill.notional
        position.cash_flow += fill.notional
        position.realized_pnl += (fill.price - avg_cost) * fill.size

    def _assert_non_negative_positions(self, position: MarketPosition) -> None:
        if position.yes_position < -EPSILON or position.no_position < -EPSILON:
            reason = (
                f"critical accounting error: negative position detected "
                f"condition_id={position.condition_id} yes_position={position.yes_position:.8f} "
                f"no_position={position.no_position:.8f}"
            )
            self.logger.critical(reason)
            raise PositionAccountingError(reason)
