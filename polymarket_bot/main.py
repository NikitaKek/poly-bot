"""Entry point for the safe paper-trading MVP bot."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Protocol

from .config import DEFAULT_CONFIG, BotConfig
from .logger import (
    StructuredEventLogger,
    create_session_log_paths,
    generate_session_id,
    log_context,
    set_session_context,
    setup_logger,
    snapshot_event_fields,
)
from .market_data import MockMarketDataGenerator
from .models import MarketSnapshot, Order, Side, Token
from .order_manager import OrderManager
from .paper_exchange import PaperExchange
from .position_manager import PositionManager
from .polymarket_market_data import (
    MarketDataError,
    MarketDiscoveryClient,
    PolymarketMarketDataClient,
    PolymarketOrderBookClient,
)
from .risk_manager import RiskLimits, RiskManager

FORCED_EXIT_BLOCK_NEW_SECONDS = DEFAULT_CONFIG.expiry_block_new_seconds
FORCED_EXIT_CANCEL_SECONDS = DEFAULT_CONFIG.expiry_cancel_seconds


class MarketDataProvider(Protocol):
    """Market data provider interface used by the paper bot loop."""

    def get_snapshot(self) -> MarketSnapshot | None:
        """Return the latest market snapshot, or None when data is incomplete."""


def build_components(config: BotConfig) -> tuple[
    logging.Logger,
    StructuredEventLogger,
    MarketDataProvider,
    OrderManager,
    PositionManager,
    RiskManager,
]:
    """Construct the bot's paper-trading components."""

    session_id = generate_session_id(config.market_data_mode)
    log_paths = create_session_log_paths(session_id)
    set_session_context(session_id)
    logger = setup_logger(log_file=log_paths.bot_log_path, session_id=session_id)
    event_logger = StructuredEventLogger(
        session_id=session_id,
        paths=log_paths,
        market_data_mode=config.market_data_mode,
    )
    event_logger.write_config(config)
    market_data = _create_market_data_provider(config=config, logger=logger)
    paper_exchange = PaperExchange(logger=logger, event_logger=event_logger)
    position_manager = PositionManager(logger=logger, initial_cash=config.initial_cash)
    risk_manager = RiskManager(
        limits=RiskLimits(
            max_position_per_token=config.max_position_per_token,
            max_inventory_imbalance=config.max_inventory_imbalance,
            max_order_size=config.max_order_size,
            min_price=config.min_price,
            max_price=config.max_price,
        ),
        position_manager=position_manager,
        logger=logger,
        event_logger=event_logger,
    )
    order_manager = OrderManager(
        paper_exchange=paper_exchange,
        logger=logger,
        risk_manager=risk_manager,
        event_logger=event_logger,
    )
    return logger, event_logger, market_data, order_manager, position_manager, risk_manager


def _create_market_data_provider(config: BotConfig, logger: logging.Logger) -> MarketDataProvider:
    """Create the configured market data provider."""

    if config.market_data_mode == "mock":
        logger.info("Using mock market data mode")
        return MockMarketDataGenerator(logger=logger, seed=config.random_seed)

    if config.market_data_mode == "polymarket":
        logger.info(
            "Using public Polymarket market data mode | gamma_base_url=%s clob_base_url=%s query=%s",
            config.gamma_base_url,
            config.clob_base_url,
            config.btc_market_query,
        )
        discovery_client = MarketDiscoveryClient(
            logger=logger,
            gamma_base_url=config.gamma_base_url,
            btc_market_query=config.btc_market_query,
            timeout_seconds=config.http_timeout_seconds,
        )
        orderbook_client = PolymarketOrderBookClient(
            logger=logger,
            clob_base_url=config.clob_base_url,
            timeout_seconds=config.http_timeout_seconds,
        )
        return PolymarketMarketDataClient(
            discovery_client=discovery_client,
            orderbook_client=orderbook_client,
            market_refresh_seconds=config.market_refresh_seconds,
            logger=logger,
        )

    raise ValueError(f"unsupported market_data_mode={config.market_data_mode!r}")


def _log_market_selected(
    event_logger: StructuredEventLogger,
    logger: logging.Logger,
    snapshot: MarketSnapshot,
    config: BotConfig,
    iteration: int,
) -> None:
    """Log metadata for the current market selection."""

    with log_context(
        event="market_selected",
        iteration=iteration,
        market_slug=snapshot.market_slug,
        condition_id=snapshot.condition_id,
    ):
        logger.info(
            "Selected market | question=%s slug=%s condition_id=%s yes_token_id=%s "
            "no_token_id=%s expiry_time=%s mode=%s",
            snapshot.market_question,
            snapshot.market_slug,
            snapshot.condition_id,
            snapshot.yes_token_id,
            snapshot.no_token_id,
            snapshot.expiry_time,
            config.market_data_mode,
        )
    event_logger.emit(
        "market_selected",
        iteration=iteration,
        market_slug=snapshot.market_slug,
        condition_id=snapshot.condition_id,
        question=snapshot.market_question,
        yes_token_id=snapshot.yes_token_id,
        no_token_id=snapshot.no_token_id,
        expiry_time=snapshot.expiry_time,
        market_data_mode=config.market_data_mode,
    )


def _log_position_event(
    event_logger: StructuredEventLogger,
    iteration: int | None,
    snapshot: MarketSnapshot,
    position_manager: PositionManager,
) -> None:
    """Log the current market-specific position and portfolio equity."""

    market_position = position_manager.get_market_position(snapshot.condition_id)
    total_equity = position_manager.equity
    event_logger.emit(
        "position",
        iteration=iteration,
        market_slug=snapshot.market_slug,
        condition_id=snapshot.condition_id,
        cash=position_manager.cash,
        equity=total_equity,
        total_equity=total_equity,
        current_market_equity=market_position.equity,
        realized_pnl=market_position.realized_pnl,
        unrealized_pnl=market_position.unrealized_pnl,
        total_realized_pnl=position_manager.realized_pnl,
        total_unrealized_pnl=position_manager.unrealized_pnl,
        yes_position=market_position.yes_position,
        no_position=market_position.no_position,
        yes_avg_cost=market_position.yes_avg_cost,
        no_avg_cost=market_position.no_avg_cost,
        inventory_imbalance=market_position.inventory_imbalance,
    )


def _build_session_summary(
    *,
    event_logger: StructuredEventLogger,
    config: BotConfig,
    position_manager: PositionManager,
    order_manager: OrderManager,
    risk_manager: RiskManager,
    ended_at: datetime,
) -> dict[str, object]:
    """Build the final session summary persisted to summary.json."""

    ending_equity = position_manager.equity
    summary = event_logger.base_summary(ended_at=ended_at)
    markets_seen = set(summary.get("markets_seen", []))
    markets_seen.update(position.market_slug for position in position_manager.positions_by_condition_id.values())
    markets_traded = set(summary.get("markets_traded", []))
    markets_traded.update(fill.market_slug for fill in position_manager.fills_history)

    summary.update(
        {
            "starting_cash": config.initial_cash,
            "ending_cash": position_manager.cash,
            "ending_equity": ending_equity,
            "realized_pnl": position_manager.realized_pnl,
            "unrealized_pnl": position_manager.unrealized_pnl,
            "max_equity": summary["max_equity"] if summary["max_equity"] is not None else ending_equity,
            "min_equity": summary["min_equity"] if summary["min_equity"] is not None else ending_equity,
            "total_orders": len(order_manager.get_all_orders()),
            "total_fills": len(position_manager.fills_history),
            "total_risk_rejections": risk_manager.rejected_orders,
            "open_orders": len(order_manager.get_open_orders()),
            "markets_seen": sorted(markets_seen),
            "markets_traded": sorted(markets_traded),
            "final_positions": {
                condition_id: {
                    "market_slug": position.market_slug,
                    "yes_token_id": position.yes_token_id,
                    "no_token_id": position.no_token_id,
                    "expiry_time": position.expiry_time,
                    "yes_position": position.yes_position,
                    "no_position": position.no_position,
                    "yes_avg_cost": position.yes_avg_cost,
                    "no_avg_cost": position.no_avg_cost,
                    "realized_pnl": position.realized_pnl,
                    "unrealized_pnl": position.unrealized_pnl,
                    "current_market_equity": position.equity,
                    "unresolved_inventory": position.unresolved_inventory,
                    "unresolved_reason": position.unresolved_reason,
                }
                for condition_id, position in position_manager.positions_by_condition_id.items()
            },
        }
    )
    return summary


def run_bot(config: BotConfig) -> None:
    """Run the paper-trading loop."""

    logger, event_logger, market_data, order_manager, position_manager, risk_manager = build_components(config)
    event_logger.emit(
        "session_started",
        market_data_mode=config.market_data_mode,
        starting_cash=config.initial_cash,
        log_dir=str(event_logger.paths.session_dir),
    )
    with log_context(event="session_started"):
        logger.info(
            "Starting Polymarket BTCUSDT 15m paper bot | session_id=%s log_dir=%s config=%s",
            event_logger.session_id,
            event_logger.paths.session_dir,
            config,
        )

    iteration = 0
    current_condition_id: str | None = None
    current_market_slug: str | None = None
    last_snapshot: MarketSnapshot | None = None
    try:
        while config.max_iterations is None or iteration < config.max_iterations:
            iteration += 1
            try:
                snapshot = market_data.get_snapshot()
            except MarketDataError as exc:
                with log_context(event="market_data_unavailable", iteration=iteration):
                    logger.warning("Market data unavailable; skipping iteration %d | %s", iteration, exc)
                event_logger.emit(
                    "market_data_unavailable",
                    level="WARNING",
                    iteration=iteration,
                    reason=str(exc),
                    market_data_mode=config.market_data_mode,
                )
                snapshot = None

            if snapshot is None:
                with log_context(event="market_data_unavailable", iteration=iteration):
                    logger.warning("Market snapshot unavailable; skipping iteration %d", iteration)
                event_logger.emit(
                    "market_data_unavailable",
                    level="WARNING",
                    iteration=iteration,
                    reason="market snapshot unavailable",
                    market_data_mode=config.market_data_mode,
                )
                _handle_stale_market_lifecycle(
                    last_snapshot=last_snapshot,
                    order_manager=order_manager,
                    position_manager=position_manager,
                    logger=logger,
                    event_logger=event_logger,
                    iteration=iteration,
                    block_new_seconds=config.expiry_block_new_seconds,
                    cancel_seconds=config.expiry_cancel_seconds,
                )
                if config.max_iterations is None or iteration < config.max_iterations:
                    time.sleep(config.market_update_interval_seconds)
                continue

            last_snapshot = snapshot
            if current_condition_id is None:
                _log_market_selected(event_logger, logger, snapshot, config, iteration)
            elif snapshot.condition_id != current_condition_id:
                cancelled = cancel_open_orders_not_for_condition(
                    order_manager,
                    snapshot.condition_id,
                    iteration=iteration,
                    cancel_reason="market_rollover",
                )
                with log_context(
                    event="market_rollover",
                    iteration=iteration,
                    market_slug=snapshot.market_slug,
                    condition_id=snapshot.condition_id,
                ):
                    logger.info(
                        "Market changed | old_condition_id=%s new_condition_id=%s new_market_slug=%s "
                        "cancelled_old_market_orders=%d",
                        current_condition_id,
                        snapshot.condition_id,
                        snapshot.market_slug,
                        cancelled,
                    )
                event_logger.emit(
                    "market_rollover",
                    iteration=iteration,
                    market_slug=snapshot.market_slug,
                    condition_id=snapshot.condition_id,
                    old_condition_id=current_condition_id,
                    old_market_slug=current_market_slug,
                    new_condition_id=snapshot.condition_id,
                    new_market_slug=snapshot.market_slug,
                    cancelled_old_market_orders=cancelled,
                )
                _log_market_selected(event_logger, logger, snapshot, config, iteration)
            current_condition_id = snapshot.condition_id
            current_market_slug = snapshot.market_slug
            with log_context(
                event="position",
                iteration=iteration,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
            ):
                position_manager.ensure_market(snapshot)

            seconds_to_expiry = _seconds_to_expiry(snapshot)
            with log_context(
                event="snapshot",
                iteration=iteration,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
            ):
                logger.info(
                    "Snapshot | YES %.3f/%.3f | NO %.3f/%.3f",
                    snapshot.yes.best_bid,
                    snapshot.yes.best_ask,
                    snapshot.no.best_bid,
                    snapshot.no.best_ask,
                )
            event_logger.emit(
                "snapshot",
                iteration=iteration,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
                **snapshot_event_fields(snapshot, seconds_to_expiry),
            )

            fills = order_manager.update_orders_from_market(snapshot, iteration=iteration)
            position_manager.apply_fills(fills)
            position_manager.update_unrealized_pnl(snapshot)
            _log_position_event(event_logger, iteration, snapshot, position_manager)

            if _forced_exit_required(snapshot, config.expiry_cancel_seconds):
                cancel_all_open_orders(
                    order_manager,
                    iteration=iteration,
                    cancel_reason="forced_exit",
                )
                _attempt_forced_exit(
                    snapshot=snapshot,
                    order_manager=order_manager,
                    position_manager=position_manager,
                    logger=logger,
                    event_logger=event_logger,
                    iteration=iteration,
                    max_exit_order_size=config.max_order_size,
                )
            else:
                cancel_all_open_orders(
                    order_manager,
                    iteration=iteration,
                    cancel_reason="stale_quote_refresh",
                )
                if not _new_positions_blocked(snapshot, config.expiry_block_new_seconds):
                    _place_test_orders(
                        snapshot=snapshot,
                        order_manager=order_manager,
                        position_manager=position_manager,
                        config=config,
                        event_logger=event_logger,
                        iteration=iteration,
                    )
                else:
                    with log_context(
                        event="forced_exit",
                        iteration=iteration,
                        market_slug=snapshot.market_slug,
                        condition_id=snapshot.condition_id,
                    ):
                        logger.info(
                            "Forced-exit guard active | market_slug=%s condition_id=%s "
                            "seconds_to_expiry=%.1f new_positions_blocked=True",
                            snapshot.market_slug,
                            snapshot.condition_id,
                            seconds_to_expiry,
                        )
                    event_logger.emit(
                        "forced_exit",
                        iteration=iteration,
                        market_slug=snapshot.market_slug,
                        condition_id=snapshot.condition_id,
                        seconds_to_expiry=seconds_to_expiry,
                        action="block_new_positions",
                    )

            _log_status(
                logger,
                iteration,
                snapshot,
                order_manager.get_open_orders(),
                position_manager,
                risk_manager,
            )

            if config.max_iterations is None or iteration < config.max_iterations:
                time.sleep(config.market_update_interval_seconds)
    except KeyboardInterrupt:
        with log_context(event="session_finished"):
            logger.info("Paper bot stopped by user")
    except Exception:
        with log_context(event="error"):
            logger.exception("Fatal error in paper bot loop")
        event_logger.emit("error", level="ERROR", error="fatal error in paper bot loop")
        raise
    finally:
        cancelled_on_shutdown = cancel_all_open_orders(
            order_manager,
            iteration=iteration if iteration > 0 else None,
            cancel_reason="session_shutdown",
        )
        if cancelled_on_shutdown:
            with log_context(event="session_finished", iteration=iteration if iteration > 0 else None):
                logger.info("Cancelled open paper orders on shutdown | count=%d", cancelled_on_shutdown)
        ended_at = datetime.now(timezone.utc)
        summary = _build_session_summary(
            event_logger=event_logger,
            config=config,
            position_manager=position_manager,
            order_manager=order_manager,
            risk_manager=risk_manager,
            ended_at=ended_at,
        )
        event_logger.emit(
            "session_finished",
            ending_cash=summary["ending_cash"],
            ending_equity=summary["ending_equity"],
            total_orders=summary["total_orders"],
            total_fills=summary["total_fills"],
            total_risk_rejections=summary["total_risk_rejections"],
            open_orders=summary["open_orders"],
        )
        event_logger.write_summary(summary)
        with log_context(event="session_finished"):
            logger.info("Final positions | %s", position_manager.summary())
            logger.info(
                "All orders=%d fills=%d rejected_orders=%d summary=%s",
                len(order_manager.get_all_orders()),
                len(position_manager.fills_history),
                risk_manager.rejected_orders,
                event_logger.paths.summary_json_path,
            )


def cancel_all_open_orders(
    order_manager: OrderManager,
    *,
    iteration: int | None = None,
    cancel_reason: str = "stale_quote_refresh",
) -> int:
    """Cancel all currently open or partially filled orders."""

    cancelled_count = 0
    for order in order_manager.get_open_orders():
        if order_manager.cancel_order(order.order_id, cancel_reason=cancel_reason, iteration=iteration):
            cancelled_count += 1
    return cancelled_count


def cancel_open_orders_for_condition(
    order_manager: OrderManager,
    condition_id: str,
    *,
    iteration: int | None = None,
    cancel_reason: str = "stale_market_lifecycle",
) -> int:
    """Cancel active orders that belong to the given condition."""

    cancelled_count = 0
    for order in order_manager.get_open_orders_for_condition(condition_id):
        if order_manager.cancel_order(order.order_id, cancel_reason=cancel_reason, iteration=iteration):
            cancelled_count += 1
    return cancelled_count


def cancel_open_orders_not_for_condition(
    order_manager: OrderManager,
    condition_id: str,
    *,
    iteration: int | None = None,
    cancel_reason: str = "market_rollover",
) -> int:
    """Cancel active orders that do not belong to the given condition."""

    cancelled_count = 0
    for order in order_manager.get_open_orders():
        if order.condition_id == condition_id:
            continue
        if order_manager.cancel_order(order.order_id, cancel_reason=cancel_reason, iteration=iteration):
            cancelled_count += 1
    return cancelled_count


def _log_status(
    logger: logging.Logger,
    iteration: int,
    snapshot: MarketSnapshot,
    open_orders: Iterable[Order],
    position_manager: PositionManager,
    risk_manager: RiskManager,
) -> None:
    open_orders_list = list(open_orders)
    with log_context(
        event="position",
        iteration=iteration,
        market_slug=snapshot.market_slug,
        condition_id=snapshot.condition_id,
    ):
        logger.info(
            "Iteration %d | current_market_slug=%s condition_id=%s "
            "yes_token_id=%s no_token_id=%s YES %.3f/%.3f | NO %.3f/%.3f",
            iteration,
            snapshot.market_slug,
            snapshot.condition_id,
            snapshot.yes_token_id,
            snapshot.no_token_id,
            snapshot.yes.best_bid,
            snapshot.yes.best_ask,
            snapshot.no.best_bid,
            snapshot.no.best_ask,
        )
        logger.info("Portfolio | %s", position_manager.summary())
        logger.info("Current market position | %s", position_manager.market_summary(snapshot.condition_id))
        logger.info(
            "Orders | open_orders=%d rejected_orders=%d",
            len(open_orders_list),
            risk_manager.rejected_orders,
        )
        for order in open_orders_list:
            logger.info(
                "Open order | order=%s condition_id=%s market_slug=%s %s %s token_id=%s "
                "size=%.4f filled=%.4f remaining=%.4f price=%.3f status=%s",
                order.order_id,
                order.condition_id,
                order.market_slug,
                order.side,
                order.outcome,
                order.token_id,
                order.size,
                order.filled_size,
                order.remaining_size,
                order.price,
                order.status,
            )


def _place_test_orders(
    snapshot: MarketSnapshot,
    order_manager: OrderManager,
    position_manager: PositionManager,
    config: BotConfig,
    event_logger: StructuredEventLogger | None = None,
    iteration: int | None = None,
) -> None:
    """Place simple passive test quotes around YES and NO books.

    BUY quotes are normally eligible subject to risk checks. If current-market
    inventory is too skewed, the strategy stops buying the overloaded outcome
    and focuses exits on that side before the risk manager has to reject orders.
    SELL quotes are only proposed when spot inventory can cover the full test
    order size, which keeps normal logs free from predictable naked-short
    rejections.
    """

    size = config.default_order_size
    market_position = position_manager.ensure_market(snapshot)
    fair_yes = snapshot.yes.midpoint
    fair_no = snapshot.no.midpoint
    base_spread = ((snapshot.yes.best_ask - snapshot.yes.best_bid) + (snapshot.no.best_ask - snapshot.no.best_bid)) / 2.0
    inventory_skew = _inventory_skew_ratio(
        market_position.inventory_imbalance,
        config.max_inventory_imbalance,
    )
    skew_threshold = min(max(config.inventory_skew_disable_ratio, 0.0), 1.0)
    yes_heavy = inventory_skew >= skew_threshold
    no_heavy = inventory_skew <= -skew_threshold
    proposals: list[tuple[Token, Side, float, str]] = []
    skipped_quotes: list[dict[str, object]] = []

    def skip_quote(outcome: Token, side: Side, reason: str, **fields: object) -> None:
        skipped_quotes.append(
            {
                "outcome": outcome,
                "side": side,
                "reason": reason,
                "inventory_imbalance": market_position.inventory_imbalance,
                "inventory_skew": inventory_skew,
                **fields,
            }
        )

    if yes_heavy:
        skip_quote(
            Token.YES,
            Side.BUY,
            "inventory skew blocks BUY YES",
        )
    else:
        proposals.append(
            (
                Token.YES,
                Side.BUY,
                snapshot.yes.best_bid - config.strategy_quote_offset,
                "test_quote",
            )
        )

    if no_heavy:
        skip_quote(
            Token.NO,
            Side.BUY,
            "inventory skew blocks BUY NO",
        )
    else:
        proposals.append(
            (
                Token.NO,
                Side.BUY,
                snapshot.no.best_bid - config.strategy_quote_offset,
                "test_quote",
            )
        )

    if market_position.yes_position < size:
        skip_quote(
            Token.YES,
            Side.SELL,
            "insufficient YES inventory for spot SELL",
            current_position=market_position.yes_position,
            required_size=size,
        )
    elif no_heavy:
        skip_quote(
            Token.YES,
            Side.SELL,
            "inventory skew avoids SELL YES while NO-heavy",
            current_position=market_position.yes_position,
        )
    else:
        raw_price = (
            snapshot.yes.best_ask - config.strategy_quote_offset
            if yes_heavy
            else snapshot.yes.best_ask + config.strategy_quote_offset
        )
        proposals.append(
            (
                Token.YES,
                Side.SELL,
                raw_price,
                "inventory_rebalance" if yes_heavy else "test_quote",
            )
        )

    if market_position.no_position < size:
        skip_quote(
            Token.NO,
            Side.SELL,
            "insufficient NO inventory for spot SELL",
            current_position=market_position.no_position,
            required_size=size,
        )
    elif yes_heavy:
        skip_quote(
            Token.NO,
            Side.SELL,
            "inventory skew avoids SELL NO while YES-heavy",
            current_position=market_position.no_position,
        )
    else:
        raw_price = (
            snapshot.no.best_ask - config.strategy_quote_offset
            if no_heavy
            else snapshot.no.best_ask + config.strategy_quote_offset
        )
        proposals.append(
            (
                Token.NO,
                Side.SELL,
                raw_price,
                "inventory_rebalance" if no_heavy else "test_quote",
            )
        )

    proposed_quotes = [
        {
            "outcome": outcome,
            "side": side,
            "raw_price": raw_price,
            "price": min(max(raw_price, config.min_price), config.max_price),
            "size": size,
            "reason": reason,
        }
        for outcome, side, raw_price, reason in proposals
    ]
    if event_logger is not None:
        event_logger.emit(
            "strategy_decision",
            iteration=iteration,
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
            fair_yes=fair_yes,
            fair_no=fair_no,
            base_spread=base_spread,
            quote_offset=config.strategy_quote_offset,
            inventory_imbalance=market_position.inventory_imbalance,
            inventory_skew=inventory_skew,
            inventory_skew_disable_ratio=skew_threshold,
            proposed_quotes=proposed_quotes,
            skipped_quotes=skipped_quotes,
            skip_reasons=[str(item["reason"]) for item in skipped_quotes],
        )

    for outcome, side, raw_price, reason in proposals:
        price = min(max(raw_price, config.min_price), config.max_price)
        order_manager.place_limit_order(
            market_snapshot=snapshot,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            iteration=iteration,
            reason=reason,
        )


def _inventory_skew_ratio(inventory_imbalance: float, max_inventory_imbalance: float) -> float:
    """Return inventory imbalance normalized to the configured hard limit."""

    if max_inventory_imbalance <= 0:
        return 0.0
    return inventory_imbalance / max_inventory_imbalance


def _handle_stale_market_lifecycle(
    *,
    last_snapshot: MarketSnapshot | None,
    order_manager: OrderManager,
    position_manager: PositionManager,
    logger: logging.Logger,
    event_logger: StructuredEventLogger | None = None,
    iteration: int | None = None,
    block_new_seconds: float = FORCED_EXIT_BLOCK_NEW_SECONDS,
    cancel_seconds: float = FORCED_EXIT_CANCEL_SECONDS,
) -> None:
    """Apply expiry safeguards when fresh market data is unavailable."""

    if last_snapshot is None:
        return

    seconds_to_expiry = _seconds_to_expiry(last_snapshot)
    if seconds_to_expiry is None:
        return

    position = position_manager.ensure_market(last_snapshot)
    if seconds_to_expiry <= block_new_seconds:
        with log_context(
            event="forced_exit",
            iteration=iteration,
            market_slug=last_snapshot.market_slug,
            condition_id=last_snapshot.condition_id,
        ):
            logger.info(
                "Stale market block-new guard active | market_slug=%s condition_id=%s "
                "seconds_to_expiry=%.1f block_new_seconds=%.1f",
                last_snapshot.market_slug,
                last_snapshot.condition_id,
                seconds_to_expiry,
                block_new_seconds,
            )
        if event_logger is not None:
            event_logger.emit(
                "forced_exit",
                iteration=iteration,
                market_slug=last_snapshot.market_slug,
                condition_id=last_snapshot.condition_id,
                seconds_to_expiry=seconds_to_expiry,
                action="stale_market_block_new_positions",
                block_new_seconds=block_new_seconds,
            )

    if seconds_to_expiry <= cancel_seconds:
        cancelled = cancel_open_orders_for_condition(
            order_manager,
            last_snapshot.condition_id,
            iteration=iteration,
            cancel_reason="stale_market_expiry_guard",
        )
        with log_context(
            event="forced_exit",
            iteration=iteration,
            market_slug=last_snapshot.market_slug,
            condition_id=last_snapshot.condition_id,
        ):
            logger.warning(
                "Stale market expiry guard active | market_slug=%s condition_id=%s "
                "seconds_to_expiry=%.1f cancel_seconds=%.1f cancelled_orders=%d",
                last_snapshot.market_slug,
                last_snapshot.condition_id,
                seconds_to_expiry,
                cancel_seconds,
                cancelled,
            )
        if event_logger is not None:
            event_logger.emit(
                "forced_exit",
                level="WARNING",
                iteration=iteration,
                market_slug=last_snapshot.market_slug,
                condition_id=last_snapshot.condition_id,
                seconds_to_expiry=seconds_to_expiry,
                action="stale_market_expiry_guard",
                cancel_seconds=cancel_seconds,
                cancelled_orders=cancelled,
            )

    has_inventory = position.yes_position > 1e-9 or position.no_position > 1e-9
    if seconds_to_expiry <= 0 and has_inventory and not position.unresolved_inventory:
        position_manager.archive_unresolved_inventory(
            last_snapshot.condition_id,
            "market data unavailable through expiry; inventory could not be settled or exited",
        )
        if event_logger is not None:
            event_logger.emit(
                "forced_exit",
                level="WARNING",
                iteration=iteration,
                market_slug=last_snapshot.market_slug,
                condition_id=last_snapshot.condition_id,
                seconds_to_expiry=seconds_to_expiry,
                action="archive_unresolved_inventory_stale_data",
                yes_position=position.yes_position,
                no_position=position.no_position,
                reason=position.unresolved_reason,
            )


def _attempt_forced_exit(
    snapshot: MarketSnapshot,
    order_manager: OrderManager,
    position_manager: PositionManager,
    logger: logging.Logger,
    event_logger: StructuredEventLogger | None = None,
    iteration: int | None = None,
    max_exit_order_size: float = DEFAULT_CONFIG.max_order_size,
) -> None:
    """Try to close current-market inventory using paper-only crossing SELL orders."""

    market_position = position_manager.ensure_market(snapshot)
    seconds_to_expiry = _seconds_to_expiry(snapshot) or 0.0
    with log_context(
        event="forced_exit",
        iteration=iteration,
        market_slug=snapshot.market_slug,
        condition_id=snapshot.condition_id,
    ):
        logger.info(
            "Forced-exit mode active | market_slug=%s condition_id=%s seconds_to_expiry=%.1f",
            snapshot.market_slug,
            snapshot.condition_id,
            seconds_to_expiry,
        )
    if event_logger is not None:
        event_logger.emit(
            "forced_exit",
            iteration=iteration,
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
            seconds_to_expiry=seconds_to_expiry,
            action="attempt_close_inventory",
            yes_position=market_position.yes_position,
            no_position=market_position.no_position,
        )

    exit_specs = [
        (Token.YES, market_position.yes_position, snapshot.yes.best_bid),
        (Token.NO, market_position.no_position, snapshot.no.best_bid),
    ]
    for outcome, position_size, exit_price in exit_specs:
        if position_size <= 1e-9:
            continue
        chunks = _split_exit_chunks(position_size, max_exit_order_size)
        with log_context(
            event="forced_exit",
            iteration=iteration,
            market_slug=snapshot.market_slug,
            condition_id=snapshot.condition_id,
        ):
            logger.info(
                "Forced-exit chunked orders | outcome=%s total_position=%.8f "
                "max_chunk_size=%.8f number_of_chunks=%d",
                outcome,
                position_size,
                max_exit_order_size,
                len(chunks),
            )
        if event_logger is not None:
            event_logger.emit(
                "forced_exit",
                iteration=iteration,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
                action="chunk_exit_orders",
                outcome=outcome,
                total_position=position_size,
                chunk_size=max_exit_order_size,
                number_of_chunks=len(chunks),
                chunks=chunks,
            )
        for chunk_size in chunks:
            order_manager.place_limit_order(
                market_snapshot=snapshot,
                outcome=outcome,
                side=Side.SELL,
                price=exit_price,
                size=chunk_size,
                iteration=iteration,
                reason="forced_exit_chunk",
            )

    fills = order_manager.update_orders_from_market(snapshot, iteration=iteration)
    position_manager.apply_fills(fills)
    position_manager.update_unrealized_pnl(snapshot)
    if event_logger is not None:
        _log_position_event(event_logger, iteration, snapshot, position_manager)
    cancel_all_open_orders(order_manager, iteration=iteration, cancel_reason="forced_exit_cleanup")

    market_position = position_manager.get_market_position(snapshot.condition_id)
    if market_position.yes_position > 1e-9 or market_position.no_position > 1e-9:
        position_manager.archive_unresolved_inventory(
            snapshot.condition_id,
            "forced exit could not close full inventory before expiry",
        )
        if event_logger is not None:
            event_logger.emit(
                "forced_exit",
                level="WARNING",
                iteration=iteration,
                market_slug=snapshot.market_slug,
                condition_id=snapshot.condition_id,
                action="archive_unresolved_inventory",
                yes_position=market_position.yes_position,
                no_position=market_position.no_position,
                reason=market_position.unresolved_reason,
            )


def _split_exit_chunks(position_size: float, max_exit_order_size: float) -> list[float]:
    """Split one exit position into order sizes accepted by risk limits."""

    if position_size <= 1e-9:
        return []
    if max_exit_order_size <= 0:
        raise ValueError(f"max_exit_order_size must be positive, got {max_exit_order_size}")

    chunks: list[float] = []
    remaining = position_size
    while remaining > 1e-9:
        chunk_size = min(max_exit_order_size, remaining)
        chunks.append(round(chunk_size, 8))
        remaining = max(remaining - chunk_size, 0.0)
    return chunks


def _seconds_to_expiry(snapshot: MarketSnapshot) -> float | None:
    """Return seconds to expiry for snapshots that expose an expiry time."""

    if snapshot.expiry_time is None:
        return None
    expiry_time = snapshot.expiry_time
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)
    return (expiry_time - datetime.now(timezone.utc)).total_seconds()


def _forced_exit_required(
    snapshot: MarketSnapshot,
    cancel_seconds: float = FORCED_EXIT_CANCEL_SECONDS,
) -> bool:
    """Return whether open orders should be cancelled and inventory exited."""

    seconds_to_expiry = _seconds_to_expiry(snapshot)
    return seconds_to_expiry is not None and seconds_to_expiry <= cancel_seconds


def _new_positions_blocked(
    snapshot: MarketSnapshot,
    block_new_seconds: float = FORCED_EXIT_BLOCK_NEW_SECONDS,
) -> bool:
    """Return whether opening new positions is blocked by the expiry guard."""

    seconds_to_expiry = _seconds_to_expiry(snapshot)
    return seconds_to_expiry is not None and seconds_to_expiry <= block_new_seconds


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description="Safe Polymarket paper-trading MVP bot.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of loop iterations to run. Use 0 for an infinite loop.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_CONFIG.market_update_interval_seconds,
        help="Seconds to sleep between market snapshots.",
    )
    parser.add_argument(
        "--order-size",
        type=float,
        default=DEFAULT_CONFIG.default_order_size,
        help="Paper order size for each test quote.",
    )
    parser.add_argument(
        "--inventory-skew-disable-ratio",
        type=float,
        default=DEFAULT_CONFIG.inventory_skew_disable_ratio,
        help="Disable overloaded-side BUY quotes when abs(inventory skew) reaches this ratio.",
    )
    parser.add_argument(
        "--expiry-block-new-seconds",
        type=float,
        default=DEFAULT_CONFIG.expiry_block_new_seconds,
        help="Seconds before expiry when opening new positions is blocked.",
    )
    parser.add_argument(
        "--expiry-cancel-seconds",
        type=float,
        default=DEFAULT_CONFIG.expiry_cancel_seconds,
        help="Seconds before expiry when open orders are cancelled and forced exit is attempted.",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=DEFAULT_CONFIG.initial_cash,
        help="Starting paper cash balance.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_CONFIG.random_seed,
        help="Random seed for mock market data. Use any integer for reproducible runs.",
    )
    parser.add_argument(
        "--market-data-mode",
        choices=("mock", "polymarket"),
        default=DEFAULT_CONFIG.market_data_mode,
        help="Market data source. Trading remains paper-only in both modes.",
    )
    parser.add_argument(
        "--gamma-base-url",
        default=DEFAULT_CONFIG.gamma_base_url,
        help="Public Polymarket Gamma API base URL.",
    )
    parser.add_argument(
        "--clob-base-url",
        default=DEFAULT_CONFIG.clob_base_url,
        help="Public Polymarket CLOB API base URL.",
    )
    parser.add_argument(
        "--btc-market-query",
        default=DEFAULT_CONFIG.btc_market_query,
        help="Text used to select an active BTC 15-minute market by question/slug.",
    )
    parser.add_argument(
        "--market-refresh-seconds",
        type=float,
        default=DEFAULT_CONFIG.market_refresh_seconds,
        help="Seconds between Gamma market rediscovery attempts in Polymarket mode.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI wrapper for the paper bot."""

    args = parse_args()
    config = BotConfig(
        initial_cash=args.initial_cash,
        market_update_interval_seconds=args.interval,
        default_order_size=args.order_size,
        inventory_skew_disable_ratio=args.inventory_skew_disable_ratio,
        expiry_block_new_seconds=args.expiry_block_new_seconds,
        expiry_cancel_seconds=args.expiry_cancel_seconds,
        max_iterations=None if args.iterations == 0 else args.iterations,
        random_seed=args.seed,
        market_data_mode=args.market_data_mode,
        gamma_base_url=args.gamma_base_url,
        clob_base_url=args.clob_base_url,
        btc_market_query=args.btc_market_query,
        market_refresh_seconds=args.market_refresh_seconds,
    )
    run_bot(config)


if __name__ == "__main__":
    main()
