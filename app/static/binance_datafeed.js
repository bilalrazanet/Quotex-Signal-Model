(function () {
  "use strict";

  const INTERVAL_BY_RESOLUTION = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "1D": "1d",
    "1W": "1w",
    "1M": "1M"
  };

  class BinanceDatafeed {
    constructor(symbols, intervals, binanceSymbols = symbols.filter((symbol) => symbol.endsWith("USDT"))) {
      this.symbols = symbols;
      this.intervals = intervals;
      this.binanceSymbols = binanceSymbols;
      this.subscribers = new Map();
      this.supportedResolutions = Object.entries(INTERVAL_BY_RESOLUTION)
        .filter(([, interval]) => this.intervals.includes(interval))
        .map(([resolution]) => resolution);
      this.supportedResolutions = [...new Set(this.supportedResolutions)];
    }

    onReady(callback) {
      setTimeout(() => callback({
        supported_resolutions: this.supportedResolutions,
        supports_search: true,
        supports_group_request: false,
        supports_marks: false,
        supports_timescale_marks: false,
        supports_time: false
      }), 0);
    }

    searchSymbols(userInput, _exchange, _symbolType, callback) {
      const query = userInput.toUpperCase();
      callback(this.symbols
        .filter((symbol) => symbol.includes(query))
        .map((symbol) => ({
          symbol,
          full_name: `${this.binanceSymbols.includes(symbol) ? "BINANCE" : "TWELVEDATA"}:${symbol}`,
          description: `${symbol} Spot`,
          exchange: this.binanceSymbols.includes(symbol) ? "BINANCE" : "TWELVEDATA",
          ticker: symbol,
          type: "crypto"
        })));
    }

    resolveSymbol(symbolName, onResolve, onError) {
      const symbol = symbolName.split(":").pop().toUpperCase();
      if (!this.symbols.includes(symbol)) {
        onError(`Symbol ${symbol} is not enabled by the market-data server.`);
        return;
      }
      const exchange = this.binanceSymbols.includes(symbol) ? "BINANCE" : "TWELVEDATA";
      const instrumentType = symbol.includes("/")
        ? (/^(XAU|XAG|WTI|BRENT)\//.test(symbol) ? "commodity" : "forex")
        : (this.binanceSymbols.includes(symbol) ? "crypto" : "stock");
      setTimeout(() => onResolve({
        name: symbol,
        ticker: symbol,
        description: `${symbol} ${instrumentType === "crypto" ? "Spot" : ""}`.trim(),
        type: instrumentType,
        session: "24x7",
        timezone: "Etc/UTC",
        exchange,
        listed_exchange: exchange,
        minmov: 1,
        pricescale: 100000000,
        has_intraday: true,
        has_daily: this.intervals.includes("1d"),
        has_weekly_and_monthly: this.intervals.includes("1w") || this.intervals.includes("1M"),
        supported_resolutions: this.supportedResolutions,
        volume_precision: 8,
        data_status: "streaming"
      }), 0);
    }

    async getBars(symbolInfo, resolution, periodParams, onHistory, onError) {
      const interval = INTERVAL_BY_RESOLUTION[resolution];
      if (!interval || !this.intervals.includes(interval)) {
        onError(`Resolution ${resolution} is not enabled by the market-data server.`);
        return;
      }
      const query = new URLSearchParams({
        symbol: symbolInfo.ticker,
        interval,
        from: String(periodParams.from),
        to: String(periodParams.to),
        countBack: String(Math.min(periodParams.countBack || 500, 1000))
      });
      try {
        const response = await fetch(`/api/chart/history?${query}`);
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || `History request failed (${response.status}).`);
        const bars = result.bars.map((bar) => ({
          time: bar.time,
          open: bar.open,
          high: bar.high,
          low: bar.low,
          close: bar.close,
          volume: bar.volume
        }));
        onHistory(bars, {
          noData: result.no_data,
          nextTime: bars.length ? bars[0].time : undefined
        });
      } catch (error) {
        onError(error.message || "Chart history request failed.");
      }
    }

    subscribeBars(symbolInfo, resolution, onRealtime, subscriberUid, onResetCache) {
      this.subscribers.set(subscriberUid, {
        symbol: symbolInfo.ticker,
        interval: INTERVAL_BY_RESOLUTION[resolution],
        onRealtime,
        onResetCache
      });
    }

    unsubscribeBars(subscriberUid) {
      this.subscribers.delete(subscriberUid);
    }

    updateFromStream(candle) {
      for (const subscriber of this.subscribers.values()) {
        if (subscriber.symbol !== candle.symbol || subscriber.interval !== candle.interval) continue;
        subscriber.onRealtime({
          time: candle.open_time,
          open: candle.open,
          high: candle.high,
          low: candle.low,
          close: candle.close,
          volume: candle.volume
        });
      }
    }

    resetSubscriptions() {
      for (const subscriber of this.subscribers.values()) subscriber.onResetCache();
    }
  }

  window.BinanceDatafeed = BinanceDatafeed;
})();
