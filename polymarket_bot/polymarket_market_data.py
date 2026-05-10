"""Public Polymarket market data clients.

This module is intentionally read-only. It uses public Gamma and CLOB endpoints
for market discovery and order book snapshots, and it does not contain any
order-routing, signing, private-key, or API-key logic.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from .models import MarketSnapshot, Token, TokenMarketSnapshot, utc_now

BTC_15M_SLUG_PREFIXES = ("btc-updown-15m", "btc-up-or-down-15m")
FIFTEEN_MINUTES_SECONDS = 15 * 60


class MarketDataError(RuntimeError):
    """Raised when public market data cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    """Polymarket binary market selected for paper trading."""

    question: str
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str


def parse_clob_token_ids(raw_value: Any) -> list[str]:
    """Parse Gamma's `clobTokenIds` value into a token id list.

    Gamma commonly returns this field as a JSON-encoded string, but tests and
    fixtures often use a plain list. Supporting both keeps the discovery layer
    tolerant without hiding malformed responses.
    """

    if isinstance(raw_value, str):
        value = raw_value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(raw_value, list):
        parsed = raw_value
    elif isinstance(raw_value, tuple):
        parsed = list(raw_value)
    else:
        return []

    return [str(token_id) for token_id in parsed if str(token_id).strip()]


def select_yes_no_token_ids(clob_token_ids: list[str]) -> tuple[str, str]:
    """Return YES and NO token ids from Gamma's binary outcome token list."""

    if len(clob_token_ids) < 2:
        raise MarketDataError(f"expected at least two clobTokenIds, got {len(clob_token_ids)}")
    return clob_token_ids[0], clob_token_ids[1]


def generate_btc_15m_slug_candidates(
    now: datetime | None = None,
    window_offsets: tuple[int, ...] = (0, 1, -1, 2, -2, 3, -3, 4, -4),
) -> list[str]:
    """Generate timestamp-based BTC 15-minute Polymarket slug candidates.

    Polymarket's rolling BTC 15m events use slugs like
    `btc-updown-15m-1778410800`, where the suffix is a Unix timestamp aligned
    to a 15-minute UTC boundary. We try the current window first, then nearby
    windows to tolerate small clock and publication timing differences.
    """

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    timestamp = int(current_time.timestamp())
    base_timestamp = timestamp - (timestamp % FIFTEEN_MINUTES_SECONDS)

    slugs: list[str] = []
    seen: set[str] = set()
    for offset in window_offsets:
        candidate_timestamp = base_timestamp + offset * FIFTEEN_MINUTES_SECONDS
        if candidate_timestamp <= 0:
            continue
        for prefix in BTC_15M_SLUG_PREFIXES:
            slug = f"{prefix}-{candidate_timestamp}"
            if slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
    return slugs


@dataclass(slots=True)
class MarketDiscoveryClient:
    """Discover active BTC 15-minute markets using the public Gamma API."""

    logger: logging.Logger
    gamma_base_url: str
    btc_market_query: str
    timeout_seconds: float = 10.0
    page_limit: int = 100
    max_pages: int = 5

    def discover_btc_15m_market(self) -> DiscoveredMarket:
        """Find the best active BTC 15-minute market and return token ids."""

        markets = self._fetch_active_markets()
        scored_markets = []
        for market in markets:
            token_ids = parse_clob_token_ids(
                market.get("clobTokenIds", market.get("clob_token_ids"))
            )
            if token_ids:
                scored_markets.append((self._score_market(market), market))
        scored_markets = [(score, market) for score, market in scored_markets if score > 0]
        if not scored_markets:
            raise MarketDataError(
                f"no active BTC 15-minute market found for query={self.btc_market_query!r}"
            )

        _, selected = max(scored_markets, key=lambda item: item[0])
        token_ids = parse_clob_token_ids(
            selected.get("clobTokenIds", selected.get("clob_token_ids"))
        )
        yes_token_id, no_token_id = select_yes_no_token_ids(token_ids)
        market = DiscoveredMarket(
            question=str(selected.get("question") or selected.get("title") or ""),
            slug=str(selected.get("slug") or ""),
            condition_id=str(selected.get("conditionId") or selected.get("condition_id") or ""),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )
        self.logger.info(
            "Selected Polymarket market | question=%s slug=%s condition_id=%s "
            "yes_token_id=%s no_token_id=%s",
            market.question,
            market.slug,
            market.condition_id,
            market.yes_token_id,
            market.no_token_id,
        )
        return market

    def _fetch_active_markets(self) -> list[dict[str, Any]]:
        timestamp_markets = self._fetch_timestamp_slug_markets()
        if timestamp_markets:
            return timestamp_markets

        markets: list[dict[str, Any]] = []
        for slug_contains in ("btc-updown-15m", "btc-up-or-down-15m"):
            markets.extend(self._fetch_active_slug_markets(slug_contains=slug_contains))
            markets.extend(self._fetch_active_event_markets(slug_contains=slug_contains))

        for page in range(self.max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": str(self.page_limit),
                "offset": str(page * self.page_limit),
                "order": "volume24hr",
                "ascending": "false",
            }
            payload = _get_json(
                base_url=self.gamma_base_url,
                path="/markets",
                params=params,
                timeout_seconds=self.timeout_seconds,
            )
            page_markets = payload if isinstance(payload, list) else payload.get("markets", [])
            if not page_markets:
                break
            markets.extend(
                market
                for market in page_markets
                if isinstance(market, dict) and _is_open_market_record(market)
            )
            if len(page_markets) < self.page_limit:
                break

        return markets

    def _fetch_timestamp_slug_markets(self) -> list[dict[str, Any]]:
        for slug in generate_btc_15m_slug_candidates():
            market = self._fetch_market_from_event_slug(slug) or self._fetch_market_from_market_slug(slug)
            if market is None:
                continue
            if not _is_open_market_record(market):
                continue
            self.logger.info("Found BTC 15m market via timestamp slug | slug=%s", slug)
            return [market]
        return []

    def _fetch_market_from_event_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            payload = _get_json(
                base_url=self.gamma_base_url,
                path=f"/events/slug/{slug}",
                params={},
                timeout_seconds=self.timeout_seconds,
            )
        except MarketDataError:
            return None

        if not isinstance(payload, dict):
            return None
        event_title = payload.get("title") or payload.get("question") or ""
        event_slug = payload.get("slug") or slug
        markets = payload.get("markets", [])
        if not isinstance(markets, list):
            return None

        for market in markets:
            if not isinstance(market, dict):
                continue
            market.setdefault("question", event_title)
            market.setdefault("slug", event_slug)
            return market
        return None

    def _fetch_market_from_market_slug(self, slug: str) -> dict[str, Any] | None:
        try:
            payload = _get_json(
                base_url=self.gamma_base_url,
                path=f"/markets/slug/{slug}",
                params={},
                timeout_seconds=self.timeout_seconds,
            )
        except MarketDataError:
            return None

        if not isinstance(payload, dict):
            return None
        payload.setdefault("slug", slug)
        return payload

    def _fetch_active_slug_markets(self, slug_contains: str) -> list[dict[str, Any]]:
        payload = _get_json(
            base_url=self.gamma_base_url,
            path="/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": str(self.page_limit),
                "_limit": str(self.page_limit),
                "slug_contains": slug_contains,
            },
            timeout_seconds=self.timeout_seconds,
        )
        page_markets = payload if isinstance(payload, list) else payload.get("markets", [])
        return [
            market
            for market in page_markets
            if isinstance(market, dict) and _is_open_market_record(market)
        ]

    def _fetch_active_event_markets(self, slug_contains: str) -> list[dict[str, Any]]:
        event_markets: list[dict[str, Any]] = []
        payload = _get_json(
            base_url=self.gamma_base_url,
            path="/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": str(self.page_limit),
                "_limit": str(self.page_limit),
                "slug_contains": slug_contains,
            },
            timeout_seconds=self.timeout_seconds,
        )
        events = payload if isinstance(payload, list) else payload.get("events", [])
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []):
                if not isinstance(market, dict):
                    continue
                if not _is_open_market_record(market):
                    continue
                market.setdefault("question", event.get("title") or event.get("question") or "")
                market.setdefault("slug", event.get("slug") or "")
                event_markets.append(market)
        return event_markets

    def _score_market(self, market: dict[str, Any]) -> int:
        question = str(market.get("question") or market.get("title") or "")
        slug = str(market.get("slug") or "")
        text = _normalize_text(f"{question} {slug}")
        query_terms = _normalize_text(self.btc_market_query).split()

        has_btc = "btc" in text or "bitcoin" in text
        has_15m = "15m" in text or "15 min" in text or "15 minute" in text
        if not has_btc or not has_15m:
            return 0

        score = 0
        score += 20
        if "updown" in text or ("up" in text and "down" in text):
            score += 2
        if market.get("acceptingOrders") is True or market.get("accepting_orders") is True:
            score += 3
        score += sum(1 for term in query_terms if term in text)
        return score


@dataclass(slots=True)
class PolymarketOrderBookClient:
    """Read top-of-book data from the public Polymarket CLOB API."""

    logger: logging.Logger
    clob_base_url: str
    timeout_seconds: float = 10.0

    def get_market_snapshot(self, market: DiscoveredMarket) -> MarketSnapshot | None:
        """Fetch YES/NO books and convert them to a `MarketSnapshot`."""

        yes_book = self.get_orderbook(market.yes_token_id)
        no_book = self.get_orderbook(market.no_token_id)
        yes_snapshot = self.orderbook_to_token_snapshot(yes_book, Token.YES, market.yes_token_id)
        no_snapshot = self.orderbook_to_token_snapshot(no_book, Token.NO, market.no_token_id)
        if yes_snapshot is None or no_snapshot is None:
            return None

        snapshot = MarketSnapshot(yes=yes_snapshot, no=no_snapshot, timestamp=utc_now())
        self.logger.info(
            "Polymarket snapshot | question=%s slug=%s YES %.3f/%.3f NO %.3f/%.3f",
            market.question,
            market.slug,
            snapshot.yes.best_bid,
            snapshot.yes.best_ask,
            snapshot.no.best_bid,
            snapshot.no.best_ask,
        )
        return snapshot

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Fetch one public CLOB order book by token id."""

        payload = _get_json(
            base_url=self.clob_base_url,
            path="/book",
            params={"token_id": token_id},
            timeout_seconds=self.timeout_seconds,
        )
        if not isinstance(payload, dict):
            raise MarketDataError(f"unexpected CLOB orderbook response for token_id={token_id}")
        return payload

    def orderbook_to_token_snapshot(
        self,
        orderbook: dict[str, Any],
        token: Token,
        token_id: str,
    ) -> TokenMarketSnapshot | None:
        """Convert a CLOB order book response into a token snapshot."""

        bids = _parse_levels(orderbook.get("bids", []))
        asks = _parse_levels(orderbook.get("asks", []))
        if not bids or not asks:
            self.logger.warning(
                "Empty Polymarket orderbook side | token=%s token_id=%s bids=%d asks=%d",
                token,
                token_id,
                len(bids),
                len(asks),
            )
            return None

        best_bid, bid_size = max(bids, key=lambda level: level[0])
        best_ask, ask_size = min(asks, key=lambda level: level[0])
        last_trade_raw = orderbook.get("last_trade_price") or orderbook.get("lastTradePrice")
        last_trade_price = _safe_float(last_trade_raw)
        if last_trade_price is None:
            last_trade_price = (best_bid + best_ask) / 2.0

        return TokenMarketSnapshot(
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            last_trade_price=last_trade_price,
            timestamp=utc_now(),
        )


@dataclass(slots=True)
class PolymarketMarketDataClient:
    """High-level read-only market data provider for the paper bot."""

    discovery_client: MarketDiscoveryClient
    orderbook_client: PolymarketOrderBookClient
    market_refresh_seconds: float
    logger: logging.Logger
    _market: DiscoveredMarket | None = field(default=None, init=False, repr=False)
    _market_last_refreshed_at: datetime | None = field(default=None, init=False, repr=False)

    def get_snapshot(self) -> MarketSnapshot | None:
        """Return a live public Polymarket snapshot, or None when book data is incomplete."""

        market = self._get_or_refresh_market()
        return self.orderbook_client.get_market_snapshot(market)

    def _get_or_refresh_market(self) -> DiscoveredMarket:
        now = datetime.now(timezone.utc)
        refresh_due = (
            self._market is None
            or self._market_last_refreshed_at is None
            or now - self._market_last_refreshed_at >= timedelta(seconds=self.market_refresh_seconds)
        )
        if refresh_due:
            self._market = self.discovery_client.discover_btc_15m_market()
            self._market_last_refreshed_at = now
        return self._market


def _get_json(
    base_url: str,
    path: str,
    params: dict[str, str],
    timeout_seconds: float,
) -> Any:
    url = _build_url(base_url, path, params)
    request = Request(url, headers={"User-Agent": "polymarket-paper-bot/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise MarketDataError(f"HTTP {exc.code} while fetching {url}") from exc
    except URLError as exc:
        raise MarketDataError(f"network error while fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MarketDataError(f"timeout while fetching {url}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise MarketDataError(f"invalid JSON while fetching {url}") from exc


def _build_url(base_url: str, path: str, params: dict[str, str]) -> str:
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    return f"{url}?{urlencode(params)}" if params else url


def _parse_levels(raw_levels: Any) -> list[tuple[float, float]]:
    if not isinstance(raw_levels, list):
        return []

    levels: list[tuple[float, float]] = []
    for raw_level in raw_levels:
        if not isinstance(raw_level, dict):
            continue
        price = _safe_float(raw_level.get("price"))
        size = _safe_float(raw_level.get("size"))
        if price is None or size is None or size <= 0:
            continue
        levels.append((price, size))
    return levels


def _is_open_market_record(market: dict[str, Any]) -> bool:
    active = market.get("active")
    closed = market.get("closed")
    if active is False:
        return False
    if closed is True:
        return False
    return True


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: str) -> str:
    normalized = value.lower()
    normalized = normalized.replace("15-minute", "15 minute")
    normalized = normalized.replace("15min", "15 min")
    normalized = normalized.replace("15 mins", "15 min")
    normalized = normalized.replace("15 minutes", "15 minute")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())
