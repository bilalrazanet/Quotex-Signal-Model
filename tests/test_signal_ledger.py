import tempfile
import unittest
from pathlib import Path

from app.signal_ledger import performance_summary, record_signal, resolve_closed_candles


class SignalLedgerTests(unittest.TestCase):
    def test_records_deduplicated_signals_and_resolves_directional_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "signals.sqlite3"
            analysis = {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "preference": "ANY",
                "analyzed_candle_open_time": 60_000,
                "signal_for_candle_open_time": 120_000,
                "next_candle_prediction": "UP",
                "action": "LONG",
                "entry": 100.0,
                "stop_loss": 99.0,
                "take_profit_1": 102.0,
                "take_profit_2": 103.0,
            }
            record_signal(database_path, analysis)
            record_signal(database_path, analysis)

            resolved = resolve_closed_candles(database_path, "BTCUSDT", "1m", [{
                "open_time": 120_000,
                "open": 100.0,
                "close": 101.0,
            }])
            summary = performance_summary(database_path)

        self.assertEqual(resolved[0]["outcome"], "WIN")
        self.assertEqual(summary["total_signals"], 1)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["directional_win_rate_pct"], 100.0)
        self.assertIn("not realized trade P&L", summary["outcome_basis"])

    def test_unresolved_or_flat_outcomes_do_not_inflate_win_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "signals.sqlite3"
            analysis = {
                "symbol": "EUR/USD",
                "interval": "5m",
                "preference": "ANY",
                "analyzed_candle_open_time": 300_000,
                "signal_for_candle_open_time": 600_000,
                "next_candle_prediction": "DOWN",
                "action": "SHORT",
                "entry": 1.1,
                "stop_loss": 1.11,
                "take_profit_1": 1.08,
                "take_profit_2": 1.07,
            }
            record_signal(database_path, analysis)
            resolved = resolve_closed_candles(database_path, "EUR/USD", "5m", [{
                "open_time": 600_000,
                "open": 1.1,
                "close": 1.1,
            }])
            summary = performance_summary(database_path, "EUR/USD", "5m")

        self.assertEqual(resolved[0]["outcome"], "FLAT")
        self.assertEqual(summary["flat"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertIsNone(summary["directional_win_rate_pct"])


if __name__ == "__main__":
    unittest.main()
