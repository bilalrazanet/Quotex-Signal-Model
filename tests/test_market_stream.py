import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.main import MarketData, websocket_reconnect_delay


class RecordingClient:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class FailingSocket:
    async def recv(self):
        raise OSError("connection reset")


class FakeSocketContext:
    async def __aenter__(self):
        return FailingSocket()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class MarketStreamTests(unittest.IsolatedAsyncioTestCase):
    def test_reconnect_delay_is_exponential_bounded_and_jittered(self):
        with patch("app.main.random.uniform", return_value=1.0):
            self.assertEqual(websocket_reconnect_delay(1), 1.0)
            self.assertEqual(websocket_reconnect_delay(2), 2.0)
            self.assertEqual(websocket_reconnect_delay(3), 4.0)
            self.assertEqual(websocket_reconnect_delay(20), 30.0)

    async def test_stream_failure_publishes_reconnecting_state_and_retries(self):
        market = MarketData()
        context = FakeSocketContext()
        with (
            patch.object(market, "load_history", new_callable=AsyncMock),
            patch("app.main.websockets.connect", return_value=context) as connect,
            patch("app.main.websocket_reconnect_delay", return_value=1.0),
            patch("app.main.asyncio.sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError),
            self.assertLogs("market-board", level="ERROR"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await market.run()

        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.kwargs["ping_interval"], 10)
        self.assertEqual(market.feed_state, "reconnecting")
        self.assertEqual(market.reconnect_attempt, 1)

    async def test_short_lived_connections_keep_increasing_backoff(self):
        market = MarketData()
        delays = []

        async def cancel_after_three_retries(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        with (
            patch.object(market, "load_history", new_callable=AsyncMock),
            patch("app.main.websockets.connect", side_effect=lambda *args, **kwargs: FakeSocketContext()) as connect,
            patch("app.main.websocket_reconnect_delay", side_effect=lambda attempt: attempt) as delay_for,
            patch("app.main.asyncio.sleep", new_callable=AsyncMock, side_effect=cancel_after_three_retries),
            self.assertLogs("market-board", level="ERROR"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await market.run()

        self.assertEqual(connect.call_count, 3)
        self.assertEqual([call.args[0] for call in delay_for.call_args_list], [1, 2, 3])
        self.assertEqual(delays, [1, 2, 3])
        self.assertEqual(market.reconnect_attempt, 3)

    async def test_out_of_order_closed_candle_is_ignored(self):
        market = MarketData()
        client = RecordingClient()
        market.clients.add(client)
        market.candles[market.key("BTCUSDT", "1m")].append({
            "open_time": 2_000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 5.0,
            "close_time": 61_999,
        })
        message = {
            "data": {
                "k": {
                    "s": "BTCUSDT", "i": "1m", "t": 1_000, "T": 60_999,
                    "o": "100", "h": "101", "l": "99", "c": "100", "v": "5", "x": True,
                },
            },
        }

        with self.assertLogs("market-board", level="WARNING"):
            await market.handle_message(message)

        self.assertFalse(client.messages)
        market.clients.clear()

    async def test_malformed_kline_is_logged_and_ignored(self):
        market = MarketData()
        client = RecordingClient()
        market.clients.add(client)

        with self.assertLogs("market-board", level="WARNING"):
            await market.handle_message({"data": {"k": {"s": "BTCUSDT", "i": "1m", "c": "NaN"}}})

        self.assertEqual(client.messages, [])
        self.assertEqual(market.prices, {})
        market.clients.clear()

    async def test_live_candle_updates_are_broadcast_without_entering_closed_history(self):
        market = MarketData()
        client = RecordingClient()
        market.clients.add(client)
        message = {
            "data": {
                "E": 1_700_000_030_000,
                "k": {
                    "s": "BTCUSDT",
                    "i": "1m",
                    "t": 1_700_000_000_000,
                    "T": 1_700_000_059_999,
                    "o": "100.0",
                    "h": "102.0",
                    "l": "99.0",
                    "c": "101.0",
                    "v": "5.0",
                    "x": False,
                },
            },
        }

        await market.handle_message(message)

        candle_events = [item for item in client.messages if item["type"] == "candle"]
        self.assertEqual(len(candle_events), 1)
        self.assertEqual(candle_events[0]["data"]["close"], 101.0)
        self.assertFalse(market.candles[market.key("BTCUSDT", "1m")])
        market.clients.clear()


if __name__ == "__main__":
    unittest.main()
