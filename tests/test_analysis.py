import unittest

from app.strategy import analyze_next_candle


def sample_candles(count=240):
    candles = []
    for index in range(count):
        close = 100 + index * 0.05 + (index % 7) * 0.03
        open_price = close - 0.02 if index % 2 else close + 0.02
        candles.append({
            "open": open_price,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 100 + index,
            "close_time": index * 60_000,
        })
    return candles


class NextCandleAnalysisTests(unittest.TestCase):
    def test_analysis_includes_indicators_and_does_not_claim_accuracy(self):
        result = analyze_next_candle(sample_candles(), "BTCUSDT", "1m")

        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertIn(result["next_candle_prediction"], {"UP", "DOWN", "SIDEWAYS"})
        self.assertIn("not calibrated accuracy", result["confidence_note"])
        self.assertIn(result["confidence_level"], {"HIGH", "MEDIUM", "LOW"})
        self.assertEqual(result["forecast_horizon"], "Immediate next 1m candle")
        self.assertTrue(result["brief_rationale"])
        self.assertIn("Trading involves risk", result["risk_disclaimer"])
        self.assertIn("ema_200", result["indicators"])
        self.assertIn("macd_histogram", result["indicators"])
        self.assertIn("stoch_rsi_k", result["indicators"])
        self.assertIn("bollinger_percent", result["indicators"])
        self.assertIn("volume_ratio", result["indicators"])
        self.assertIn("candlestick_pattern", result["indicators"])
        if result["action"] in {"LONG", "SHORT"}:
            self.assertEqual(result["risk_reward_tp1"], 2.0)
            self.assertEqual(result["risk_reward_tp2"], 3.0)
        else:
            self.assertIsNone(result["risk_reward_tp1"])
            self.assertIsNone(result["risk_reward_tp2"])

    def test_risk_reward_ratios_are_absent_when_preference_blocks_trade(self):
        any_direction = analyze_next_candle(sample_candles(), "BTCUSDT", "1m")
        opposing = "SHORT" if any_direction["next_candle_prediction"] == "UP" else "LONG"

        result = analyze_next_candle(sample_candles(), "BTCUSDT", "1m", opposing)

        if any_direction["next_candle_prediction"] in {"UP", "DOWN"}:
            self.assertEqual(result["action"], "WAIT")
            self.assertIsNone(result["risk_reward_tp1"])
            self.assertIsNone(result["risk_reward_tp2"])

    def test_opposing_direction_preference_returns_wait(self):
        any_direction = analyze_next_candle(sample_candles(), "BTCUSDT", "1m")
        requested = "SHORT" if any_direction["next_candle_prediction"] == "UP" else "LONG"

        result = analyze_next_candle(sample_candles(), "BTCUSDT", "1m", requested)

        if any_direction["next_candle_prediction"] in {"UP", "DOWN"}:
            self.assertEqual(result["action"], "WAIT")
            self.assertIsNotNone(result["warning"])
            self.assertIsNone(result["entry"])

    def test_requires_enough_closed_candles_for_ema_200(self):
        with self.assertRaisesRegex(ValueError, "205 closed candles"):
            analyze_next_candle(sample_candles(204), "BTCUSDT", "1m")

    def test_hourly_and_daily_horizons_are_explicit(self):
        hourly = analyze_next_candle(sample_candles(), "BTCUSDT", "1h")
        daily = analyze_next_candle(sample_candles(), "BTCUSDT", "1d")

        self.assertEqual(hourly["forecast_horizon"], "Upcoming 1h trend candle")
        self.assertEqual(daily["forecast_horizon"], "Upcoming 1d trend candle")

    def test_low_volume_blocks_directional_trades(self):
        candles = sample_candles()
        candles[-1]["volume"] = 0.1

        result = analyze_next_candle(candles, "BTCUSDT", "1m")

        self.assertTrue(result["indicators"]["low_volume"])
        self.assertEqual(result["action"], "WAIT")
        self.assertIn("volume", result["warning"].lower())

    def test_choppy_price_action_blocks_directional_trades(self):
        candles = sample_candles()
        for index, candle in enumerate(candles[-30:]):
            close = 111.9 + (0.1 if index % 2 else -0.1)
            candle.update({
                "open": 111.9 - (0.1 if index % 2 else -0.1),
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
            })

        result = analyze_next_candle(candles, "BTCUSDT", "1m")

        self.assertTrue(result["indicators"]["choppy_market"])
        self.assertEqual(result["action"], "WAIT")
        self.assertIn("choppy", result["warning"].lower())


if __name__ == "__main__":
    unittest.main()
