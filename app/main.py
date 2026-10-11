from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import random
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.providers import TWELVE_DATA_INTERVALS, parse_twelve_data_candles
from app.signal_ledger import performance_summary, record_signal, resolve_closed_candles
from app.strategy import analyze_next_candle, assess_higher_timeframe_trend

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("market-board")

BINANCE_SYMBOLS = [
    item.strip().upper()
    for item in os.getenv(
        "BINANCE_SYMBOLS",
        os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,XRPUSDT,BNBUSDT,SOLUSDT,ADAUSDT,DOGEUSDT"),
    ).split(",")
    if item.strip()
]
TWELVE_DATA_SYMBOLS = [
    item.strip().upper()
    for item in os.getenv(
        "TWELVE_DATA_SYMBOLS",
        "EUR/USD,GBP/USD,USD/JPY,USD/CHF,AUD/USD,USD/CAD,NZD/USD,"
        "EUR/JPY,CAD/JPY,GBP/JPY,EUR/GBP,AUD/JPY,NZD/JPY,CHF/JPY,"
        "XAU/USD,XAG/USD,WTI/USD,BRENT/USD,AAPL,MSFT,SPY,QQQ",
    ).split(",")
    if item.strip()
]
SYMBOLS = list(dict.fromkeys(BINANCE_SYMBOLS + TWELVE_DATA_SYMBOLS))
INTERVALS = [item.strip() for item in os.getenv("INTERVALS", "1m,5m,15m,30m,1h,4h,1d").split(",") if item.strip()]
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
BINANCE_REST_URL = "https://api.binance.com/api/v3/klines"
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
TWELVE_DATA_REST_URL = "https://api.twelvedata.com/time_series"
FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"
NEWS_BLACKOUT_BEFORE_MINUTES = int(os.getenv("NEWS_BLACKOUT_BEFORE_MINUTES", "30"))
NEWS_BLACKOUT_AFTER_MINUTES = int(os.getenv("NEWS_BLACKOUT_AFTER_MINUTES", "15"))
MAX_CANDLES = 500
BINANCE_PING_INTERVAL_SECONDS = 10
BINANCE_STALE_AFTER_SECONDS = 30
BINANCE_STABLE_CONNECTION_SECONDS = 30
ANALYSIS_CACHE: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
SIGNAL_DB_PATH = Path(os.getenv("SIGNAL_DB_PATH", str(Path(__file__).parent.parent / "market_signals.sqlite3")))


def is_binance_symbol(symbol: str) -> bool:
    return symbol in BINANCE_SYMBOLS


def interval_milliseconds(interval: str) -> int:
    intervals = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
        "1w": 604_800_000,
    }
    if interval not in intervals:
        raise ValueError(f"Unsupported interval: {interval}")
    return intervals[interval]


def websocket_reconnect_delay(attempt: int) -> float:
    base_delay = min(2 ** max(attempt - 1, 0), 30)
    return min(30.0, base_delay * random.uniform(0.8, 1.2))


def utc_datetime_parameter(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def higher_timeframes(interval: str) -> tuple[str, ...]:
    return {
        "1m": ("15m", "1h"),
        "5m": ("15m", "1h"),
        "15m": ("1h", "4h"),
        "30m": ("1h", "4h"),
        "1h": ("4h", "1d"),
        "4h": ("1d",),
        "1d": ("1w",),
    }[interval]


def relevant_news_countries(symbol: str) -> set[str]:
    currency_countries = {
        "USD": {"US"},
        "EUR": {"EU", "DE", "FR", "IT", "ES"},
        "GBP": {"GB"},
        "JPY": {"JP"},
        "CHF": {"CH"},
        "CAD": {"CA"},
        "AUD": {"AU"},
        "NZD": {"NZ"},
    }
    if "/" in symbol:
        currencies = symbol.split("/")
    elif symbol.endswith(("USDT", "USDC", "BUSD")):
        currencies = ["USD"]
    else:
        currencies = ["USD"]
    return set().union(*(currency_countries.get(currency, set()) for currency in currencies))


async def fetch_news_blackout(
    client: httpx.AsyncClient,
    symbol: str,
    now_ms: int,
) -> dict[str, Any]:
    if not FINNHUB_API_KEY:
        return {
            "status": "unavailable",
            "events": [],
            "message": "Set FINNHUB_API_KEY to verify high-impact economic news; signals are blocked until then.",
        }
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    try:
        response = await client.get(
            FINNHUB_CALENDAR_URL,
            params={
                "from": (now.date() - timedelta(days=1)).isoformat(),
                "to": (now.date() + timedelta(days=1)).isoformat(),
                "token": FINNHUB_API_KEY,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise ValueError("Finnhub rejected the economic calendar request.")
        events = payload.get("economicCalendar")
        if not isinstance(events, list):
            raise ValueError("Finnhub returned an invalid economic calendar response.")
        countries = relevant_news_countries(symbol)
        before = timedelta(minutes=NEWS_BLACKOUT_BEFORE_MINUTES)
        after = timedelta(minutes=NEWS_BLACKOUT_AFTER_MINUTES)
        upcoming: list[dict[str, str]] = []
        for event in events:
            if str(event.get("impact", "")).lower() != "high":
                continue
            if str(event.get("country", "")).upper() not in countries:
                continue
            event_time = datetime.fromisoformat(str(event["time"]).replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if now - after <= event_time <= now + before:
                upcoming.append({
                    "event": str(event.get("event", "High-impact event")),
                    "country": str(event.get("country", "")),
                    "time": event_time.astimezone(timezone.utc).isoformat(),
                })
        return {
            "status": "blackout" if upcoming else "clear",
            "events": upcoming,
            "message": (
                f"{len(upcoming)} high-impact event(s) fall within the news blackout window."
                if upcoming else "No matching high-impact events in the configured blackout window."
            ),
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        logger.exception("Could not verify Finnhub news calendar for %s", symbol)
        return {
            "status": "unavailable",
            "events": [],
            "message": "Finnhub calendar could not be verified; signals are blocked for safety.",
        }


def calculate_position_sizing(
    symbol: str,
    analysis: dict[str, Any],
    account_balance: float | None,
    risk_percent: float,
) -> dict[str, Any]:
    risk_amount = account_balance * risk_percent / 100 if account_balance is not None else None
    result: dict[str, Any] = {
        "account_balance": account_balance,
        "account_currency": "USD",
        "risk_percent": risk_percent,
        "max_risk_amount": 0.0 if analysis["action"] == "WAIT" else risk_amount,
        "estimated_units": None,
        "unit_label": None,
    }
    if analysis["action"] == "WAIT":
        result["message"] = "No-trade setup; position size is zero."
        result["estimated_units"] = 0
        return result
    if account_balance is None:
        result["message"] = "Enter account balance to calculate estimated position size."
        return result
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        label = f"{base} units"
    elif symbol.endswith(("USDT", "USDC", "BUSD")):
        quote = next(quote for quote in ("USDT", "USDC", "BUSD") if symbol.endswith(quote))
        label = f"{symbol[:-len(quote)]} units"
    elif symbol.endswith("USD"):
        quote, label = "USD", f"{symbol[:-3]} units"
    else:
        quote, label = "USD", "shares"
    if quote not in {"USD", "USDT", "USDC", "BUSD"}:
        result["message"] = (
            f"Cannot calculate exact position units in account currency USD without "
            f"a live {quote}/USD conversion rate."
        )
        return result
    stop_distance = abs(float(analysis["entry"]) - float(analysis["stop_loss"]))
    if stop_distance <= 0:
        result["message"] = "Cannot estimate position size because stop distance is zero."
        return result
    result["estimated_units"] = risk_amount / stop_distance
    result["unit_label"] = label
    result["message"] = "Approximate linear-contract size; excludes fees, spread, leverage, lot steps, and broker rules."
    return result


async def fetch_chart_candles(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str,
    from_time_ms: int,
    to_time_ms: int,
    count_back: int,
) -> list[dict[str, Any]]:
    if is_binance_symbol(symbol):
        rows = await fetch_binance_json(
            client,
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": from_time_ms,
                "endTime": to_time_ms,
                "limit": min(count_back, 1000),
            },
        )
        return [
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
            if len(row) >= 7 and from_time_ms <= int(row[0]) <= to_time_ms
        ]

    if not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Set TWELVE_DATA_API_KEY to enable Forex, metals, and stock market data.",
        )
    twelve_interval = TWELVE_DATA_INTERVALS.get(interval)
    if not twelve_interval:
        raise HTTPException(status_code=400, detail="This interval is not supported by Twelve Data.")
    response = await client.get(
        TWELVE_DATA_REST_URL,
        params={
            "symbol": symbol,
            "interval": twelve_interval,
            "outputsize": min(count_back, 5000),
            "order": "ASC",
            "timezone": "UTC",
            "start_date": utc_datetime_parameter(from_time_ms),
            "end_date": utc_datetime_parameter(to_time_ms),
        },
        headers={"Authorization": f"apikey {TWELVE_DATA_API_KEY}"},
    )
    response.raise_for_status()
    try:
        candles = parse_twelve_data_candles(
            response.json(),
            from_time_ms=from_time_ms,
            to_time_ms=to_time_ms,
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"Twelve Data response error: {exc}") from exc
    return candles[-count_back:]


async def fetch_binance_json(
    client: httpx.AsyncClient,
    params: dict[str, int | str],
    *,
    max_attempts: int = 4,
) -> Any:
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.get(BINANCE_REST_URL, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            retryable = status_code in {418, 429, 500, 502, 503, 504}
            if not retryable or attempt == max_attempts:
                raise
            retry_after = exc.response.headers.get("Retry-After")
            try:
                delay = min(900.0, max(0.0, float(retry_after))) if retry_after else websocket_reconnect_delay(attempt)
            except ValueError:
                delay = websocket_reconnect_delay(attempt)
            logger.warning(
                "Binance REST returned HTTP %s; retrying attempt %s/%s in %.1f seconds.",
                status_code,
                attempt + 1,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == max_attempts:
                raise
            delay = websocket_reconnect_delay(attempt)
            logger.warning(
                "Binance REST network failure; retrying attempt %s/%s in %.1f seconds.",
                attempt + 1,
                max_attempts,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Binance request retry loop exited unexpectedly.")


class MarketData:
    def __init__(self) -> None:
        self.candles: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=MAX_CANDLES))
        self.prices: dict[str, dict[str, Any]] = {}
        self.clients: set[WebSocket] = set()
        self.feed_state = "starting"
        self.feed_error: str | None = None
        self.reconnect_attempt = 0
        self.last_market_message_time_ms: int | None = None

    @staticmethod
    def key(symbol: str, interval: str) -> str:
        return f"{symbol}:{interval}"

    async def broadcast(self, message: dict[str, Any]) -> None:
        disconnected: list[WebSocket] = []
        for client in self.clients:
            try:
                await client.send_json(message)
            except (RuntimeError, WebSocketDisconnect, OSError):
                disconnected.append(client)
            except Exception:
                logger.exception("Could not broadcast market update to dashboard client.")
                disconnected.append(client)
        for client in disconnected:
            self.clients.discard(client)

    async def publish_feed_status(self, state: str, error: str | None = None) -> None:
        self.feed_state = state
        self.feed_error = error
        payload = {
            "state": state,
            "last_message_time_ms": self.last_market_message_time_ms,
            "reconnect_attempt": self.reconnect_attempt,
            "ping_interval_seconds": BINANCE_PING_INTERVAL_SECONDS,
            "error": error,
        }
        await self.broadcast({"type": "feed_status", "data": payload})

    async def heartbeat(self, connected_at_ms: int) -> None:
        while True:
            await asyncio.sleep(BINANCE_PING_INTERVAL_SECONDS)
            last_message_time = self.last_market_message_time_ms or connected_at_ms
            message_age_seconds = max(
                0,
                (int(time.time() * 1000) - last_message_time) // 1000,
            )
            state = "stale" if message_age_seconds >= BINANCE_STALE_AFTER_SECONDS else "connected"
            logger.info(
                "Binance feed heartbeat | state=%s | last market event %ss ago | "
                "WebSocket protocol ping every %ss",
                state,
                message_age_seconds,
                BINANCE_PING_INTERVAL_SECONDS,
            )
            await self.publish_feed_status(state)

    async def load_history(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            for symbol in BINANCE_SYMBOLS:
                for interval in INTERVALS:
                    history = self.candles[self.key(symbol, interval)]
                    params: dict[str, int | str] = {
                        "symbol": symbol,
                        "interval": interval,
                        "limit": MAX_CANDLES,
                        "endTime": int(time.time() * 1000),
                    }
                    interval_ms = interval_milliseconds(interval)
                    if history:
                        params["startTime"] = min(
                            params["endTime"],
                            max(
                                history[-1]["open_time"] + interval_ms,
                                params["endTime"] - interval_ms * MAX_CANDLES,
                            ),
                        )
                    try:
                        rows = await fetch_binance_json(client, params)
                        merged = {candle["open_time"]: candle for candle in history}
                        for row in rows:
                            if int(row[6]) >= int(time.time() * 1000):
                                continue
                            merged[int(row[0])] = {
                                "open_time": int(row[0]),
                                "open": float(row[1]),
                                "high": float(row[2]),
                                "low": float(row[3]),
                                "close": float(row[4]),
                                "volume": float(row[5]),
                                "close_time": int(row[6]),
                            }
                        history.clear()
                        history.extend(sorted(merged.values(), key=lambda candle: candle["open_time"])[-MAX_CANDLES:])
                        logger.info("Loaded %s candles for %s %s", len(history), symbol, interval)
                    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
                        logger.exception("Could not load Binance history for %s %s", symbol, interval)

    async def run(self) -> None:
        try:
            await self.load_history()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not load initial Binance history; continuing with the live stream.")
        streams = "/".join(
            f"{symbol.lower()}@kline_{interval}" for symbol in BINANCE_SYMBOLS for interval in INTERVALS
        )
        if not streams:
            await self.publish_feed_status("unavailable", "No Binance symbols or intervals are configured.")
            logger.error("Cannot start Binance stream without configured symbols and intervals.")
            return
        url = f"{BINANCE_WS_URL}?streams={streams}"
        reconnect_attempt = 0
        while True:
            connected_since: float | None = None
            try:
                if reconnect_attempt:
                    try:
                        await self.load_history()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Could not backfill Binance candles after disconnect.")
                await self.publish_feed_status("connecting")
                async with websockets.connect(
                    url,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=BINANCE_PING_INTERVAL_SECONDS,
                    ping_timeout=BINANCE_PING_INTERVAL_SECONDS,
                ) as socket:
                    connected_since = time.monotonic()
                    connected_at = int(time.time() * 1000)
                    await self.publish_feed_status("connected")
                    logger.info(
                        "Binance market WebSocket connected; protocol ping every %s seconds.",
                        BINANCE_PING_INTERVAL_SECONDS,
                    )
                    heartbeat_task = asyncio.create_task(self.heartbeat(connected_at))
                    try:
                        while True:
                            raw_message = await socket.recv()
                            try:
                                payload = json.loads(raw_message)
                            except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
                                logger.warning("Ignoring malformed Binance WebSocket message: %s", exc)
                                continue
                            await self.handle_message(payload)
                    finally:
                        heartbeat_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await heartbeat_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if (
                    connected_since is not None
                    and time.monotonic() - connected_since >= BINANCE_STABLE_CONNECTION_SECONDS
                ):
                    reconnect_attempt = 0
                reconnect_attempt += 1
                self.reconnect_attempt = reconnect_attempt
                delay = websocket_reconnect_delay(reconnect_attempt)
                await self.publish_feed_status("reconnecting", str(exc))
                logger.exception(
                    "Binance stream disconnected; reconnect attempt %s in %.1f seconds.",
                    reconnect_attempt,
                    delay,
                )
                await asyncio.sleep(delay)

    async def handle_message(self, message: dict[str, Any]) -> None:
        payload = message.get("data", {}) if isinstance(message, dict) else None
        if not isinstance(payload, dict):
            logger.warning("Ignoring malformed Binance stream message without a data object.")
            return
        kline = payload.get("k")
        if not isinstance(kline, dict):
            return
        try:
            symbol = str(kline["s"]).upper()
            interval = str(kline["i"])
            price = float(kline["c"])
            open_price = float(kline["o"])
            high = float(kline["h"])
            low = float(kline["l"])
            volume = float(kline["v"])
            open_time = int(kline["t"])
            close_time = int(kline["T"])
            event_time = int(payload.get("E", 0))
            candle_closed = kline["x"]
            values = (price, open_price, high, low, volume)
            if (
                not symbol
                or not interval
                or not isinstance(candle_closed, bool)
                or not all(math.isfinite(value) for value in values)
                or low > min(open_price, price)
                or high < max(open_price, price)
                or high < low
                or volume < 0
                or close_time < open_time
            ):
                raise ValueError("invalid kline fields or OHLCV values")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("Ignoring malformed Binance kline event: %s", exc)
            return
        history = self.candles[self.key(symbol, interval)]
        if candle_closed and history and open_time < history[-1]["open_time"]:
            logger.warning(
                "Ignoring out-of-order Binance candle for %s %s: %s < %s",
                symbol,
                interval,
                open_time,
                history[-1]["open_time"],
            )
            return
        self.prices[symbol] = {
            "symbol": symbol,
            "price": price,
            "event_time": event_time,
            "interval": interval,
            "candle_closed": candle_closed,
        }
        self.last_market_message_time_ms = int(time.time() * 1000)
        if self.feed_state != "connected":
            await self.publish_feed_status("connected")
        await self.broadcast({"type": "price", "data": self.prices[symbol]})
        candle = {
            "open_time": open_time,
            "open": open_price,
            "high": high,
            "low": low,
            "close": price,
            "volume": volume,
            "close_time": close_time,
        }
        await self.broadcast({"type": "candle", "data": {"symbol": symbol, "interval": interval, **candle}})
        if not kline["x"]:
            return

        try:
            outcomes = resolve_closed_candles(SIGNAL_DB_PATH, symbol, interval, [candle])
        except sqlite3.Error:
            logger.exception("Could not resolve signal outcomes for Binance candle %s %s.", symbol, interval)
            outcomes = []
        for outcome in outcomes:
            await self.broadcast({"type": "signal_outcome", "data": outcome})
        if history and history[-1]["open_time"] == candle["open_time"]:
            return
        history.append(candle)

    def snapshot(self) -> dict[str, Any]:
        latest_candles = {
            key: dict(history[-1])
            for key, history in self.candles.items()
            if history
        }
        return {
            "symbols": SYMBOLS,
            "binance_symbols": BINANCE_SYMBOLS,
            "twelve_data_symbols": TWELVE_DATA_SYMBOLS,
            "twelve_data_configured": bool(TWELVE_DATA_API_KEY),
            "finnhub_configured": bool(FINNHUB_API_KEY),
            "intervals": INTERVALS,
            "feed": {
                "state": self.feed_state,
                "last_message_time_ms": self.last_market_message_time_ms,
                "reconnect_attempt": self.reconnect_attempt,
                "ping_interval_seconds": BINANCE_PING_INTERVAL_SECONDS,
                "error": self.feed_error,
            },
            "prices": list(self.prices.values()),
            "candles": latest_candles,
        }


market = MarketData()


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(market.run())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Live Market Signal Board", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def dashboard() -> Any:
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/snapshot")
async def snapshot() -> dict[str, Any]:
    return market.snapshot()


@app.get("/api/performance")
async def signal_performance(
    symbol: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    normalized_symbol = symbol.upper() if symbol else None
    if normalized_symbol and normalized_symbol not in SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol is not enabled in the configured market symbols.")
    if interval and interval not in INTERVALS:
        raise HTTPException(status_code=400, detail="Interval is not enabled in INTERVALS.")
    return performance_summary(SIGNAL_DB_PATH, normalized_symbol, interval)


@app.get("/api/chart-library/status")
async def chart_library_status() -> dict[str, bool]:
    library = Path(__file__).parent / "static" / "charting_library" / "charting_library.standalone.js"
    return {"available": library.is_file()}


@app.get("/api/chart/history")
async def chart_history(
    symbol: str,
    interval: str,
    from_time: int = Query(..., alias="from", ge=0),
    to_time: int = Query(..., alias="to", gt=0),
    count_back: int = Query(500, alias="countBack", ge=1, le=1000),
) -> dict[str, Any]:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol is not enabled in the configured market symbols.")
    if interval not in INTERVALS:
        raise HTTPException(status_code=400, detail="Interval is not enabled in INTERVALS.")
    if from_time >= to_time:
        raise HTTPException(status_code=400, detail="'from' must be earlier than 'to'.")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            candles = await fetch_chart_candles(
                client,
                symbol,
                interval,
                from_time * 1000,
                to_time * 1000,
                count_back,
            )
    except httpx.HTTPError as exc:
        logger.exception("Could not fetch chart history for %s %s", symbol, interval)
        raise HTTPException(status_code=502, detail="Market data history request failed.") from exc
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        logger.exception("Market data returned malformed chart history for %s %s", symbol, interval)
        raise HTTPException(status_code=502, detail="Market data provider returned malformed candles.") from exc

    bars = [
        {
            "time": candle["open_time"],
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
        }
        for candle in candles
    ]
    return {"bars": bars, "no_data": not bars}


@app.get("/api/analyze")
async def analyze_market(
    symbol: str,
    interval: str,
    preference: str = "ANY",
    account_balance: float | None = Query(None, gt=0),
    risk_percent: float = Query(1.0, ge=0.1, le=2.0),
) -> dict[str, Any]:
    symbol = symbol.upper()
    preference = preference.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=400, detail="Symbol is not enabled in the configured market symbols.")
    if interval not in INTERVALS:
        raise HTTPException(status_code=400, detail="Interval is not enabled in INTERVALS.")
    if preference not in {"ANY", "LONG", "SHORT"}:
        raise HTTPException(status_code=400, detail="preference must be ANY, LONG, or SHORT.")
    if not is_binance_symbol(symbol) and not TWELVE_DATA_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Set TWELVE_DATA_API_KEY to analyze Forex, metals, and stock markets.",
        )

    current_time = int(time.time() * 1000)
    interval_ms = interval_milliseconds(interval)
    higher_intervals = higher_timeframes(interval)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            candles = await fetch_chart_candles(
                client,
                symbol,
                interval,
                max(0, current_time - interval_ms * 500),
                current_time,
                500,
            )
    except httpx.HTTPError as exc:
        logger.exception("Could not fetch analysis history for %s %s", symbol, interval)
        raise HTTPException(status_code=502, detail="Market data analysis request failed.") from exc
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        logger.exception("Market data returned malformed analysis history for %s %s", symbol, interval)
        raise HTTPException(status_code=502, detail="Market data provider returned malformed candles.") from exc

    closed_candles = [
        {
            **candle,
            "close_time": candle["open_time"] + interval_ms - 1,
        }
        for candle in candles
        if candle["open_time"] + interval_ms - 1 < current_time
    ]
    if not closed_candles:
        raise HTTPException(status_code=422, detail="No completed candles are available for analysis.")
    resolved_outcomes = resolve_closed_candles(SIGNAL_DB_PATH, symbol, interval, closed_candles)
    for outcome in resolved_outcomes:
        await market.broadcast({"type": "signal_outcome", "data": outcome})
    last_closed_candle = closed_candles[-1]
    cache_key = (symbol, interval, preference, account_balance, risk_percent)

    news_filter: dict[str, Any]
    higher_trends: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            async def fetch_higher_trend(higher_interval: str) -> tuple[str, str]:
                higher_ms = interval_milliseconds(higher_interval)
                history = await fetch_chart_candles(
                    client,
                    symbol,
                    higher_interval,
                    max(0, current_time - higher_ms * 120),
                    current_time,
                    120,
                )
                closed_history = [
                    candle for candle in history
                    if candle["open_time"] + higher_ms - 1 < current_time
                ]
                return higher_interval, assess_higher_timeframe_trend(closed_history)

            high_results, news_filter = await asyncio.gather(
                asyncio.gather(*(fetch_higher_trend(item) for item in higher_intervals)),
                fetch_news_blackout(client, symbol, current_time),
            )
            higher_trends = dict(high_results)
    except (httpx.HTTPError, HTTPException, ValueError, TypeError, KeyError, IndexError):
        logger.exception("Could not verify higher-timeframe market data for %s %s", symbol, interval)
        higher_trends = {higher_interval: "UNAVAILABLE" for higher_interval in higher_intervals}
        news_filter = {
            "status": "unavailable",
            "events": [],
            "message": "Required market/news confirmation data could not be verified; signals are blocked.",
        }

    cached_analysis = ANALYSIS_CACHE.get(cache_key)
    if cached_analysis and cached_analysis[0] == last_closed_candle["open_time"]:
        analysis = copy.deepcopy(cached_analysis[1])
    else:
        try:
            analysis = analyze_next_candle(closed_candles, symbol, interval, preference)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    deterministic_analysis = copy.deepcopy(analysis)

    expected_trend = "BULLISH" if analysis["next_candle_prediction"] == "UP" else (
        "BEARISH" if analysis["next_candle_prediction"] == "DOWN" else "SIDEWAYS"
    )
    has_higher_confluence = bool(higher_trends) and all(
        trend == expected_trend for trend in higher_trends.values()
    )
    analysis["higher_timeframe_trends"] = higher_trends
    analysis["higher_timeframe_confluence"] = has_higher_confluence
    if not has_higher_confluence and analysis["action"] != "WAIT":
        analysis["action"] = "WAIT"
        analysis["warning"] = "Higher-timeframe trend does not confirm the next-candle direction; no trade."
    analysis["news_filter"] = news_filter
    if news_filter["status"] != "clear":
        analysis["action"] = "WAIT"
        analysis["warning"] = news_filter["message"]
    if analysis["indicators"]["volatility_spike"]:
        analysis["action"] = "WAIT"
        analysis["warning"] = "Volatility is unusually high versus its recent baseline; no trade."

    if analysis["action"] == "WAIT":
        analysis["entry"] = None
        analysis["stop_loss"] = None
        analysis["take_profit_1"] = None
        analysis["take_profit_2"] = None
        analysis["risk_reward_tp1"] = None
        analysis["risk_reward_tp2"] = None
    else:
        risk_distance = abs(analysis["entry"] - analysis["stop_loss"])
        if not risk_distance or abs(analysis["take_profit_1"] - analysis["entry"]) / risk_distance < 2:
            analysis["action"] = "WAIT"
            analysis["warning"] = "The setup does not meet the minimum 1:2 risk-to-reward requirement."
            analysis["entry"] = None
            analysis["stop_loss"] = None
            analysis["take_profit_1"] = None
            analysis["take_profit_2"] = None
            analysis["risk_reward_tp1"] = None
            analysis["risk_reward_tp2"] = None

    if analysis["action"] == "WAIT":
        analysis["confidence_level"] = "LOW"
        if analysis.get("warning"):
            analysis["brief_rationale"] = analysis["warning"]

    analysis["analyzed_candle_open_time"] = last_closed_candle["open_time"]
    analysis["signal_for_candle_open_time"] = last_closed_candle["open_time"] + interval_ms
    analysis["signal_valid_until"] = last_closed_candle["open_time"] + interval_ms * 2
    analysis["signal_hold_time"] = interval
    if candles:
        analysis["latest_candle"] = {
            "symbol": symbol,
            "interval": interval,
            **candles[-1],
            "close_time": candles[-1]["open_time"] + interval_ms - 1,
        }
    analysis["position_sizing"] = calculate_position_sizing(symbol, analysis, account_balance, risk_percent)
    record_signal(SIGNAL_DB_PATH, analysis)
    ANALYSIS_CACHE[cache_key] = (last_closed_candle["open_time"], deterministic_analysis)
    return analysis


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    market.clients.add(websocket)
    await websocket.send_json({"type": "snapshot", "data": market.snapshot()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        market.clients.discard(websocket)
