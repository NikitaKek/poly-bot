"""Tests for session-level structured logging artifacts."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from polymarket_bot.config import BotConfig
from polymarket_bot.logger import (
    StructuredEventLogger,
    create_session_log_paths,
    fill_event_fields,
    generate_session_id,
    setup_logger,
    snapshot_event_fields,
)
from polymarket_bot.models import Fill, MarketSnapshot, Side, Token, TokenMarketSnapshot


def sample_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        yes=TokenMarketSnapshot(
            best_bid=0.40,
            best_ask=0.42,
            bid_size=100.0,
            ask_size=101.0,
            last_trade_price=0.41,
        ),
        no=TokenMarketSnapshot(
            best_bid=0.58,
            best_ask=0.60,
            bid_size=90.0,
            ask_size=91.0,
            last_trade_price=0.59,
        ),
        condition_id="condition-a",
        market_slug="market-a",
        yes_token_id="yes-a",
        no_token_id="no-a",
        market_question="Will BTC be up?",
    )


class SessionLoggingTests(unittest.TestCase):
    def test_session_id_format(self) -> None:
        session_id = generate_session_id("polymarket", datetime(2026, 5, 10, 14, 51, 1))

        self.assertEqual(session_id, "20260510_145101_polymarket_paper")
        self.assertRegex(session_id, r"^\d{8}_\d{6}_polymarket_paper$")

    def test_session_directory_structure_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = create_session_log_paths("20260510_145101_mock_paper", logs_root=temp_dir)
            event_logger = StructuredEventLogger(paths.session_id, paths, "mock")
            event_logger.write_config(BotConfig())
            event_logger.write_summary({"session_id": paths.session_id})
            logger = setup_logger(
                name="polymarket_bot_test_structure",
                log_file=paths.bot_log_path,
                session_id=paths.session_id,
            )
            logger.info("structure test")
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

            self.assertTrue(paths.session_dir.is_dir())
            self.assertTrue(paths.markets_dir.is_dir())
            self.assertTrue(paths.csv_dir.is_dir())
            self.assertTrue(paths.bot_log_path.is_file())
            self.assertTrue(paths.events_jsonl_path.is_file())
            self.assertTrue(paths.config_json_path.is_file())
            self.assertTrue(paths.summary_json_path.is_file())

    def test_events_jsonl_contains_valid_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_logger = self._event_logger(temp_dir)

            event_logger.emit("session_started", market_data_mode="mock", starting_cash=1_000.0)

            lines = event_logger.paths.events_jsonl_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            parsed = json.loads(lines[0])
            self.assertEqual(parsed["event"], "session_started")
            self.assertEqual(parsed["session_id"], event_logger.session_id)

    def test_snapshot_event_contains_yes_no_bid_ask(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_logger = self._event_logger(temp_dir)
            snapshot = sample_snapshot()

            event_logger.emit(
                "snapshot",
                iteration=1,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
                **snapshot_event_fields(snapshot, time_to_expiry=120.0),
            )

            parsed = json.loads(event_logger.paths.events_jsonl_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["yes_bid"], 0.40)
            self.assertEqual(parsed["yes_ask"], 0.42)
            self.assertEqual(parsed["no_bid"], 0.58)
            self.assertEqual(parsed["no_ask"], 0.60)

    def test_fill_event_contains_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_logger = self._event_logger(temp_dir)
            fill = Fill(
                order_id="order-1",
                condition_id="condition-a",
                market_slug="market-a",
                token_id="yes-a",
                outcome=Token.YES,
                side=Side.BUY,
                price=0.42,
                size=10.0,
            )

            event_logger.emit(
                "fill",
                iteration=2,
                **fill_event_fields(fill, time_to_expiry=60.0),
            )

            parsed = json.loads(event_logger.paths.events_jsonl_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["order_id"], "order-1")
            self.assertEqual(parsed["side"], "BUY")
            self.assertEqual(parsed["price"], 0.42)
            self.assertEqual(parsed["size"], 10.0)
            self.assertEqual(parsed["notional"], 4.2)

    def test_summary_json_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_logger = self._event_logger(temp_dir)

            event_logger.write_summary(
                {
                    "session_id": event_logger.session_id,
                    "ending_cash": 1_005.0,
                    "ending_equity": 1_010.0,
                }
            )

            summary = json.loads(event_logger.paths.summary_json_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["session_id"], event_logger.session_id)
            self.assertEqual(summary["ending_cash"], 1_005.0)

    def test_csv_files_are_created_with_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_logger = self._event_logger(temp_dir)
            csv_paths = [
                event_logger.paths.snapshots_csv_path,
                event_logger.paths.orders_csv_path,
                event_logger.paths.fills_csv_path,
                event_logger.paths.positions_csv_path,
                event_logger.paths.risk_rejections_csv_path,
            ]

            for path in csv_paths:
                with path.open(newline="", encoding="utf-8") as file:
                    reader = csv.reader(file)
                    header = next(reader)
                self.assertGreater(len(header), 0, msg=str(path))
                self.assertIn("session_id", header, msg=str(path))

    @staticmethod
    def _event_logger(temp_dir: str) -> StructuredEventLogger:
        session_id = generate_session_id("mock", datetime(2026, 5, 10, 14, 51, 1))
        paths = create_session_log_paths(session_id, logs_root=Path(temp_dir))
        return StructuredEventLogger(session_id, paths, "mock")


if __name__ == "__main__":
    unittest.main()
