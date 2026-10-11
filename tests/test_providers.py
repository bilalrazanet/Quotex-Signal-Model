import unittest

from app.providers import TWELVE_DATA_INTERVALS, parse_twelve_data_candles


class TwelveDataParsingTests(unittest.TestCase):
    def test_maps_provider_interval_names(self):
        self.assertEqual(TWELVE_DATA_INTERVALS["1m"], "1min")
        self.assertEqual(TWELVE_DATA_INTERVALS["1d"], "1day")

    def test_parses_utc_candles_and_missing_volume(self):
        candles = parse_twelve_data_candles(
            {
                "values": [
                    {
                        "datetime": "2024-01-01 00:01:00",
                        "open": "2",
                        "high": "3",
                        "low": "1",
                        "close": "2.5",
                    },
                    {
                        "datetime": "2024-01-01 00:00:00",
                        "open": "1",
                        "high": "2",
                        "low": "0.5",
                        "close": "1.5",
                        "volume": None,
                    },
                ]
            },
            from_time_ms=0,
            to_time_ms=1_800_000_000_000,
        )

        self.assertEqual([item["open"] for item in candles], [1, 2])
        self.assertEqual(candles[0]["volume"], 0)

    def test_provider_error_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "invalid API key"):
            parse_twelve_data_candles(
                {"status": "error", "message": "invalid API key"},
                from_time_ms=0,
                to_time_ms=10,
            )


if __name__ == "__main__":
    unittest.main()
