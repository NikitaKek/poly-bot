"""Tests for public Polymarket market data parsing and conversion."""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone

from polymarket_bot.config import BotConfig
from polymarket_bot.main import _create_market_data_provider
from polymarket_bot.market_data import MockMarketDataGenerator
from polymarket_bot.models import Token
from polymarket_bot.polymarket_market_data import (
    MarketDiscoveryClient,
    PolymarketOrderBookClient,
    generate_btc_15m_slug_candidates,
    parse_clob_token_ids,
    select_yes_no_token_ids,
)


def test_logger() -> logging.Logger:
    logger = logging.getLogger("polymarket_market_data_tests")
    logger.addHandler(logging.NullHandler())
    return logger


class FakeMarketDiscoveryClient(MarketDiscoveryClient):
    def _fetch_active_markets(self) -> list[dict[str, object]]:
        return [
            {
                "question": "Will bitcoin hit $1m before GTA VI?",
                "slug": "will-bitcoin-hit-1m-before-gta-vi-872",
                "conditionId": "condition-btc-wrong",
                "clobTokenIds": '["wrong-yes", "wrong-no"]',
            },
            {
                "question": "Bitcoin Up or Down - 15 Minute",
                "slug": "btc-updown-15m-123",
                "conditionId": "condition-btc",
                "clobTokenIds": '["btc-yes", "btc-no"]',
            },
        ]


class FakeTimestampSlugDiscoveryClient(MarketDiscoveryClient):
    def _fetch_market_from_event_slug(self, slug: str) -> dict[str, object] | None:
        if not slug.startswith("btc-updown-15m-"):
            return None
        return {
            "question": "Bitcoin Up or Down - May 10, 7:00AM-7:15AM ET",
            "slug": slug,
            "conditionId": "condition-timestamp",
            "clobTokenIds": '["timestamp-yes", "timestamp-no"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
        }

    def _fetch_market_from_market_slug(self, slug: str) -> dict[str, object] | None:
        return None

    def _fetch_active_slug_markets(self, slug_contains: str) -> list[dict[str, object]]:
        return []

    def _fetch_active_event_markets(self, slug_contains: str) -> list[dict[str, object]]:
        return []


class PolymarketMarketDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = test_logger()
        self.orderbook_client = PolymarketOrderBookClient(
            logger=self.logger,
            clob_base_url="https://clob.example.test",
        )

    def test_parse_clob_token_ids_from_json_string(self) -> None:
        token_ids = parse_clob_token_ids('["yes-token", "no-token"]')

        self.assertEqual(token_ids, ["yes-token", "no-token"])

    def test_parse_clob_token_ids_from_list(self) -> None:
        token_ids = parse_clob_token_ids(["yes-token", "no-token"])

        self.assertEqual(token_ids, ["yes-token", "no-token"])

    def test_select_yes_no_token_ids_uses_first_two_tokens(self) -> None:
        yes_token_id, no_token_id = select_yes_no_token_ids(["yes-token", "no-token", "extra"])

        self.assertEqual(yes_token_id, "yes-token")
        self.assertEqual(no_token_id, "no-token")

    def test_generate_btc_15m_slug_candidates_aligns_to_timestamp_window(self) -> None:
        now = datetime.fromtimestamp(1778410756, tz=timezone.utc)
        slugs = generate_btc_15m_slug_candidates(now=now, window_offsets=(0, 1, -1))

        self.assertEqual(slugs[0], "btc-updown-15m-1778409900")
        self.assertEqual(slugs[1], "btc-up-or-down-15m-1778409900")
        self.assertIn("btc-updown-15m-1778410800", slugs)
        self.assertIn("btc-updown-15m-1778409000", slugs)

    def test_market_discovery_selects_btc_15m_market(self) -> None:
        discovery_client = FakeMarketDiscoveryClient(
            logger=self.logger,
            gamma_base_url="https://gamma.example.test",
            btc_market_query="btc 15m bitcoin",
        )

        market = discovery_client.discover_btc_15m_market()

        self.assertEqual(market.question, "Bitcoin Up or Down - 15 Minute")
        self.assertEqual(market.slug, "btc-updown-15m-123")
        self.assertEqual(market.condition_id, "condition-btc")
        self.assertEqual(market.yes_token_id, "btc-yes")
        self.assertEqual(market.no_token_id, "btc-no")

    def test_market_discovery_uses_timestamp_slug_candidates(self) -> None:
        discovery_client = FakeTimestampSlugDiscoveryClient(
            logger=self.logger,
            gamma_base_url="https://gamma.example.test",
            btc_market_query="btc 15m bitcoin",
            max_pages=0,
        )

        market = discovery_client.discover_btc_15m_market()

        self.assertTrue(market.slug.startswith("btc-updown-15m-"))
        self.assertEqual(market.condition_id, "condition-timestamp")
        self.assertEqual(market.yes_token_id, "timestamp-yes")
        self.assertEqual(market.no_token_id, "timestamp-no")

    def test_orderbook_to_market_snapshot_levels(self) -> None:
        orderbook = {
            "bids": [
                {"price": "0.40", "size": "10"},
                {"price": "0.44", "size": "7"},
            ],
            "asks": [
                {"price": "0.48", "size": "8"},
                {"price": "0.46", "size": "3"},
            ],
            "last_trade_price": "0.45",
        }

        snapshot = self.orderbook_client.orderbook_to_token_snapshot(
            orderbook=orderbook,
            token=Token.YES,
            token_id="yes-token",
        )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.best_bid, 0.44)
        self.assertEqual(snapshot.bid_size, 7.0)
        self.assertEqual(snapshot.best_ask, 0.46)
        self.assertEqual(snapshot.ask_size, 3.0)
        self.assertEqual(snapshot.last_trade_price, 0.45)

    def test_empty_orderbook_returns_none(self) -> None:
        snapshot = self.orderbook_client.orderbook_to_token_snapshot(
            orderbook={"bids": [], "asks": [{"price": "0.46", "size": "3"}]},
            token=Token.YES,
            token_id="yes-token",
        )

        self.assertIsNone(snapshot)

    def test_mock_mode_still_works(self) -> None:
        provider = _create_market_data_provider(
            config=BotConfig(market_data_mode="mock"),
            logger=self.logger,
        )

        self.assertIsInstance(provider, MockMarketDataGenerator)
        self.assertIsNotNone(provider.get_snapshot())


if __name__ == "__main__":
    unittest.main()
