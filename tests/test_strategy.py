import unittest

from app.strategy import atr, build_signal, ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_tracks_values(self):
        self.assertEqual(ema([1, 2, 3], 2), [1, 1 + 2 / 3, 1 + 2 / 3 + (3 - (1 + 2 / 3)) * 2 / 3])

    def test_rsi_is_100_for_only_gains(self):
        self.assertEqual(rsi([1, 2, 3, 4], 2)[-1], 100)

    def test_atr_uses_true_range(self):
        candles = [
            {"high": 11, "low": 9, "close": 10},
            {"high": 14, "low": 12, "close": 13},
        ]
        self.assertEqual(atr(candles, 2)[-1], 3.0)

    def test_buy_signal_sets_atr_based_risk_levels(self):
        closes = [
            100.0, 99.3229503063, 99.1455300425, 98.9136661498, 99.3476638988,
            99.8159541854, 100.264189536, 100.7403972108, 100.2430822565,
            99.578686071, 99.2291782418, 98.538673481, 97.9812839736,
            97.9739016791, 97.5150062114, 97.5478909114, 97.3046556348,
            96.9335464564, 97.3967128681, 96.9202349975, 97.2790691575,
            97.2112637975, 97.3068223094, 96.779630258, 96.9735779197,
            97.3410591023, 97.0287787124, 97.0461173664, 97.2800687657,
            97.2997083305, 97.6521083309, 97.8752812127, 97.7020866611,
            97.9693739557, 98.3470783126, 98.3531607167,
        ]
        candles = [
            {"high": close + 0.5, "low": close - 0.5, "close": close, "close_time": index}
            for index, close in enumerate(closes)
        ]

        signal = build_signal(candles, "TEST", "1m")

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["action"], "BUY")
        self.assertAlmostEqual(signal["entry"] - signal["stop_loss"], signal["atr"] * 1.5)
        self.assertAlmostEqual(signal["take_profit_1"] - signal["entry"], signal["atr"] * 3)
        self.assertAlmostEqual(signal["take_profit_2"] - signal["entry"], signal["atr"] * 4.5)
        self.assertEqual(signal["risk_reward_tp1"], 2.0)
        self.assertEqual(signal["risk_reward_tp2"], 3.0)


if __name__ == "__main__":
    unittest.main()
