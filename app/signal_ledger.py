from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_ledger (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            preference TEXT NOT NULL,
            analyzed_candle_open_time INTEGER NOT NULL,
            signal_for_candle_open_time INTEGER NOT NULL,
            prediction TEXT NOT NULL,
            action TEXT NOT NULL,
            entry REAL NOT NULL,
            stop_loss REAL NOT NULL,
            take_profit_1 REAL NOT NULL,
            take_profit_2 REAL NOT NULL,
            outcome TEXT NOT NULL DEFAULT 'PENDING',
            resolved_candle_open_time INTEGER,
            UNIQUE(symbol, interval, preference, analyzed_candle_open_time)
        )
        """
    )
    return connection


def record_signal(database_path: Path, analysis: dict[str, Any]) -> None:
    if analysis["action"] not in {"LONG", "SHORT"}:
        return
    with closing(_connect(database_path)) as connection, connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO signal_ledger (
                symbol, interval, preference, analyzed_candle_open_time,
                signal_for_candle_open_time, prediction, action, entry,
                stop_loss, take_profit_1, take_profit_2
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis["symbol"],
                analysis["interval"],
                analysis["preference"],
                analysis["analyzed_candle_open_time"],
                analysis["signal_for_candle_open_time"],
                analysis["next_candle_prediction"],
                analysis["action"],
                analysis["entry"],
                analysis["stop_loss"],
                analysis["take_profit_1"],
                analysis["take_profit_2"],
            ),
        )


def resolve_closed_candles(
    database_path: Path,
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    with closing(_connect(database_path)) as connection, connection:
        for candle in candles:
            row = connection.execute(
                """
                SELECT id, prediction
                FROM signal_ledger
                WHERE symbol = ? AND interval = ?
                  AND signal_for_candle_open_time = ? AND outcome = 'PENDING'
                """,
                (symbol, interval, candle["open_time"]),
            ).fetchone()
            if row is None:
                continue
            close = float(candle["close"])
            open_price = float(candle["open"])
            actual = "UP" if close > open_price else "DOWN" if close < open_price else "FLAT"
            outcome = "WIN" if actual == row["prediction"] else "LOSS" if actual != "FLAT" else "FLAT"
            connection.execute(
                """
                UPDATE signal_ledger
                SET outcome = ?, resolved_candle_open_time = ?
                WHERE id = ? AND outcome = 'PENDING'
                """,
                (outcome, candle["open_time"], row["id"]),
            )
            resolved.append({
                "symbol": symbol,
                "interval": interval,
                "prediction": row["prediction"],
                "actual_direction": actual,
                "outcome": outcome,
                "invalidated": outcome == "LOSS",
            })
    return resolved


def performance_summary(
    database_path: Path,
    symbol: str | None = None,
    interval: str | None = None,
) -> dict[str, Any]:
    filters: list[str] = []
    values: list[str] = []
    if symbol:
        filters.append("symbol = ?")
        values.append(symbol)
    if interval:
        filters.append("interval = ?")
        values.append(interval)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    with closing(_connect(database_path)) as connection, connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN outcome = 'FLAT' THEN 1 ELSE 0 END) AS flat,
                SUM(CASE WHEN outcome = 'PENDING' THEN 1 ELSE 0 END) AS pending
            FROM signal_ledger {where}
            """,
            values,
        ).fetchone()
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    resolved_directions = wins + losses
    return {
        "total_signals": row["total"],
        "wins": wins,
        "losses": losses,
        "flat": row["flat"] or 0,
        "pending": row["pending"] or 0,
        "directional_win_rate_pct": round(wins / resolved_directions * 100, 2) if resolved_directions else None,
        "outcome_basis": "Next candle close direction; not realized trade P&L.",
    }
