# Live Market Signal Board

Signal dashboard for Binance crypto and optional Twelve Data Forex, metals, and equities. It evaluates only closed candles and produces a next-candle directional heuristic; it does not place orders.

## Features

- Binance Spot REST history and live WebSocket klines.
- Optional Twelve Data history for major/cross FX pairs, XAU/USD, XAG/USD, WTI/Brent, selected US stocks, and index-tracking ETFs; Binance crypto symbols are configurable.
- Chart history for 1m, 5m, 15m, 30m, 1h, 4h, and 1d.
- Closed-candle analysis: EMA 20/50/200, MACD, RSI(14), Stochastic RSI, Bollinger Bands, ATR(14), available volume, support/resistance, trendline slope, and simple candlestick patterns.
- Long/short preference checks; an opposing preference returns WAIT with a warning.
- Higher-timeframe trend confirmation, high-impact Finnhub economic-news blackout, sideways/range and abnormal-volatility no-trade filters.
- Binance WebSocket protocol pings every 10 seconds, stale-feed heartbeat visibility, jittered exponential reconnects, and REST candle backfill after reconnects.
- Directional-efficiency chop and relative low-volume filters; unavailable volume is reported instead of fabricated.
- Heuristic liquidity-sweep and order-block candidates from OHLCV candles; these are price-action proxies, not claims about institutional orders.
- Signals are generated only when requested; the technical analysis is cached for the latest closed candle, while live safety filters are rechecked on every request and can downgrade a cached direction to WAIT.
- Signal response includes the next candle's open time and hold timeframe.
- Responses include a short-term next-candle or longer-timeframe forecast horizon, a qualitative confidence band, a brief indicator rationale, and a risk disclaimer; confidence is not calibrated accuracy or a probability of profit.
- SQLite-backed next-candle direction outcomes, win rate, and invalidation alert once the forecast candle closes.
- ATR-based entry and stop loss, with targets at 2R and 3R when a directional setup is supported.
- Risk-percent and optional balance-based position-size estimate for USD- and USD-stablecoin-quoted linear instruments.
- Risk/reward ratios for each take-profit target in the analysis response and dashboard.
- TradingView Advanced Charts custom datafeed when its licensed assets are present, with a Lightweight Charts fallback.

## Run locally

Requires Python 3.10 or newer and market-data network access.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload --env-file .env
```

Open <http://127.0.0.1:8000>. Set `BINANCE_SYMBOLS`, `TWELVE_DATA_SYMBOLS`, and `INTERVALS` in `.env`. Configure `TWELVE_DATA_API_KEY` in `.env` to enable FX, metals, oil, and equity history, and `FINNHUB_API_KEY` to enable the high-impact economic calendar; do not commit real API keys. The news filter fails closed: if the calendar is missing or cannot be verified, signal action is WAIT. Twelve Data symbols use provider notation such as `EUR/USD`, `XAU/USD`, `XAG/USD`, `WTI/USD`, `AAPL`, and `SPY`.

### Run from VS Code

Open the project folder in VS Code, select a Python interpreter with the project requirements installed, and create `.env` from `.env.example` if needed. Run **Tasks: Run Task** and select **Run Live Market Signal Board**. The task uses the interpreter selected by the Python extension and serves the dashboard at <http://127.0.0.1:8000>.

Twelve Data entitlements, latency, and API quotas depend on the account plan. Analysis is fetched only when the user requests a signal, not for the entire symbol universe. Missing volume is reported as unavailable rather than fabricated. SPY/QQQ are index-tracking ETFs, not the S&P 500/Nasdaq indexes themselves.

To enable TradingView Advanced Charts, install the licensed package so that `charting_library.standalone.js` is at `app/static/charting_library/charting_library.standalone.js`, preserving the package's relative assets. The package is not included in this repository. If it is absent, the app uses Lightweight Charts from its public CDN and displays candles from the selected instrument's market data.

## API

- `GET /api/snapshot` — configured symbols/timeframes, recent Binance prices, provider configuration, and Binance feed state (`starting`, `connecting`, `connected`, `stale`, `reconnecting`, or `unavailable`), last event time, and retry count.
- `GET /api/chart/history?symbol=BTCUSDT&interval=1m&from=<unix-seconds>&to=<unix-seconds>&countBack=500` — OHLCV history formatted for the chart. Symbol can be a configured Binance or Twelve Data instrument such as `XAU/USD`.
- `GET /api/analyze?symbol=BTCUSDT&interval=1m&preference=ANY` — next-candle direction, indicators, entry, stop, targets, and numeric `risk_reward_tp1`/`risk_reward_tp2` ratios. Preference accepts `ANY`, `LONG`, or `SHORT`.
- Add optional `account_balance=<amount>&risk_percent=1` to `/api/analyze` to receive an estimated position size; risk percent is limited to 0.1–2%. Non-USD/stablecoin quote currencies return no unit estimate unless converted.
- `GET /api/performance` — persisted next-candle directional outcomes and win rate; optional `symbol`/`interval` filters.
- `WS /ws` — live Binance price/candle and signal events.

The dashboard generates an analysis only after the user presses **Start Get A Signal**; changing the chart selection does not issue a signal. For the same symbol, timeframe, preference, balance, risk setting, and latest closed candle, the technical analysis is cached; higher-timeframe and news safety checks are refreshed and may block a previously allowed trade. The analyzer requires at least 205 closed candles for EMA 200. Low relative volume and low directional efficiency block a trade when volume data is available; these filters reduce exposure to some poor conditions but do not prove higher accuracy. It returns a heuristic strength score, not calibrated accuracy, a probability of profit, or a guarantee; no 90% hit rate can be promised. The performance page scores only whether the following candle closed in the predicted direction; it does not calculate realized P&L, and it is not self-learning. Position sizing is an estimate and excludes broker contract rules, conversion rates, margin, fees, and slippage. Historical testing, out-of-sample validation, and paper trading with fees, spreads, slippage, outages, and provider limits are necessary before considering live execution. This project is a market-data and signal dashboard: it contains no exchange order placement, signed order requests, position tracking, or active trailing-stop manager, and therefore cannot verify trading credentials or execute buys/sells.

## Test

```powershell
python -m unittest discover -s tests
```
