from __future__ import annotations

from statistics import median
from typing import Any


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    changes = [current - previous for previous, current in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value() -> float:
        if average_loss == 0:
            return 50.0 if average_gain == 0 else 100.0
        relative_strength = average_gain / average_loss
        return 100 - 100 / (1 + relative_strength)

    result[period] = value()
    for index in range(period, len(changes)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
        result[index + 1] = value()
    return result


def atr(candles: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(candles)
    if len(candles) < period:
        return result

    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        high = float(candle["high"])
        low = float(candle["low"])
        previous_close = float(candles[index - 1]["close"]) if index else float(candle["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    average = sum(true_ranges[:period]) / period
    result[period - 1] = average
    for index in range(period, len(true_ranges)):
        average = (average * (period - 1) + true_ranges[index]) / period
        result[index] = average
    return result


def build_signal(
    candles: list[dict[str, Any]],
    symbol: str,
    interval: str,
    *,
    fast_period: int = 9,
    slow_period: int = 21,
    rsi_period: int = 14,
    atr_period: int = 14,
    stop_atr_multiple: float = 1.5,
) -> dict[str, Any] | None:
    closes = [float(candle["close"]) for candle in candles]
    if len(closes) < max(slow_period + 1, rsi_period + 2, atr_period + 1):
        return None

    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    rsi_values = rsi(closes, rsi_period)
    atr_values = atr(candles, atr_period)
    current_rsi = rsi_values[-1]
    current_atr = atr_values[-1]
    if current_rsi is None or current_atr is None or current_atr <= 0:
        return None

    action: str | None = None
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1] and 50 < current_rsi < 70:
        action = "BUY"
    elif fast[-2] >= slow[-2] and fast[-1] < slow[-1] and 30 < current_rsi < 50:
        action = "SELL"
    if action is None:
        return None

    entry = closes[-1]
    risk = current_atr * stop_atr_multiple
    if action == "BUY":
        stop_loss, take_profit_1, take_profit_2 = entry - risk, entry + risk * 2, entry + risk * 3
    else:
        stop_loss, take_profit_1, take_profit_2 = entry + risk, entry - risk * 2, entry - risk * 3
    risk_reward_tp1 = round(abs(take_profit_1 - entry) / abs(entry - stop_loss), 2)
    risk_reward_tp2 = round(abs(take_profit_2 - entry) / abs(entry - stop_loss), 2)

    rsi_center = 60 if action == "BUY" else 40
    score = round(60 + max(0, 15 - abs(current_rsi - rsi_center)) + min(10, abs(fast[-1] - slow[-1]) / current_atr * 10))
    return {
        "symbol": symbol,
        "interval": interval,
        "action": action,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward_tp1": risk_reward_tp1,
        "risk_reward_tp2": risk_reward_tp2,
        "rsi": current_rsi,
        "atr": current_atr,
        "setup_score": min(score, 85),
        "candle_close_time": candles[-1]["close_time"],
    }


def _macd(values: list[float]) -> tuple[list[float], list[float], list[float]]:
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal = ema(line, 9)
    histogram = [value - signal_value for value, signal_value in zip(line, signal)]
    return line, signal, histogram


def _stochastic_rsi(values: list[float], period: int = 14) -> tuple[float | None, float | None]:
    rsi_values = rsi(values, period)
    raw: list[float | None] = [None] * len(rsi_values)
    for index in range(period, len(rsi_values)):
        window = [value for value in rsi_values[max(period, index - period + 1):index + 1] if value is not None]
        if len(window) < period:
            continue
        low, high = min(window), max(window)
        raw[index] = 0.0 if high == low else (float(rsi_values[index]) - low) / (high - low) * 100

    def average(items: list[float | None]) -> float | None:
        valid = [value for value in items if value is not None]
        return sum(valid) / len(valid) if len(valid) == len(items) else None

    if len(raw) < 5:
        return None, None
    k_values = [average(raw[index - 2:index + 1]) if index >= 2 else None for index in range(len(raw))]
    return k_values[-1], average(k_values[-3:])


def _candlestick_pattern(candles: list[dict[str, Any]]) -> str:
    current, previous = candles[-1], candles[-2]
    open_price, close = float(current["open"]), float(current["close"])
    high, low = float(current["high"]), float(current["low"])
    body = abs(close - open_price)
    candle_range = high - low
    if candle_range <= 0:
        return "NONE"
    if body <= candle_range * 0.1:
        return "DOJI"

    previous_open, previous_close = float(previous["open"]), float(previous["close"])
    current_bullish = close > open_price
    previous_bullish = previous_close > previous_open
    if current_bullish != previous_bullish:
        if current_bullish and open_price <= previous_close and close >= previous_open:
            return "BULLISH_ENGULFING"
        if not current_bullish and open_price >= previous_close and close <= previous_open:
            return "BEARISH_ENGULFING"

    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low
    if lower_wick >= body * 2 and upper_wick <= body:
        return "BULLISH_PIN_BAR"
    if upper_wick >= body * 2 and lower_wick <= body:
        return "BEARISH_PIN_BAR"
    return "NONE"


def assess_higher_timeframe_trend(candles: list[dict[str, Any]]) -> str:
    if len(candles) < 55:
        return "UNAVAILABLE"
    closes = [float(candle["close"]) for candle in candles]
    macd_histogram = _macd(closes)[2]
    ema_20 = ema(closes, 20)[-1]
    ema_50 = ema(closes, 50)[-1]
    if ema_20 > ema_50 and macd_histogram[-1] > 0:
        return "BULLISH"
    if ema_20 < ema_50 and macd_histogram[-1] < 0:
        return "BEARISH"
    return "SIDEWAYS"


def _smart_money_setups(
    candles: list[dict[str, Any]],
) -> tuple[str, float | None, float | None]:
    highs = [float(candle["high"]) for candle in candles]
    lows = [float(candle["low"]) for candle in candles]
    closes = [float(candle["close"]) for candle in candles]
    current = candles[-1]
    prior_high = max(highs[-21:-1])
    prior_low = min(lows[-21:-1])
    current_close = closes[-1]

    liquidity_grab = "NONE"
    if lows[-1] < prior_low and current_close > prior_low:
        liquidity_grab = "BULLISH_SWEEP"
    elif highs[-1] > prior_high and current_close < prior_high:
        liquidity_grab = "BEARISH_SWEEP"

    bullish_order_block = bearish_order_block = None
    if current_close > prior_high:
        for candle in reversed(candles[-21:-1]):
            if float(candle["close"]) < float(candle["open"]):
                bullish_order_block = float(candle["low"])
                break
    elif current_close < prior_low:
        for candle in reversed(candles[-21:-1]):
            if float(candle["close"]) > float(candle["open"]):
                bearish_order_block = float(candle["high"])
                break
    return liquidity_grab, bullish_order_block, bearish_order_block


def analyze_next_candle(
    candles: list[dict[str, Any]],
    symbol: str,
    interval: str,
    preference: str = "ANY",
) -> dict[str, Any]:
    if preference not in {"ANY", "LONG", "SHORT"}:
        raise ValueError("preference must be ANY, LONG, or SHORT")
    if len(candles) < 205:
        raise ValueError("At least 205 closed candles are required for EMA 200 analysis.")

    closes = [float(candle["close"]) for candle in candles]
    highs = [float(candle["high"]) for candle in candles]
    lows = [float(candle["low"]) for candle in candles]
    current_close = closes[-1]
    current_atr_values = atr(candles, 14)
    current_atr = current_atr_values[-1]
    recent_atr = [value for value in current_atr_values[-101:-1] if value is not None]
    atr_baseline = median(recent_atr) if recent_atr else current_atr
    current_rsi = rsi(closes, 14)[-1]
    if current_atr is None or current_atr <= 0 or current_rsi is None:
        raise ValueError("Insufficient valid candles to calculate indicators.")

    ema_50 = ema(closes, 50)
    ema_200 = ema(closes, 200)
    ema_20 = ema(closes, 20)
    macd_line, macd_signal, macd_histogram = _macd(closes)
    stoch_k, stoch_d = _stochastic_rsi(closes)
    bb_period = 20
    bb_middle = sum(closes[-bb_period:]) / bb_period
    bb_deviation = (sum((value - bb_middle) ** 2 for value in closes[-bb_period:]) / bb_period) ** 0.5
    bb_upper = bb_middle + 2 * bb_deviation
    bb_lower = bb_middle - 2 * bb_deviation
    bb_percent = (current_close - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5

    if ema_50[-1] > ema_200[-1] and macd_histogram[-1] > 0:
        trend = "BULLISH"
    elif ema_50[-1] < ema_200[-1] and macd_histogram[-1] < 0:
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    pattern = _candlestick_pattern(candles)
    liquidity_grab, bullish_order_block, bearish_order_block = _smart_money_setups(candles)
    support = min(lows[-51:-1])
    resistance = max(highs[-51:-1])
    near_support = current_close - support <= current_atr * 0.25
    near_resistance = resistance - current_close <= current_atr * 0.25
    volume_values = [float(candle.get("volume", 0) or 0) for candle in candles[-21:]]
    has_volume = len(volume_values) == 21 and sum(volume_values[:-1]) > 0
    volume_ratio = volume_values[-1] / (sum(volume_values[:-1]) / 20) if has_volume else None
    volume_too_low = bool(has_volume and volume_ratio is not None and volume_ratio < 0.5)
    recent_closes = closes[-21:]
    total_price_travel = sum(
        abs(current - previous) for previous, current in zip(recent_closes, recent_closes[1:])
    )
    directional_efficiency = (
        abs(recent_closes[-1] - recent_closes[0]) / total_price_travel
        if total_price_travel > 0 else 0.0
    )
    choppy_market = directional_efficiency < 0.15

    score = 0.0
    score += 2.0 if ema_50[-1] > ema_200[-1] else -2.0 if ema_50[-1] < ema_200[-1] else 0
    score += 0.5 if ema_20[-1] > ema_50[-1] else -0.5 if ema_20[-1] < ema_50[-1] else 0
    score += 1.5 if macd_histogram[-1] > 0 and macd_histogram[-1] >= macd_histogram[-2] else (
        -1.5 if macd_histogram[-1] < 0 and macd_histogram[-1] <= macd_histogram[-2] else 0
    )
    score += 1.0 if 50 <= current_rsi <= 70 else -1.0 if 30 <= current_rsi < 50 else 0
    if stoch_k is not None and stoch_d is not None:
        score += 0.75 if stoch_k > stoch_d and stoch_k < 80 else (
            -0.75 if stoch_k < stoch_d and stoch_k > 20 else 0
        )
    if bb_percent >= 0.95:
        score += 0.75 if has_volume and volume_ratio is not None and volume_ratio >= 1.5 else -0.5
    elif bb_percent <= 0.05:
        score += -0.75 if has_volume and volume_ratio is not None and volume_ratio >= 1.5 else 0.5
    if has_volume and volume_ratio is not None and volume_ratio >= 1.5:
        score += 0.5 if closes[-1] > closes[-2] else -0.5 if closes[-1] < closes[-2] else 0
    if pattern in {"BULLISH_ENGULFING", "BULLISH_PIN_BAR"}:
        score += 1.0
    elif pattern in {"BEARISH_ENGULFING", "BEARISH_PIN_BAR"}:
        score -= 1.0
    if near_support and closes[-1] > closes[-2]:
        score += 0.5
    elif near_resistance and closes[-1] < closes[-2]:
        score -= 0.5
    if liquidity_grab == "BULLISH_SWEEP":
        score += 1.0
    elif liquidity_grab == "BEARISH_SWEEP":
        score -= 1.0

    max_score = 9.5
    normalized_score = max(-1.0, min(1.0, score / max_score))
    if normalized_score >= 0.2:
        prediction = "UP"
    elif normalized_score <= -0.2:
        prediction = "DOWN"
    else:
        prediction = "SIDEWAYS"
    heuristic_strength = round(50 + abs(normalized_score) * 25)

    expected_action = "LONG" if prediction == "UP" else "SHORT" if prediction == "DOWN" else "WAIT"
    if choppy_market:
        action = "WAIT"
        warning = "Recent price action is choppy; wait for a cleaner directional move."
    elif volume_too_low:
        action = "WAIT"
        warning = "Recent volume is below half its 20-candle average; no trade."
    elif trend == "SIDEWAYS":
        action = "WAIT"
        warning = "Market trend is sideways; wait for a directional setup."
    elif preference != "ANY" and expected_action not in {"WAIT", preference}:
        action = "WAIT"
        warning = f"Requested {preference} bias conflicts with the current {prediction} setup."
    elif expected_action == "WAIT":
        action = "WAIT"
        warning = "Indicators do not show a sufficiently aligned direction; wait for confirmation."
    else:
        action = expected_action
        warning = None

    risk = current_atr * 1.5
    if action == "LONG":
        stop_loss, take_profit_1, take_profit_2 = current_close - risk, current_close + risk * 2, current_close + risk * 3
    elif action == "SHORT":
        stop_loss, take_profit_1, take_profit_2 = current_close + risk, current_close - risk * 2, current_close - risk * 3
    else:
        stop_loss = take_profit_1 = take_profit_2 = None
    risk_reward_tp1 = risk_reward_tp2 = None
    if action != "WAIT":
        risk_reward_tp1 = round(abs(take_profit_1 - current_close) / abs(current_close - stop_loss), 2)
        risk_reward_tp2 = round(abs(take_profit_2 - current_close) / abs(current_close - stop_loss), 2)

    if action == "WAIT":
        confidence_level = "LOW"
        brief_rationale = warning or "No sufficiently aligned directional setup."
    else:
        confidence_level = (
            "HIGH" if heuristic_strength >= 68
            else "MEDIUM" if heuristic_strength >= 58
            else "LOW"
        )
        direction = "bullish" if prediction == "UP" else "bearish"
        brief_rationale = (
            f"{trend.title()} trend context; indicator confluence leans {direction}"
            f" ({pattern.replace('_', ' ').lower()} candle pattern)."
        )
        if near_support and prediction == "UP":
            brief_rationale += " Price is near recent support."
        elif near_resistance and prediction == "DOWN":
            brief_rationale += " Price is near recent resistance."

    interval_minutes = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
        "1w": 10080,
    }.get(interval)
    if interval_minutes is None:
        raise ValueError(f"Unsupported interval: {interval}")
    if interval_minutes <= 5:
        forecast_horizon = f"Immediate next {interval} candle"
    elif interval_minutes >= 60:
        forecast_horizon = f"Upcoming {interval} trend candle"
    else:
        forecast_horizon = f"Next {interval} candle"

    return {
        "symbol": symbol,
        "interval": interval,
        "forecast_horizon": forecast_horizon,
        "current_trend": trend,
        "next_candle_prediction": prediction,
        "heuristic_strength_pct": heuristic_strength,
        "confidence_level": confidence_level,
        "confidence_note": "Qualitative band from a heuristic score; not calibrated accuracy or a probability of profit.",
        "brief_rationale": brief_rationale,
        "risk_disclaimer": "Trading involves risk. Use proper risk management.",
        "preference": preference,
        "action": action,
        "warning": warning,
        "entry": current_close if action != "WAIT" else None,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward_tp1": risk_reward_tp1,
        "risk_reward_tp2": risk_reward_tp2,
        "indicators": {
            "ema_20": ema_20[-1],
            "ema_50": ema_50[-1],
            "ema_200": ema_200[-1],
            "macd": macd_line[-1],
            "macd_signal": macd_signal[-1],
            "macd_histogram": macd_histogram[-1],
            "rsi_14": current_rsi,
            "stoch_rsi_k": stoch_k,
            "stoch_rsi_d": stoch_d,
            "bollinger_upper": bb_upper,
            "bollinger_middle": bb_middle,
            "bollinger_lower": bb_lower,
            "bollinger_percent": bb_percent,
            "bollinger_bandwidth": (bb_upper - bb_lower) / bb_middle if bb_middle else 0,
            "atr_14": current_atr,
            "atr_percent": current_atr / current_close * 100,
            "atr_vs_recent_median": current_atr / atr_baseline if atr_baseline else 1.0,
            "volatility_spike": bool(atr_baseline and current_atr >= atr_baseline * 2.5),
            "volume_ratio": volume_ratio,
            "volume_available": has_volume,
            "low_volume": volume_too_low,
            "directional_efficiency_20": directional_efficiency,
            "choppy_market": choppy_market,
            "support_50": support,
            "resistance_50": resistance,
            "trendline_slope_20": (
                sum((index - 9.5) * value for index, value in enumerate(closes[-20:])) / 665
            ),
            "candlestick_pattern": pattern,
            "liquidity_grab": liquidity_grab,
            "bullish_order_block_candidate": bullish_order_block,
            "bearish_order_block_candidate": bearish_order_block,
        },
        "candle_close_time": candles[-1]["close_time"],
    }
