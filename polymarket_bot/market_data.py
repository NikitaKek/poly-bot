"""Mock market data for Polymarket-style 15-minute BTCUSDT markets."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta

from .logger import log_context
from .models import MarketSnapshot, TokenMarketSnapshot, utc_now


@dataclass(slots=True)
class MockMarketDataGenerator:
    """Generate internally consistent YES/NO top-of-book snapshots.

    The generator models a binary prediction market where YES and NO midpoint
    prices are approximately complementary. It is deliberately simple and does
    not represent real Polymarket liquidity.
    """

    logger: logging.Logger
    seed: int | None = 42
    fair_yes: float = 0.50
    volatility: float = 0.025
    min_price: float = 0.02
    max_price: float = 0.98
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def get_snapshot(self) -> MarketSnapshot:
        """Return a fresh mock market snapshot."""

        self._move_fair_value()
        spread_yes = self._rng.uniform(0.01, 0.03)
        spread_no = self._rng.uniform(0.01, 0.03)

        yes_mid = self.fair_yes
        no_mid = 1.0 - yes_mid

        yes = self._make_token_snapshot(yes_mid, spread_yes)
        no = self._make_token_snapshot(no_mid, spread_no)

        now = utc_now()
        snapshot = MarketSnapshot(
            yes=yes,
            no=no,
            condition_id="mock-btc-15m-condition",
            market_slug="mock-btc-15m",
            yes_token_id="mock-btc-15m-yes",
            no_token_id="mock-btc-15m-no",
            market_question="Mock BTC 15-minute prediction market",
            expiry_time=now + timedelta(minutes=15),
            timestamp=now,
        )
        with log_context(
            event="snapshot",
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
        ):
            self.logger.info(
                "Market snapshot | YES %.3f/%.3f size %.2f/%.2f | NO %.3f/%.3f size %.2f/%.2f",
                snapshot.yes.best_bid,
                snapshot.yes.best_ask,
                snapshot.yes.bid_size,
                snapshot.yes.ask_size,
                snapshot.no.best_bid,
                snapshot.no.best_ask,
                snapshot.no.bid_size,
                snapshot.no.ask_size,
            )
        return snapshot

    def _move_fair_value(self) -> None:
        step = self._rng.gauss(0.0, self.volatility)
        self.fair_yes = min(max(self.fair_yes + step, 0.05), 0.95)

    def _make_token_snapshot(self, midpoint: float, spread: float) -> TokenMarketSnapshot:
        half_spread = spread / 2.0
        best_bid = min(max(midpoint - half_spread, self.min_price), self.max_price)
        best_ask = min(max(midpoint + half_spread, self.min_price), self.max_price)

        if best_bid >= best_ask:
            best_bid = max(best_ask - 0.01, self.min_price)

        bid_size = self._rng.uniform(5.0, 25.0)
        ask_size = self._rng.uniform(5.0, 25.0)
        last_trade_price = min(
            max(self._rng.uniform(best_bid, best_ask), self.min_price),
            self.max_price,
        )

        return TokenMarketSnapshot(
            best_bid=round(best_bid, 3),
            best_ask=round(best_ask, 3),
            bid_size=round(bid_size, 3),
            ask_size=round(ask_size, 3),
            last_trade_price=round(last_trade_price, 3),
            timestamp=utc_now(),
        )
