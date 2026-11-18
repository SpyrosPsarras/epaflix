#requirements:
#finviz==2.1.0
#requests==2.34.2

"""Two-strategy screener: Pullback / EMA bounce and Breakout / momentum.

Finviz finds the candidates. Daily OHLCV comes from the Finviz chart endpoint
(split adjusted, same host as the screener, no API key). Every indicator is
computed here from completed daily bars only, so a signal on day t uses no data
after day t. The script sends no email and changes nothing; it returns JSON.
"""

import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from finviz.screener import NoResults, Screener

FINVIZ_HOSTS = {"finviz.com", "www.finviz.com"}
CHART_URL = "https://finviz.com/api/quote"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
STRATEGIES = ("pullback", "breakout")
# A daily bar counts as final this long after the 16:00 New York close (Finviz free data is delayed).
SESSION_FINAL_AFTER = (16, 20)
MAX_TICKERS = 200
MAX_VALIDATION_DAYS = 750

DEFAULTS = {
    "benchmarks": ["QQQ", "SMH"],
    "min_bars": 260,
    "min_price": 5.0,
    "min_dollar_volume": 10_000_000.0,
    "max_missing_sessions": 2,
    "max_zero_volume_days": 2,
    "rs_lookback": 63,
    "max_risk_atr": 2.5,
    # Pullback / EMA bounce
    "pullback_min_atr": 1.0,
    "touch_atr": 0.25,
    "reversal_close_location": 0.5,
    "pullback_stop_atr": 0.2,
    "pullback_min_rr": 1.5,
    # Breakout / momentum
    "breakout_lookback": 50,
    "min_base_bars": 5,
    "min_touches": 2,
    "resistance_touch_atr": 0.5,
    "max_base_depth": 0.30,
    "max_extension_atr": 1.0,
    "min_rel_volume": 1.5,
    "breakout_stop_atr": 0.5,
    "breakout_min_rr": 2.0,
    # Validation
    "validation_horizon": 20,
    "request_pause": 0.4,
}


# ---------------------------------------------------------------- data


class Bars:
    """Daily bars, oldest first."""

    def __init__(self, ticker, dates, open_, high, low, close, volume):
        self.ticker = ticker
        self.dates, self.open, self.high, self.low, self.close, self.volume = dates, open_, high, low, close, volume

    def __len__(self):
        return len(self.dates)

    def head(self, n):
        return Bars(self.ticker, self.dates[:n], self.open[:n], self.high[:n], self.low[:n], self.close[:n], self.volume[:n])


def parse_chart(payload, ticker):
    if not isinstance(payload, dict) or payload.get("ticker", "").upper() != ticker:
        raise ValueError("chart data is for another ticker")
    columns = [payload.get(k) for k in ("date", "open", "high", "low", "close", "volume")]
    if not all(isinstance(column, list) for column in columns) or len({len(column) for column in columns}) != 1:
        raise ValueError("chart data has missing or uneven columns")
    stamps, open_, high, low, close, volume = columns
    # Finviz stamps each daily bar during that session (13:00 or 14:00 UTC).
    dates = [datetime.fromtimestamp(stamp, timezone.utc).date() for stamp in stamps]
    if any(later <= earlier for earlier, later in zip(dates, dates[1:])):
        raise ValueError("chart dates are not strictly increasing")
    numbers = [[float(x) if x is not None else math.nan for x in column] for column in (open_, high, low, close, volume)]
    return Bars(ticker, dates, *numbers)


def fetch_bars(ticker, count, retries=3):
    for attempt in range(retries + 1):
        response = requests.get(
            CHART_URL,
            params={"instrument": "stock", "ticker": ticker, "timeframe": "d", "barsCount": count},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return parse_chart(response.json(), ticker)


def new_york_time(now):
    """UTC to US Eastern wall time, with US daylight saving rules (no tz database needed)."""
    year = now.year
    march_8, november_1 = date(year, 3, 8), date(year, 11, 1)
    dst_start = march_8 + timedelta(days=(6 - march_8.weekday()) % 7)
    dst_end = november_1 + timedelta(days=(6 - november_1.weekday()) % 7)
    start = datetime(year, 3, dst_start.day, 7, tzinfo=timezone.utc)
    end = datetime(year, 11, dst_end.day, 6, tzinfo=timezone.utc)
    return now + timedelta(hours=-4 if start <= now < end else -5)


def completed_bars(bars, now):
    """Drop today's bar until the session is over, so a signal never uses a partial day."""
    local = new_york_time(now)
    keep = len(bars)
    while keep and (bars.dates[keep - 1] > local.date() or (
            bars.dates[keep - 1] == local.date() and (local.hour, local.minute) < SESSION_FINAL_AFTER)):
        keep -= 1
    return bars.head(keep) if keep != len(bars) else bars


def quality_problems(bars, calendar, cfg):
    """Reasons the data cannot be trusted. calendar: sorted session dates from the benchmark."""
    n = cfg["min_bars"]
    if len(bars) < n:
        return [f"only {len(bars)} daily bars, need {n}"]
    problems = []
    # Every bar, not only recent ones: one bad value would poison the running averages after it.
    for i in range(len(bars)):
        o, h, l, c, v = bars.open[i], bars.high[i], bars.low[i], bars.close[i], bars.volume[i]
        if not all(math.isfinite(x) for x in (o, h, l, c, v)) or min(o, h, l, c) <= 0 or v < 0:
            problems.append(f"missing or invalid values on {bars.dates[i]}")
            break
        slack = 1e-6 * h
        if h + slack < max(o, c, l) or l - slack > min(o, c, h):
            problems.append(f"inconsistent high/low on {bars.dates[i]}")
            break
    zero = sum(1 for i in range(len(bars) - 50, len(bars)) if bars.volume[i] == 0)
    if zero > cfg["max_zero_volume_days"]:
        problems.append(f"{zero} days without volume in the last 50")
    if calendar and bars.dates[-1] < calendar[-1]:
        problems.append(f"last bar {bars.dates[-1]} is older than the last session {calendar[-1]}")
    if calendar:
        start = bars.dates[-60]
        expected = [d for d in calendar if start <= d <= bars.dates[-1]]
        missing = len(set(expected) - set(bars.dates))
        if missing > cfg["max_missing_sessions"]:
            problems.append(f"{missing} sessions missing in the last 60")
    return problems


# ---------------------------------------------------------------- indicators
# Each returns one value per bar; the value at i uses only bars 0..i.


def sma(values, n):
    out, total = [None] * len(values), 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values, n):
    """Seeded with the simple average of the first n values."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    k, current = 2 / (n + 1), sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


def true_range(high, low, close):
    return [None] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, len(close))]


def wilder(values, n):
    """Wilder smoothing of values[1:], first value at index n."""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    current = sum(values[1:n + 1]) / n
    out[n] = current
    for i in range(n + 1, len(values)):
        current = (current * (n - 1) + values[i]) / n
        out[i] = current
    return out


def atr(high, low, close, n=14):
    return wilder(true_range(high, low, close), n)


def rsi(close, n=14):
    changes = [None] + [close[i] - close[i - 1] for i in range(1, len(close))]
    gains = wilder([None] + [max(c, 0.0) for c in changes[1:]], n)
    losses = wilder([None] + [max(-c, 0.0) for c in changes[1:]], n)
    return [None if g is None else 100.0 if l == 0 else 100 - 100 / (1 + g / l) for g, l in zip(gains, losses)]


def prior_mean(values, n):
    """Mean of the n values before i, excluding i."""
    out, total = [None] * len(values), 0.0
    for i, value in enumerate(values):
        if i >= n:
            out[i] = total / n
            total -= values[i - n]
        total += value
    return out


def relative_strength(bars, benchmark, lookback):
    """Ratio of the stock to the benchmark close on the same date, and its change over lookback bars."""
    line = [bars.close[i] / benchmark[d] if benchmark.get(d) else None for i, d in enumerate(bars.dates)]
    change = [None] * len(line)
    for i in range(lookback, len(line)):
        if line[i] is not None and line[i - lookback] is not None:
            change[i] = line[i] / line[i - lookback] - 1
    return line, change


def compute(bars, benchmarks, cfg):
    series = {
        "ema20": ema(bars.close, 20),
        "ema50": ema(bars.close, 50),
        "sma200": sma(bars.close, 200),
        "atr14": atr(bars.high, bars.low, bars.close, 14),
        "rsi14": rsi(bars.close, 14),
        "volume_avg50": prior_mean(bars.volume, 50),
        "dollar_volume_avg50": prior_mean([c * v for c, v in zip(bars.close, bars.volume)], 50),
    }
    for name, closes in benchmarks.items():
        series[f"rs_line_{name}"], series[f"rs_{name}"] = relative_strength(bars, closes, cfg["rs_lookback"])
    return series


# ---------------------------------------------------------------- strategies


def _check(checks, name, passed, detail):
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _points(scores, name, points, earned, detail):
    scores.append({"name": name, "points": points, "earned": points if earned else 0, "detail": detail})


def _rs_score(scores, series, t, benchmarks, points):
    for name, value in zip(benchmarks, points):
        rs = series.get(f"rs_{name}", [None] * (t + 1))[t]
        _points(scores, f"outperforms_{name}", value, rs is not None and rs > 0,
                "no aligned benchmark data" if rs is None else f"{rs * 100:+.1f}% vs {name} over the lookback")


def _context(bars, series, t, cfg):
    """Indicator values at t, or None if history is too short."""
    needed = ("ema20", "ema50", "sma200", "atr14", "rsi14", "volume_avg50", "dollar_volume_avg50")
    if t < max(252, cfg["breakout_lookback"] + 1) or any(series[k][t] is None for k in needed) or series["ema50"][t - 10] is None:
        return None
    values = {k: series[k][t] for k in needed}
    values["close"] = bars.close[t]
    return values


def _liquidity(checks, ctx, cfg):
    _check(checks, "price", ctx["close"] >= cfg["min_price"], f"close {ctx['close']:.2f}, minimum {cfg['min_price']:g}")
    _check(checks, "liquidity", ctx["dollar_volume_avg50"] >= cfg["min_dollar_volume"],
           f"50-day average dollar volume {ctx['dollar_volume_avg50'] / 1e6:.1f}M, minimum {cfg['min_dollar_volume'] / 1e6:g}M")


def _result(strategy, bars, t, checks, scores, levels, ctx):
    passed = all(c["passed"] for c in checks)
    return {
        "strategy": strategy,
        "ticker": bars.ticker,
        "signal_date": bars.dates[t].isoformat(),
        "qualified": passed,
        "checks": checks,
        "score": sum(s["earned"] for s in scores) if passed else None,
        "scoring": scores if passed else [],
        "levels": levels,
        "indicators": {k: round(v, 4) for k, v in (ctx or {}).items()},
    }


def _levels(entry, stop, target):
    risk = entry - stop
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
            "risk_per_share": round(risk, 2), "reward_risk": round((target - entry) / risk, 2) if risk > 0 else None}


def evaluate_pullback(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("pullback", bars, t, checks, scores, None, None)
    h, l, o, c, v = bars.high, bars.low, bars.open, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    _liquidity(checks, ctx, cfg)
    _check(checks, "uptrend", c[t] > m200 and e20 > e50 > m200,
           f"close {c[t]:.2f}, EMA20 {e20:.2f}, EMA50 {e50:.2f}, SMA200 {m200:.2f}")
    _check(checks, "ema50_rising", e50 > series["ema50"][t - 10], f"EMA50 {e50:.2f} vs {series['ema50'][t - 10]:.2f} ten bars ago")
    swing_high, pull_low = max(h[t - 19:t + 1]), min(l[t - 2:t + 1])
    depth = (swing_high - pull_low) / a
    _check(checks, "pullback_depth", depth >= cfg["pullback_min_atr"],
           f"low of last 3 bars {pull_low:.2f} is {depth:.2f} ATR below the 20-day high {swing_high:.2f}, minimum {cfg['pullback_min_atr']:g}")
    tolerance = cfg["touch_atr"] * a
    touched = "EMA50" if pull_low <= e50 + tolerance else "EMA20" if pull_low <= e20 + tolerance else None
    _check(checks, "ema_touch", touched, f"low of last 3 bars {pull_low:.2f} reached {touched}" if touched
           else f"low of last 3 bars {pull_low:.2f} stayed above EMA20 {e20:.2f} + {cfg['touch_atr']:g} ATR")
    _check(checks, "holds_ema50", c[t] > e50, f"close {c[t]:.2f} vs EMA50 {e50:.2f}")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _check(checks, "reversal_bar", c[t] > o[t] and c[t] > c[t - 1] and location >= cfg["reversal_close_location"],
           f"open {o[t]:.2f}, close {c[t]:.2f}, previous close {c[t - 1]:.2f}, close at {location:.0%} of the day's range")
    entry = h[t] + 0.01
    stop = pull_low - cfg["pullback_stop_atr"] * a
    levels = _levels(entry, stop, swing_high)
    risk = entry - stop
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels["reward_risk"] is not None and levels["reward_risk"] >= cfg["pullback_min_rr"],
           f"reward/risk {levels['reward_risk']} to the 20-day high, minimum {cfg['pullback_min_rr']:g}")
    benchmarks = cfg["benchmarks"]
    _rs_score(scores, series, t, benchmarks[:2], (20, 10))
    _points(scores, "healthy_rsi", 15, 40 <= ctx["rsi14"] <= 60, f"RSI14 {ctx['rsi14']:.1f}, best 40-60")
    quiet = sum(v[t - 5:t]) / 5
    _points(scores, "volume_dry_up", 15, quiet < ctx["volume_avg50"], f"pullback volume {quiet / ctx['volume_avg50']:.2f}x the 50-day average")
    _points(scores, "reversal_volume", 10, v[t] > v[t - 1], f"signal volume {v[t]:,.0f} vs {v[t - 1]:,.0f} the day before")
    _points(scores, "closes_above_prior_high", 10, c[t] > h[t - 1], f"close {c[t]:.2f} vs previous high {h[t - 1]:.2f}")
    _points(scores, "ema_stack", 10, c[t] > e20 > e50 > m200, "close > EMA20 > EMA50 > SMA200")
    _points(scores, "reward_risk_3", 10, (levels["reward_risk"] or 0) >= 2.5, f"reward/risk {levels['reward_risk']}, best 2.5 or more")
    return _result("pullback", bars, t, checks, scores, levels, ctx)


def resistance(bars, end, lookback, tolerance):
    """Highest high of the lookback bars before end, its age, and how many separate times price came within tolerance."""
    start = end - lookback
    highs = bars.high[start:end]
    level = max(highs)
    last = start + max(i for i, x in enumerate(highs) if x == level)
    touches, inside = 0, False
    for x in highs:
        near = x >= level - tolerance
        touches += near and not inside
        inside = near
    return level, end - last, touches, min(bars.low[start:end])


def evaluate_breakout(bars, series, t, cfg):
    checks, scores = [], []
    ctx = _context(bars, series, t, cfg)
    if ctx is None:
        _check(checks, "history", False, "not enough history for EMA50, SMA200 and ATR14")
        return _result("breakout", bars, t, checks, scores, None, None)
    h, l, c, v = bars.high, bars.low, bars.close, bars.volume
    a, e20, e50, m200 = ctx["atr14"], ctx["ema20"], ctx["ema50"], ctx["sma200"]
    lookback = cfg["breakout_lookback"]
    _liquidity(checks, ctx, cfg)
    _check(checks, "uptrend", c[t] > e20 > e50 > m200, f"close {c[t]:.2f}, EMA20 {e20:.2f}, EMA50 {e50:.2f}, SMA200 {m200:.2f}")
    level, age, touches, base_low = resistance(bars, t, lookback, cfg["resistance_touch_atr"] * a)
    _check(checks, "base_age", age >= cfg["min_base_bars"], f"{lookback}-day high {level:.2f} set {age} bars ago, minimum {cfg['min_base_bars']}")
    _check(checks, "resistance_touches", touches >= cfg["min_touches"],
           f"{touches} separate tests of {level:.2f} within {cfg['resistance_touch_atr']:g} ATR, minimum {cfg['min_touches']}")
    depth = (level - base_low) / level
    _check(checks, "base_depth", depth <= cfg["max_base_depth"], f"base {depth:.0%} deep ({base_low:.2f}-{level:.2f}), maximum {cfg['max_base_depth']:.0%}")
    previous_level = max(h[t - 1 - lookback:t - 1])
    _check(checks, "fresh_breakout", c[t] > level and c[t - 1] <= previous_level,
           f"close {c[t]:.2f} vs resistance {level:.2f}; previous close {c[t - 1]:.2f} vs {previous_level:.2f}")
    extension = (c[t] - level) / a
    _check(checks, "not_extended", extension <= cfg["max_extension_atr"], f"{extension:.2f} ATR above resistance, maximum {cfg['max_extension_atr']:g}")
    rel_volume = v[t] / ctx["volume_avg50"] if ctx["volume_avg50"] else 0.0
    ctx["rel_volume"] = rel_volume
    _check(checks, "volume", rel_volume >= cfg["min_rel_volume"], f"relative volume {rel_volume:.2f}, minimum {cfg['min_rel_volume']:g}")
    entry = c[t]
    stop = min(l[t], level) - cfg["breakout_stop_atr"] * a
    levels = _levels(entry, stop, entry + (level - base_low))
    levels["resistance"] = round(level, 2)
    risk = entry - stop
    _check(checks, "risk_size", 0 < risk <= cfg["max_risk_atr"] * a, f"risk {risk:.2f} = {risk / a:.2f} ATR, maximum {cfg['max_risk_atr']:g}")
    _check(checks, "reward_risk", levels["reward_risk"] is not None and levels["reward_risk"] >= cfg["breakout_min_rr"],
           f"reward/risk {levels['reward_risk']} to the measured move, minimum {cfg['breakout_min_rr']:g}")
    benchmarks = cfg["benchmarks"]
    _points(scores, "strong_volume", 20, rel_volume >= 2, f"relative volume {rel_volume:.2f}, best 2 or more")
    _rs_score(scores, series, t, benchmarks[:2], (15, 10))
    line = series.get(f"rs_line_{benchmarks[0]}", [None] * (t + 1)) if benchmarks else [None] * (t + 1)
    window = [x for x in line[t - 49:t + 1] if x is not None]
    _points(scores, "rs_line_high", 15, line[t] is not None and line[t] >= max(window),
            f"relative strength line vs {benchmarks[0] if benchmarks else '-'} at a 50-day high" if line[t] is not None else "no aligned benchmark data")
    span = h[t] - l[t]
    location = (c[t] - l[t]) / span if span > 0 else 0.0
    _points(scores, "strong_close", 10, location >= 0.75, f"close at {location:.0%} of the day's range")
    tight = (level - min(l[t - 10:t])) / a
    _points(scores, "tight_base", 10, tight <= 3, f"last 10 bars span {tight:.1f} ATR below resistance, best 3 or less")
    year_high = max(h[t - 251:t + 1])
    _points(scores, "near_52w_high", 10, c[t] >= 0.9 * year_high, f"close {c[t] / year_high:.0%} of the 52-week high {year_high:.2f}")
    _points(scores, "reward_risk_3", 10, (levels["reward_risk"] or 0) >= 3, f"reward/risk {levels['reward_risk']}, best 3 or more")
    return _result("breakout", bars, t, checks, scores, levels, ctx)


EVALUATORS = {"pullback": evaluate_pullback, "breakout": evaluate_breakout}


# ---------------------------------------------------------------- validation


def simulate(bars, t, signal, horizon):
    """Outcome of a signal on bar t, using only bars after t.

    Pullback: buy-stop at entry, valid on the next bar only. Breakout: buy at the next open.
    When the stop and the target fall in the same bar, the stop counts (conservative).
    """
    levels = signal["levels"]
    stop, target = levels["stop"], levels["target"]
    i = t + 1
    if i + horizon > len(bars):
        return {"outcome": "open"}
    if signal["strategy"] == "pullback":
        if bars.high[i] < levels["entry"]:
            return {"outcome": "not_filled"}
        fill = max(bars.open[i], levels["entry"])
    else:
        fill = bars.open[i]
    if fill <= stop:
        return {"outcome": "not_filled", "detail": "opened below the stop"}
    risk = fill - stop
    for j in range(i, i + horizon):
        if bars.low[j] <= stop:
            exit_price, outcome = (stop if j == i else min(bars.open[j], stop)), "stop"
        elif bars.high[j] >= target:
            exit_price, outcome = (target if j == i else max(bars.open[j], target)), "target"
        else:
            continue
        return {"outcome": outcome, "fill": round(fill, 2), "exit": round(exit_price, 2), "exit_date": bars.dates[j].isoformat(),
                "r": round((exit_price - fill) / risk, 2), "bars_held": j - t}
    j = i + horizon - 1
    return {"outcome": "timeout", "fill": round(fill, 2), "exit": round(bars.close[j], 2), "exit_date": bars.dates[j].isoformat(),
            "r": round((bars.close[j] - fill) / risk, 2), "bars_held": j - t}


def validate(bars, series, strategies, days, cfg):
    """Walk forward over `days` signal days that ended at least `horizon` bars ago, so every trade can finish.

    One open trade per ticker and strategy at a time."""
    horizon = cfg["validation_horizon"]
    last = len(bars) - 1 - horizon
    trades = []
    for strategy in strategies:
        busy_until = -1
        for t in range(max(cfg["min_bars"] - 1, last - days), last):
            if t <= busy_until:
                continue
            signal = EVALUATORS[strategy](bars, series, t, cfg)
            if not signal["qualified"]:
                continue
            outcome = simulate(bars, t, signal, horizon)
            trades.append({"ticker": bars.ticker, "strategy": strategy, "signal_date": signal["signal_date"],
                           "score": signal["score"], **signal["levels"], **outcome})
            if "bars_held" in outcome:
                busy_until = t + outcome["bars_held"]
    return trades


def summarize(trades):
    closed = [x for x in trades if "r" in x]
    wins = [x for x in closed if x["r"] > 0]
    gains, losses = sum(x["r"] for x in wins), -sum(x["r"] for x in closed if x["r"] <= 0)
    return {
        "signals": len(trades),
        "not_filled": sum(x["outcome"] == "not_filled" for x in trades),
        "still_open": sum(x["outcome"] == "open" for x in trades),
        "closed": len(closed),
        "targets": sum(x["outcome"] == "target" for x in closed),
        "stops": sum(x["outcome"] == "stop" for x in closed),
        "timeouts": sum(x["outcome"] == "timeout" for x in closed),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "average_r": round((gains - losses) / len(closed), 3) if closed else None,
        "profit_factor": round(gains / losses, 2) if losses else None,
    }


# ---------------------------------------------------------------- main


def settings_with_defaults(settings):
    cfg = {**DEFAULTS, "benchmarks": list(DEFAULTS["benchmarks"])}
    for key, value in (settings or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}. Settings: {', '.join(DEFAULTS)}")
        if key == "benchmarks":
            if not isinstance(value, list) or not value or not all(isinstance(x, str) and TICKER.match(x) for x in value):
                raise ValueError("benchmarks must be a list of tickers, e.g. [\"QQQ\", \"SMH\"]")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"setting {key} must be a number of 0 or more")
        cfg[key] = value
    for key in ("min_bars", "breakout_lookback", "min_base_bars", "min_touches", "validation_horizon", "rs_lookback"):
        cfg[key] = int(cfg[key])
        if cfg[key] < 1:
            raise ValueError(f"setting {key} must be 1 or more")
    cfg["min_bars"] = max(cfg["min_bars"], 253, cfg["breakout_lookback"] + 2)
    return cfg


def discover(finviz_url, tickers, max_tickers):
    if tickers.strip():
        names = [x.strip().upper() for x in tickers.split(",") if x.strip()]
        bad = [x for x in names if not TICKER.match(x)]
        if bad:
            raise ValueError(f"not tickers: {', '.join(bad)}")
        return list(dict.fromkeys(names))[:max_tickers], "tickers"
    url = urlparse(finviz_url)
    if url.scheme != "https" or url.hostname not in FINVIZ_HOSTS:
        raise ValueError(f"not a Finviz screener URL: {finviz_url}")
    try:
        rows = list(Screener.init_from_url(finviz_url, rows=max_tickers))
    except NoResults:
        rows = []
    return list(dict.fromkeys(r["Ticker"] for r in rows if TICKER.match(r.get("Ticker", ""))))[:max_tickers], "finviz"


def main(finviz_url: str = "", tickers: str = "", strategy: str = "both", max_tickers: int = 50,
         validation_days: int = 0, settings: dict | None = None) -> dict:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    strategies = list(STRATEGIES) if strategy == "both" else [strategy]
    if strategy != "both" and strategy not in STRATEGIES:
        raise ValueError(f"strategy must be both, {' or '.join(STRATEGIES)}")
    if not finviz_url.strip() and not tickers.strip():
        raise ValueError("give a Finviz screener URL or a comma-separated list of tickers")
    if not 1 <= max_tickers <= MAX_TICKERS:
        raise ValueError(f"max_tickers must be 1 to {MAX_TICKERS}")
    if not 0 <= validation_days <= MAX_VALIDATION_DAYS:
        raise ValueError(f"validation_days must be 0 to {MAX_VALIDATION_DAYS}")
    cfg = settings_with_defaults(settings)
    count = cfg["min_bars"] + validation_days + cfg["validation_horizon"] + 60

    names, source = discover(finviz_url, tickers, max_tickers)
    benchmarks, benchmark_info, data_errors = {}, {}, []
    for name in cfg["benchmarks"]:
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            benchmarks[name] = {d: c for d, c in zip(bars.dates, bars.close) if math.isfinite(c) and c > 0}
            benchmark_info[name] = {"as_of": bars.dates[-1].isoformat(), "close": round(bars.close[-1], 2)}
        except (requests.RequestException, ValueError) as error:
            data_errors.append({"ticker": name, "reason": f"benchmark: {error}"})
        time.sleep(cfg["request_pause"])
    if not benchmarks:
        raise RuntimeError("no benchmark data, so the market calendar and relative strength cannot be checked")
    calendar = sorted(set().union(*benchmarks.values()))
    rs_benchmarks = [b for b in cfg["benchmarks"] if b in benchmarks]

    results = {s: {"qualified": [], "rejected": []} for s in strategies}
    trades = []
    for name in names:
        try:
            bars = completed_bars(fetch_bars(name, count), now)
            problems = quality_problems(bars, calendar, cfg)
        except (requests.RequestException, ValueError) as error:
            problems = [str(error)]
        time.sleep(cfg["request_pause"])
        if problems:
            data_errors.append({"ticker": name, "reason": "; ".join(problems)})
            continue
        series = compute(bars, {b: benchmarks[b] for b in rs_benchmarks}, cfg)
        for s in strategies:
            signal = EVALUATORS[s](bars, series, len(bars) - 1, {**cfg, "benchmarks": rs_benchmarks})
            if signal["qualified"]:
                results[s]["qualified"].append({k: signal[k] for k in ("ticker", "signal_date", "score", "levels", "checks", "scoring", "indicators")})
            else:
                results[s]["rejected"].append({"ticker": name, "failed": [c for c in signal["checks"] if not c["passed"]]})
        if validation_days:
            trades += validate(bars, series, strategies, validation_days, {**cfg, "benchmarks": rs_benchmarks})
    for s in strategies:
        results[s]["qualified"].sort(key=lambda x: (-x["score"], x["ticker"]))

    duration = time.monotonic() - started
    counts = ", ".join(f"{s} {len(results[s]['qualified'])}" for s in strategies)
    print(f"Summary: {len(names)} tickers, {len(data_errors)} data errors, qualified: {counts}, {duration:.1f}s")
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "as_of": calendar[-1].isoformat(),
        "universe": {"source": source, "tickers": len(names), "data_errors": len(data_errors)},
        "benchmarks": benchmark_info,
        "results": results,
        "data_errors": data_errors,
        "validation": None if not validation_days else {
            "days": validation_days,
            "horizon_bars": cfg["validation_horizon"],
            "note": "Tickers come from today's screen, so past results carry selection bias. Fills ignore slippage and fees.",
            "summary": {s: summarize([x for x in trades if x["strategy"] == s]) for s in strategies},
            "trades": trades,
        },
        "settings": cfg,
        "duration_seconds": round(duration, 2),
    }
