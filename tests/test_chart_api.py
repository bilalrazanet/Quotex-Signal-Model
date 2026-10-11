import unittest
import time
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport, AsyncClient, HTTPStatusError, Request, Response

from app.main import ANALYSIS_CACHE, app, calculate_position_sizing, fetch_binance_json


class FakeBinanceResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return [[
            1_700_000_000_000,
            "100.0",
            "105.0",
            "99.0",
            "104.0",
            "12.5",
            1_700_000_059_999,
        ]]


class FakeAsyncClient:
    last_params = None

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def get(self, url, params):
        type(self).last_params = params
        return FakeBinanceResponse()


class SequenceBinanceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = 0

    async def get(self, url, params):
        self.requests += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, HTTPStatusError):
            raise response
        return response


def http_status_error(status_code, headers=None):
    request = Request("GET", "https://api.binance.com/api/v3/klines")
    response = Response(status_code, headers=headers, request=request)
    return HTTPStatusError("temporary exchange response", request=request, response=response)


class SuccessfulJsonResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"ok": True}]


class FakeTwelveDataResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {
            "meta": {"symbol": "XAU/USD", "interval": "1min"},
            "values": [{
                "datetime": "2024-01-01 00:00:00",
                "open": "2060.0",
                "high": "2061.0",
                "low": "2059.0",
                "close": "2060.5",
                "volume": None,
            }],
        }


class FakeTwelveDataClient(FakeAsyncClient):
    last_headers = None

    async def get(self, url, params, headers):
        type(self).last_params = params
        type(self).last_headers = headers
        return FakeTwelveDataResponse()


class FakeAnalysisResponse:
    def __init__(self):
        now = int(time.time() * 1000)
        current_minute = now // 60_000 * 60_000
        start = current_minute - 240 * 60_000
        self.rows = []
        for index in range(240):
            close = 100 + index * 0.05 + (index % 7) * 0.03
            self.rows.append([
                start + index * 60_000,
                str(close - 0.02),
                str(close + 0.5),
                str(close - 0.5),
                str(close),
                str(100 + index),
                start + index * 60_000 + 59_999,
            ])

    def raise_for_status(self):
        pass

    def json(self):
        return self.rows


class FakeFinnhubResponse:
    def __init__(self, events=None):
        self.events = events or []

    def raise_for_status(self):
        pass

    def json(self):
        return {"economicCalendar": self.events}


class FakeAnalysisClient(FakeAsyncClient):
    async def get(self, url, params):
        if "calendar/economic" in url:
            return FakeFinnhubResponse(type(self).news_events)
        return FakeAnalysisResponse()

    news_events = []


class ChartHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ANALYSIS_CACHE.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_patcher = patch(
            "app.main.SIGNAL_DB_PATH",
            Path(self.temp_dir.name) / "signals.sqlite3",
        )
        self.database_patcher.start()
        FakeAnalysisClient.news_events = []
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.database_patcher.stop()
        self.temp_dir.cleanup()

    async def test_history_returns_binance_ohlcv_in_chart_format(self):
        with patch("app.main.httpx.AsyncClient", FakeAsyncClient):
            response = await self.client.get(
                "/api/chart/history",
                params={"symbol": "BTCUSDT", "interval": "1m", "from": 1_699_999_999, "to": 1_700_000_060},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bars"], [{
            "time": 1_700_000_000_000,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 12.5,
        }])
        self.assertEqual(FakeAsyncClient.last_params["symbol"], "BTCUSDT")
        self.assertEqual(FakeAsyncClient.last_params["interval"], "1m")

    async def test_binance_rest_rate_limit_respects_retry_after(self):
        client = SequenceBinanceClient([
            http_status_error(429, {"Retry-After": "3"}),
            SuccessfulJsonResponse(),
        ])
        with patch("app.main.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await fetch_binance_json(client, {"symbol": "BTCUSDT"})

        self.assertEqual(result, [{"ok": True}])
        self.assertEqual(client.requests, 2)
        sleep.assert_awaited_once_with(3.0)

    async def test_binance_rest_timeout_retries_with_backoff(self):
        client = SequenceBinanceClient([
            httpx.ConnectTimeout("request timed out"),
            SuccessfulJsonResponse(),
        ])
        with (
            patch("app.main.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("app.main.websocket_reconnect_delay", return_value=1.25),
        ):
            result = await fetch_binance_json(client, {"symbol": "BTCUSDT"})

        self.assertEqual(result, [{"ok": True}])
        self.assertEqual(client.requests, 2)
        sleep.assert_awaited_once_with(1.25)

    async def test_history_rejects_unconfigured_symbol_and_interval(self):
        symbol_response = await self.client.get(
            "/api/chart/history",
            params={"symbol": "EURUSD", "interval": "1m", "from": 1, "to": 2},
        )
        interval_response = await self.client.get(
            "/api/chart/history",
            params={"symbol": "BTCUSDT", "interval": "2m", "from": 1, "to": 2},
        )

        self.assertEqual(symbol_response.status_code, 400)
        self.assertEqual(interval_response.status_code, 400)

    async def test_twelve_data_history_supports_gold_and_maps_candle_fields(self):
        with (
            patch("app.main.TWELVE_DATA_API_KEY", "test-key"),
            patch("app.main.httpx.AsyncClient", FakeTwelveDataClient),
        ):
            response = await self.client.get(
                "/api/chart/history",
                params={"symbol": "XAU/USD", "interval": "1m", "from": 1, "to": 2_000_000_000, "countBack": 10},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bars"][0]["open"], 2060.0)
        self.assertEqual(response.json()["bars"][0]["volume"], 0)
        self.assertEqual(FakeTwelveDataClient.last_params["symbol"], "XAU/USD")
        self.assertEqual(FakeTwelveDataClient.last_params["interval"], "1min")
        self.assertEqual(FakeTwelveDataClient.last_headers["Authorization"], "apikey test-key")
        self.assertEqual(FakeTwelveDataClient.last_params["timezone"], "UTC")
        self.assertIn("start_date", FakeTwelveDataClient.last_params)
        self.assertIn("end_date", FakeTwelveDataClient.last_params)

    async def test_non_crypto_market_requires_twelve_data_key(self):
        with patch("app.main.TWELVE_DATA_API_KEY", ""):
            response = await self.client.get(
                "/api/chart/history",
                params={"symbol": "XAU/USD", "interval": "1m", "from": 1, "to": 2_000_000_000},
            )

        self.assertEqual(response.status_code, 503)

    async def test_analysis_endpoint_returns_closed_candle_indicators(self):
        with (
            patch("app.main.FINNHUB_API_KEY", "test-key"),
            patch("app.main.httpx.AsyncClient", FakeAnalysisClient),
        ):
            response = await self.client.get(
                "/api/analyze",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "preference": "ANY",
                    "account_balance": 10_000,
                    "risk_percent": 1.5,
                },
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn(result["next_candle_prediction"], {"UP", "DOWN", "SIDEWAYS"})
        self.assertIn("ema_200", result["indicators"])
        self.assertIn("latest_candle", result)
        self.assertIn(result["confidence_level"], {"HIGH", "MEDIUM", "LOW"})
        self.assertTrue(result["brief_rationale"])
        self.assertIn("Trading involves risk", result["risk_disclaimer"])
        self.assertEqual(result["latest_candle"]["symbol"], "BTCUSDT")
        self.assertEqual(result["news_filter"]["status"], "clear")
        self.assertIn("1h", result["higher_timeframe_trends"])
        self.assertIn("15m", result["higher_timeframe_trends"])
        self.assertEqual(result["signal_hold_time"], "1m")
        expected_risk_amount = 150 if result["action"] in {"LONG", "SHORT"} else 0
        self.assertEqual(result["position_sizing"]["max_risk_amount"], expected_risk_amount)
        self.assertEqual(
            result["signal_for_candle_open_time"],
            result["analyzed_candle_open_time"] + 60_000,
        )
        if result["action"] in {"LONG", "SHORT"}:
            self.assertEqual(result["risk_reward_tp1"], 2.0)
            self.assertEqual(result["risk_reward_tp2"], 3.0)

    async def test_repeated_signal_request_returns_cached_result_for_same_closed_candle(self):
        with (
            patch("app.main.FINNHUB_API_KEY", "test-key"),
            patch("app.main.httpx.AsyncClient", FakeAnalysisClient),
        ):
            first = await self.client.get(
                "/api/analyze",
                params={"symbol": "BTCUSDT", "interval": "1m", "preference": "ANY"},
            )
            second = await self.client.get(
                "/api/analyze",
                params={"symbol": "BTCUSDT", "interval": "1m", "preference": "ANY"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())

    async def test_high_impact_news_blocks_signal(self):
        event_time = datetime.now(timezone.utc) + timedelta(minutes=5)
        FakeAnalysisClient.news_events = [{
            "country": "US",
            "event": "Policy announcement",
            "impact": "high",
            "time": event_time.isoformat(),
        }]
        with (
            patch("app.main.FINNHUB_API_KEY", "test-key"),
            patch("app.main.httpx.AsyncClient", FakeAnalysisClient),
        ):
            response = await self.client.get(
                "/api/analyze",
                params={"symbol": "BTCUSDT", "interval": "1m", "preference": "ANY"},
            )

        result = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["news_filter"]["status"], "blackout")
        self.assertEqual(result["action"], "WAIT")
        self.assertIsNone(result["entry"])

    async def test_missing_news_key_fails_closed(self):
        with (
            patch("app.main.FINNHUB_API_KEY", ""),
            patch("app.main.httpx.AsyncClient", FakeAnalysisClient),
        ):
            response = await self.client.get(
                "/api/analyze",
                params={"symbol": "BTCUSDT", "interval": "1m", "preference": "ANY"},
            )

        result = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["news_filter"]["status"], "unavailable")
        self.assertEqual(result["action"], "WAIT")

    def test_position_size_uses_risk_budget_and_rejects_unconverted_quote_currency(self):
        analysis = {"action": "LONG", "entry": 110.0, "stop_loss": 100.0}

        usd_quote = calculate_position_sizing("EUR/USD", analysis, 10_000, 1)
        jpy_quote = calculate_position_sizing("EUR/JPY", analysis, 10_000, 1)

        self.assertEqual(usd_quote["max_risk_amount"], 100)
        self.assertEqual(usd_quote["estimated_units"], 10)
        self.assertEqual(usd_quote["unit_label"], "EUR units")
        self.assertIsNone(jpy_quote["estimated_units"])
        self.assertIn("JPY", jpy_quote["message"])

    async def test_chart_library_status_reports_not_installed(self):
        response = await self.client.get("/api/chart-library/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("available", response.json())

    async def test_performance_endpoint_returns_empty_ledger_summary(self):
        response = await self.client.get("/api/performance")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_signals"], 0)
        self.assertIsNone(response.json()["directional_win_rate_pct"])


if __name__ == "__main__":
    unittest.main()
