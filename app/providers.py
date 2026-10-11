from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


TWELVE_DATA_INTERVALS = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",
    "1w": "1week",
}


def parse_twelve_data_candles(
    payload: dict[str, Any],
    *,
    from_time_ms: int,
    to_time_ms: int,
) -> list[dict[str, Any]]:
    if payload.get("status") == "error":
        message = payload.get("message", "Twelve Data request failed.")
        raise ValueError(str(message))

    candles: list[dict[str, Any]] = []
    values = payload.get("values", [])
    if not isinstance(values, list):
        raise ValueError("Twelve Data returned an invalid time-series response.")
    for row in values:
        try:
            raw_time = str(row["datetime"])
            parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            open_time = int(parsed.timestamp() * 1000)
            if not from_time_ms <= open_time <= to_time_ms:
                continue
            candles.append(
                {
                    "open_time": open_time,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0) or 0),
                }
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Twelve Data returned a malformed candle.") from exc
    candles.sort(key=lambda candle: candle["open_time"])
    return candles
