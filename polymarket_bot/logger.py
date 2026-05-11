"""Human-readable and structured logging for paper-trading sessions."""

from __future__ import annotations

import csv
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from .models import Fill, MarketSnapshot, Order

SESSION_ID_FORMAT = "%Y%m%d_%H%M%S"

_session_id_context: ContextVar[str] = ContextVar("session_id", default="-")
_event_context: ContextVar[str] = ContextVar("log_event", default="-")
_iteration_context: ContextVar[str] = ContextVar("iteration", default="-")
_market_slug_context: ContextVar[str] = ContextVar("market_slug", default="-")
_condition_id_context: ContextVar[str] = ContextVar("condition_id", default="-")


SNAPSHOT_CSV_FIELDS = [
    "ts",
    "session_id",
    "iteration",
    "market_slug",
    "condition_id",
    "yes_bid",
    "yes_ask",
    "yes_bid_size",
    "yes_ask_size",
    "no_bid",
    "no_ask",
    "no_bid_size",
    "no_ask_size",
    "yes_mid",
    "no_mid",
    "yes_ask_plus_no_ask",
    "yes_bid_plus_no_bid",
    "time_to_expiry",
]
ORDER_CSV_FIELDS = [
    "ts",
    "session_id",
    "event",
    "iteration",
    "market_slug",
    "condition_id",
    "order_id",
    "token_id",
    "outcome",
    "side",
    "price",
    "size",
    "remaining_size",
    "reason",
    "cancel_reason",
]
FILL_CSV_FIELDS = [
    "ts",
    "session_id",
    "iteration",
    "market_slug",
    "condition_id",
    "order_id",
    "token_id",
    "outcome",
    "side",
    "price",
    "size",
    "notional",
    "time_to_expiry",
]
POSITION_CSV_FIELDS = [
    "ts",
    "session_id",
    "iteration",
    "market_slug",
    "condition_id",
    "cash",
    "total_equity",
    "current_market_equity",
    "realized_pnl",
    "unrealized_pnl",
    "yes_position",
    "no_position",
    "yes_avg_cost",
    "no_avg_cost",
    "inventory_imbalance",
]
RISK_CSV_FIELDS = [
    "ts",
    "session_id",
    "iteration",
    "market_slug",
    "condition_id",
    "reason",
    "outcome",
    "side",
    "price",
    "size",
    "current_position",
    "current_cash",
    "inventory_imbalance",
    "max_inventory_imbalance",
    "max_position_per_token",
]


@dataclass(frozen=True, slots=True)
class SessionLogPaths:
    """Filesystem paths for one bot run."""

    session_id: str
    session_dir: Path
    bot_log_path: Path
    events_jsonl_path: Path
    summary_json_path: Path
    config_json_path: Path
    markets_dir: Path
    csv_dir: Path

    @property
    def snapshots_csv_path(self) -> Path:
        return self.csv_dir / "snapshots.csv"

    @property
    def orders_csv_path(self) -> Path:
        return self.csv_dir / "orders.csv"

    @property
    def fills_csv_path(self) -> Path:
        return self.csv_dir / "fills.csv"

    @property
    def positions_csv_path(self) -> Path:
        return self.csv_dir / "positions.csv"

    @property
    def risk_rejections_csv_path(self) -> Path:
        return self.csv_dir / "risk_rejections.csv"


def generate_session_id(market_data_mode: str, now: datetime | None = None) -> str:
    """Generate a run identifier for a paper-trading session."""

    current_time = now or datetime.now()
    return f"{current_time.strftime(SESSION_ID_FORMAT)}_{market_data_mode}_paper"


def create_session_log_paths(session_id: str, logs_root: str | Path = "logs") -> SessionLogPaths:
    """Create the per-session log directory structure."""

    session_dir = Path(logs_root) / "sessions" / session_id
    markets_dir = session_dir / "markets"
    csv_dir = session_dir / "csv"
    markets_dir.mkdir(parents=True, exist_ok=True)
    csv_dir.mkdir(parents=True, exist_ok=True)
    return SessionLogPaths(
        session_id=session_id,
        session_dir=session_dir,
        bot_log_path=session_dir / "bot.log",
        events_jsonl_path=session_dir / "events.jsonl",
        summary_json_path=session_dir / "summary.json",
        config_json_path=session_dir / "config.json",
        markets_dir=markets_dir,
        csv_dir=csv_dir,
    )


def set_session_context(session_id: str) -> None:
    """Attach a session id to normal logging emitted in the current context."""

    _session_id_context.set(session_id)


@contextmanager
def log_context(
    *,
    event: str | None = None,
    iteration: int | str | None = None,
    market_slug: str | None = None,
    condition_id: str | None = None,
) -> Iterator[None]:
    """Temporarily add structured context to human-readable log lines."""

    tokens = []
    if event is not None:
        tokens.append((_event_context, _event_context.set(event)))
    if iteration is not None:
        tokens.append((_iteration_context, _iteration_context.set(str(iteration))))
    if market_slug is not None:
        tokens.append((_market_slug_context, _market_slug_context.set(market_slug)))
    if condition_id is not None:
        tokens.append((_condition_id_context, _condition_id_context.set(condition_id)))

    try:
        yield
    finally:
        for context_var, token in reversed(tokens):
            context_var.reset(token)


class _ContextFilter(logging.Filter):
    """Inject session and market context into standard log records."""

    def __init__(self, default_session_id: str = "-") -> None:
        super().__init__()
        self.default_session_id = default_session_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = _session_id_context.get() or self.default_session_id
        record.log_event = _event_context.get()
        record.iteration = _iteration_context.get()
        record.market_slug = _market_slug_context.get()
        record.condition_id = _condition_id_context.get()
        return True


def setup_logger(
    name: str = "polymarket_bot",
    log_file: str | Path = "logs/bot.log",
    session_id: str = "-",
) -> logging.Logger:
    """Configure the project logger while preserving normal logger.info calls."""

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | session=%(session_id)s | "
        "event=%(log_event)s | iteration=%(iteration)s | market=%(market_slug)s | "
        "condition=%(condition_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = _ContextFilter(default_session_id=session_id)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    set_session_context(session_id)
    return logger


class StructuredEventLogger:
    """Write JSONL events, CSV exports, config snapshots, and session summary."""

    def __init__(
        self,
        session_id: str,
        paths: SessionLogPaths,
        market_data_mode: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        self.session_id = session_id
        self.paths = paths
        self.market_data_mode = market_data_mode
        self.started_at = started_at or datetime.now(timezone.utc)
        self.total_orders = 0
        self.total_fills = 0
        self.total_risk_rejections = 0
        self.markets_seen: set[str] = set()
        self.markets_traded: set[str] = set()
        self.max_equity: float | None = None
        self.min_equity: float | None = None
        self.max_drawdown = 0.0
        self._peak_equity: float | None = None
        self._initialize_files()

    def emit(
        self,
        event: str,
        *,
        level: str = "INFO",
        iteration: int | None = None,
        market_slug: str | None = None,
        condition_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append one structured JSON event and matching CSV row when applicable."""

        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event": event,
            "level": level,
        }
        if iteration is not None:
            payload["iteration"] = iteration
        if market_slug is not None:
            payload["market_slug"] = market_slug
        if condition_id is not None:
            payload["condition_id"] = condition_id
        payload.update(fields)

        serializable_payload = _to_jsonable(payload)
        with self.paths.events_jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(serializable_payload, ensure_ascii=False, sort_keys=True) + "\n")
        self._write_market_event(serializable_payload)

        self._update_stats(serializable_payload)
        self._write_csv_event(serializable_payload)
        return serializable_payload

    def write_config(self, config: Any) -> None:
        """Persist the run configuration as JSON."""

        with self.paths.config_json_path.open("w", encoding="utf-8") as file:
            json.dump(_to_jsonable(config), file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")

    def write_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Persist the final session summary."""

        serializable_summary = _to_jsonable(summary)
        with self.paths.summary_json_path.open("w", encoding="utf-8") as file:
            json.dump(serializable_summary, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        return serializable_summary

    def base_summary(self, ended_at: datetime | None = None) -> dict[str, Any]:
        """Return counters accumulated by this logger."""

        end_time = ended_at or datetime.now(timezone.utc)
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": end_time,
            "duration_seconds": (end_time - self.started_at).total_seconds(),
            "market_data_mode": self.market_data_mode,
            "max_equity": self.max_equity,
            "min_equity": self.min_equity,
            "max_drawdown": self.max_drawdown,
            "total_orders": self.total_orders,
            "total_fills": self.total_fills,
            "total_risk_rejections": self.total_risk_rejections,
            "markets_seen": sorted(self.markets_seen),
            "markets_traded": sorted(self.markets_traded),
        }

    def _initialize_files(self) -> None:
        self.paths.events_jsonl_path.touch(exist_ok=True)
        self._write_csv_header(self.paths.snapshots_csv_path, SNAPSHOT_CSV_FIELDS)
        self._write_csv_header(self.paths.orders_csv_path, ORDER_CSV_FIELDS)
        self._write_csv_header(self.paths.fills_csv_path, FILL_CSV_FIELDS)
        self._write_csv_header(self.paths.positions_csv_path, POSITION_CSV_FIELDS)
        self._write_csv_header(self.paths.risk_rejections_csv_path, RISK_CSV_FIELDS)

    @staticmethod
    def _write_csv_header(path: Path, fields: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()

    def _append_csv_row(self, path: Path, fields: list[str], payload: dict[str, Any]) -> None:
        row = {field: _to_csv_value(payload.get(field)) for field in fields}
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writerow(row)

    def _write_csv_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "snapshot":
            self._append_csv_row(self.paths.snapshots_csv_path, SNAPSHOT_CSV_FIELDS, payload)
        elif event in {"order_placed", "order_cancelled"}:
            if event == "order_placed" and "remaining_size" not in payload:
                payload = {**payload, "remaining_size": payload.get("size")}
            self._append_csv_row(self.paths.orders_csv_path, ORDER_CSV_FIELDS, payload)
        elif event == "fill":
            self._append_csv_row(self.paths.fills_csv_path, FILL_CSV_FIELDS, payload)
        elif event == "position":
            self._append_csv_row(self.paths.positions_csv_path, POSITION_CSV_FIELDS, payload)
        elif event == "risk_rejection":
            self._append_csv_row(self.paths.risk_rejections_csv_path, RISK_CSV_FIELDS, payload)

    def _write_market_event(self, payload: dict[str, Any]) -> None:
        market_slug = payload.get("market_slug")
        if not isinstance(market_slug, str) or not market_slug:
            return
        market_path = self.paths.markets_dir / f"{_safe_filename(market_slug)}.jsonl"
        with market_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _update_stats(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        market_slug = payload.get("market_slug")
        if event in {"market_selected", "snapshot"} and isinstance(market_slug, str):
            self.markets_seen.add(market_slug)
        if event == "fill" and isinstance(market_slug, str):
            self.markets_traded.add(market_slug)
        if event == "order_placed":
            self.total_orders += 1
        elif event == "fill":
            self.total_fills += 1
        elif event == "risk_rejection":
            self.total_risk_rejections += 1

        equity = _safe_float(payload.get("total_equity", payload.get("equity")))
        if event == "position" and equity is not None:
            self.max_equity = equity if self.max_equity is None else max(self.max_equity, equity)
            self.min_equity = equity if self.min_equity is None else min(self.min_equity, equity)
            self._peak_equity = equity if self._peak_equity is None else max(self._peak_equity, equity)
            self.max_drawdown = max(self.max_drawdown, (self._peak_equity or equity) - equity)


def snapshot_event_fields(snapshot: MarketSnapshot, time_to_expiry: float | None = None) -> dict[str, Any]:
    """Return structured fields for a top-of-book snapshot."""

    return {
        "yes_bid": snapshot.yes.best_bid,
        "yes_ask": snapshot.yes.best_ask,
        "yes_bid_size": snapshot.yes.bid_size,
        "yes_ask_size": snapshot.yes.ask_size,
        "no_bid": snapshot.no.best_bid,
        "no_ask": snapshot.no.best_ask,
        "no_bid_size": snapshot.no.bid_size,
        "no_ask_size": snapshot.no.ask_size,
        "yes_mid": snapshot.yes.midpoint,
        "no_mid": snapshot.no.midpoint,
        "yes_ask_plus_no_ask": snapshot.yes.best_ask + snapshot.no.best_ask,
        "yes_bid_plus_no_bid": snapshot.yes.best_bid + snapshot.no.best_bid,
        "time_to_expiry": time_to_expiry,
    }


def order_event_fields(order: Order, *, reason: str | None = None, cancel_reason: str | None = None) -> dict[str, Any]:
    """Return common structured fields for order events."""

    return {
        "order_id": order.order_id,
        "market_slug": order.market_slug,
        "condition_id": order.condition_id,
        "token_id": order.token_id,
        "outcome": order.outcome,
        "side": order.side,
        "price": order.price,
        "size": order.size,
        "remaining_size": order.remaining_size,
        "reason": reason,
        "cancel_reason": cancel_reason,
    }


def fill_event_fields(fill: Fill, *, time_to_expiry: float | None = None) -> dict[str, Any]:
    """Return structured fields for a fill event."""

    return {
        "order_id": fill.order_id,
        "market_slug": fill.market_slug,
        "condition_id": fill.condition_id,
        "token_id": fill.token_id,
        "outcome": fill.outcome,
        "side": fill.side,
        "price": fill.price,
        "size": fill.size,
        "notional": fill.notional,
        "time_to_expiry": time_to_expiry,
    }


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _to_csv_value(value: Any) -> Any:
    jsonable_value = _to_jsonable(value)
    if isinstance(jsonable_value, (dict, list)):
        return json.dumps(jsonable_value, ensure_ascii=False, sort_keys=True)
    return jsonable_value


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_filename(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
